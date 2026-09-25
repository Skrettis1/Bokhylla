import os
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parents[1]
DATA = BASE / "data"
DB = DATA / "library.db"
COVERS = DATA / "covers"
BACKUPS = DATA / "backups"
STATIC = BASE / "app" / "static"

AUTHORS = [
    "Jo Nesbø", "J.K. Rowling", "Dan Brown", "Lee Child", "Lars Kepler",
    "Tom Egeland", "Jørn Lier Horst", "Thomas Enger", "Ørjan Karlsson"
]

scheduler = AsyncIOScheduler(timezone=os.getenv("TZ", "Europe/Oslo"))

def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection

def init_db():
    DATA.mkdir(parents=True, exist_ok=True)
    COVERS.mkdir(parents=True, exist_ok=True)
    BACKUPS.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS authors(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          openlibrary_key TEXT,
          last_sync TEXT
        );
        CREATE TABLE IF NOT EXISTS books(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          author_id INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
          work_key TEXT,
          title TEXT NOT NULL,
          publish_year INTEGER,
          series_name TEXT NOT NULL DEFAULT 'Ikke plassert i serie',
          series_order REAL,
          read INTEGER NOT NULL DEFAULT 0,
          tags TEXT NOT NULL DEFAULT '',
          cover_file TEXT,
          hidden INTEGER NOT NULL DEFAULT 0,
          manual_lock INTEGER NOT NULL DEFAULT 0,
          UNIQUE(author_id, work_key)
        );
        CREATE TABLE IF NOT EXISTS sync_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          author_id INTEGER,
          author_name TEXT,
          started TEXT NOT NULL,
          finished TEXT,
          status TEXT NOT NULL,
          message TEXT
        );
        """)
        for column in ("author_id INTEGER", "author_name TEXT"):
            try:
                con.execute(f"ALTER TABLE sync_log ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        for name in AUTHORS:
            con.execute("INSERT OR IGNORE INTO authors(name) VALUES(?)", (name,))

def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", value)

def headers():
    agent = os.getenv("OPENLIBRARY_USER_AGENT", "BokhyllaHome/1.0")
    contact = os.getenv("OPENLIBRARY_CONTACT", "change-me@example.invalid")
    return {"User-Agent": f"{agent} ({contact})"}

async def download_cover(client, cover_id, work_key):
    if not cover_id:
        return None
    filename = re.sub(r"[^A-Za-z0-9_-]", "_", work_key) + ".jpg"
    target = COVERS / filename
    if target.exists() and target.stat().st_size > 1000:
        return filename
    url = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg?default=false"
    response = await client.get(url, follow_redirects=True)
    if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
        target.write_bytes(response.content)
        return filename
    return None

async def sync_author(author_id: int):
    started = datetime.now().isoformat(timespec="seconds")
    with db() as con:
        author = con.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
        if not author:
            raise HTTPException(404, "Forfatter ikke funnet")
        log_id = con.execute(
            "INSERT INTO sync_log(author_id,author_name,started,status,message) VALUES(?,?,?,?,?)",
            (author_id, author["name"], started, "running", None),
        ).lastrowid

    try:
        async with httpx.AsyncClient(timeout=30, headers=headers()) as client:
            search = await client.get("https://openlibrary.org/search/authors.json", params={"q": author["name"]})
            search.raise_for_status()
            docs = search.json().get("docs", [])
            match = next((x for x in docs if normalize(x.get("name", "")) == normalize(author["name"])), None)
            if not match and len(docs) == 1:
                match = docs[0]
            if not match or not match.get("key"):
                hint = f" ({len(docs)} kandidater funnet, ingen eksakt treff)" if docs else ""
                raise RuntimeError(f"Fant ikke forfatteren i Open Library{hint}")
            key = match["key"]
            works_response = await client.get(f"https://openlibrary.org/authors/{key}/works.json", params={"limit": 1000})
            works_response.raise_for_status()
            entries = works_response.json().get("entries", [])
            inserted = 0
            updated = 0
            seen = set()
            with db() as con:
                for work in entries:
                    work_key = work.get("key")
                    title = str(work.get("title") or "Uten tittel").strip()
                    if not work_key or work_key in seen:
                        continue
                    seen.add(work_key)
                    existing = con.execute("SELECT * FROM books WHERE author_id=? AND work_key=?", (author_id, work_key)).fetchone()
                    cover_id = (work.get("covers") or [None])[0]
                    cover_file = existing["cover_file"] if existing else None
                    if not cover_file:
                        cover_file = await download_cover(client, cover_id, work_key)
                    year_raw = work.get("first_publish_date")
                    try:
                        year = int(str(year_raw)[:4]) if year_raw else None
                    except ValueError:
                        year = None
                    if existing:
                        if not existing["manual_lock"]:
                            con.execute("UPDATE books SET title=?,publish_year=?,cover_file=COALESCE(?,cover_file) WHERE id=?", (title, year, cover_file, existing["id"]))
                        updated += 1
                    else:
                        con.execute("INSERT INTO books(author_id,work_key,title,publish_year,cover_file) VALUES(?,?,?,?,?)", (author_id, work_key, title, year, cover_file))
                        inserted += 1
                finished = datetime.now().isoformat(timespec="seconds")
                con.execute("UPDATE authors SET openlibrary_key=?,last_sync=? WHERE id=?", (key, finished, author_id))
                con.execute("UPDATE sync_log SET finished=?,status='ok',message=? WHERE id=?", (finished, f"{inserted} nye, {updated} oppdatert", log_id))
            return {"status": "ok", "inserted": inserted, "updated": updated, "works": len(seen)}
    except Exception as exc:
        finished = datetime.now().isoformat(timespec="seconds")
        with db() as con:
            con.execute("UPDATE sync_log SET finished=?,status='error',message=? WHERE id=?", (finished, str(exc), log_id))
        raise

async def sync_all():
    with db() as con:
        ids = [row["id"] for row in con.execute("SELECT id FROM authors ORDER BY id")]
    for author_id in ids:
        try:
            await sync_author(author_id)
        except Exception:
            pass
    backup_db()

def backup_db():
    if DB.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(DB, BACKUPS / f"library-{stamp}.db")
        backups = sorted(BACKUPS.glob("library-*.db"), reverse=True)
        for old in backups[10:]:
            if old.exists():
                old.unlink()

class AuthorCreate(BaseModel):
    name: str

class BookUpdate(BaseModel):
    read: Optional[bool] = None
    tags: Optional[List[str]] = None
    series_name: Optional[str] = None
    series_order: Optional[float] = None
    hidden: Optional[bool] = None

app = FastAPI(title="Bokhylla", version="0.1.0")

@app.on_event("startup")
async def startup():
    init_db()
    day = os.getenv("SYNC_DAY", "sun")
    hour = int(os.getenv("SYNC_HOUR", "3"))
    scheduler.add_job(sync_all, "cron", day_of_week=day, hour=hour, minute=0, id="weekly-sync", replace_existing=True)
    scheduler.start()

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

app.mount("/covers", StaticFiles(directory=COVERS, check_dir=False), name="covers")
app.mount("/assets", StaticFiles(directory=STATIC, check_dir=False), name="assets")

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/authors")
def get_authors():
    with db() as con:
        rows = con.execute("""
          SELECT a.*, COUNT(b.id) book_count, COALESCE(SUM(b.read),0) read_count
          FROM authors a LEFT JOIN books b ON b.author_id=a.id AND b.hidden=0
          GROUP BY a.id ORDER BY a.name COLLATE NOCASE
        """).fetchall()
    return [dict(row) for row in rows]

@app.post("/api/authors")
def create_author(payload: AuthorCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Navn kan ikke være tomt")
    with db() as con:
        try:
            cur = con.execute("INSERT INTO authors(name) VALUES(?)", (name,))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Forfatteren finnes allerede")
        author_id = cur.lastrowid
    return {"id": author_id, "name": name}

@app.delete("/api/authors/{author_id}")
def delete_author(author_id: int):
    with db() as con:
        cur = con.execute("DELETE FROM authors WHERE id=?", (author_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Forfatter ikke funnet")
    return {"status": "ok"}

@app.get("/api/sync-log")
def get_sync_log(limit: int = 20):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]

@app.get("/api/authors/{author_id}/books")
def get_books(author_id: int):
    with db() as con:
        rows = con.execute("""
          SELECT * FROM books WHERE author_id=? AND hidden=0
          ORDER BY series_name COLLATE NOCASE, COALESCE(series_order,999999), COALESCE(publish_year,9999), title COLLATE NOCASE
        """, (author_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["read"] = bool(item["read"])
        item["tags"] = [x for x in item["tags"].split(",") if x]
        item["cover_url"] = f"/covers/{item['cover_file']}" if item["cover_file"] else None
        result.append(item)
    return result

@app.post("/api/authors/{author_id}/sync")
async def start_sync(author_id: int):
    try:
        return await sync_author(author_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc))

@app.post("/api/sync-all")
async def start_sync_all():
    await sync_all()
    return {"status": "ok"}

@app.patch("/api/books/{book_id}")
def update_book(book_id: int, update: BookUpdate):
    fields = []
    values = []
    data = update.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "tags":
            value = ",".join(sorted(set(x for x in value if x in {"A", "H", "T"})))
        if key in {"read", "hidden"}:
            value = int(bool(value))
        fields.append(f"{key}=?")
        values.append(value)
    if not fields:
        return {"status": "unchanged"}
    fields.append("manual_lock=1")
    values.append(book_id)
    with db() as con:
        cur = con.execute(f"UPDATE books SET {', '.join(fields)} WHERE id=?", values)
        if cur.rowcount == 0:
            raise HTTPException(404, "Bok ikke funnet")
    return {"status": "ok"}

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
