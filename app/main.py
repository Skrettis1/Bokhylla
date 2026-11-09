import asyncio
import os
import re
import shutil
import sqlite3
import unicodedata
from contextlib import asynccontextmanager
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
DATA = Path(os.getenv("BOKHYLLA_DATA_DIR", str(BASE / "data")))
DB = DATA / "library.db"
COVERS = DATA / "covers"
BACKUPS = DATA / "backups"
STATIC = BASE / "app" / "static"

AUTHORS = [
    "Jo Nesbø", "J.K. Rowling", "Dan Brown", "Lee Child", "Lars Kepler",
    "Tom Egeland", "Jørn Lier Horst", "Thomas Enger", "Ørjan Karlsson"
]

NO_SERIES = "Ikke plassert i serie"
COVER_DOWNLOADS_AT_ONCE = 4

scheduler = AsyncIOScheduler(timezone=os.getenv("TZ", "Europe/Oslo"))

# "Sync all" runs in the background; this tells the UI whether it is still going.
sync_all_state = {"running": False}

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
        # A sync that was cut off (the app stopped mid-way) is no longer running.
        con.execute(
            "UPDATE sync_log SET status='error', message='Avbrutt' WHERE status='running'"
        )

def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", value)

def headers():
    agent = os.getenv("OPENLIBRARY_USER_AGENT", "BokhyllaHome/1.0")
    contact = os.getenv("OPENLIBRARY_CONTACT", "change-me@example.invalid")
    return {"User-Agent": f"{agent} ({contact})"}

def now():
    return datetime.now().isoformat(timespec="seconds")

NB_API = "https://api.nb.no/catalog/v1/items"
NB_LANGUAGES = ("nob", "nno")
NORWEGIAN = {"nob", "nno", "nor"}
NB_PAGE_SIZE = 100

def nb_creator(name: str) -> str:
    """Author name as the National Library catalogues it: "Nesbø, Jo"."""
    name = " ".join(name.split())
    if "," in name:
        return name
    parts = name.split(" ")
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name

def main_title(title: str) -> str:
    """The title without its subtitle (" : kriminalroman") and doubled spaces."""
    return " ".join(re.split(r"\s+:\s+", title.strip())[0].split())

def cover_filename(work_key):
    return re.sub(r"[^A-Za-z0-9_-]", "_", work_key) + ".jpg"

async def download_image(client, url, filename):
    """Saves an image to the cover folder; returns the file name or None."""
    target = COVERS / filename
    if target.exists() and target.stat().st_size > 1000:
        return filename
    try:
        response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if (
        response.status_code == 200
        and response.headers.get("content-type", "").startswith("image/")
        and len(response.content) > 1000
    ):
        target.write_bytes(response.content)
        return filename
    return None

async def fetch_norwegian_editions(client, creator):
    """Every Norwegian (bokmål/nynorsk) book edition by the author in the National Library."""
    editions = []
    for language in NB_LANGUAGES:
        page = 0
        while True:
            response = await client.get(NB_API, params=[
                ("q", "*"),
                ("filter", f'creator:"{creator}"'),
                ("filter", "mediatype:bøker"),
                ("filter", f"languages:{language}"),
                ("size", NB_PAGE_SIZE),
                ("page", page),
            ])
            response.raise_for_status()
            data = response.json()
            editions += data.get("_embedded", {}).get("items", [])
            total_pages = data.get("page", {}).get("totalPages", 0)
            page += 1
            if page >= total_pages or page >= 20:
                break
    return editions

def group_by_title(editions):
    """One book per title: earliest year, and a cover from any edition that has one."""
    books = {}
    for item in editions:
        meta = item.get("metadata", {})
        title = meta.get("title") or ""
        # "A ; B" are collections of several books in one volume.
        if not title or " ; " in title:
            continue
        # Translations are catalogued with the original language as well
        # (e.g. ["eng", "nob", "aze"]); only purely Norwegian editions count.
        languages = {l.get("code") for l in meta.get("languages", [])}
        if not languages or not languages <= NORWEGIAN:
            continue
        name = main_title(title)
        key = normalize(name)
        if not key:
            continue
        year = parse_year(meta.get("originInfo", {}).get("issued"))
        thumb = (item.get("_links", {}).get("thumbnail_custom") or {}).get("href")
        book = books.setdefault(key, {"work_key": f"nb:{key}", "title": name, "year": year, "cover_url": None})
        if year and (book["year"] is None or year < book["year"]):
            book["year"] = year
        if thumb and not book["cover_url"]:
            book["cover_url"] = thumb.replace("{width},{height}", "0,300")
    return list(books.values())

async def open_library_lookup(client, title, author_name):
    """The Open Library work with exactly this title by the author, if any.

    Returns (first publish year, cover URL). Used to correct the year (the
    National Library does not always have the first edition) and as a
    fallback cover.
    """
    try:
        response = await client.get(
            "https://openlibrary.org/search.json",
            params={"title": title, "author": author_name, "fields": "title,first_publish_year,cover_i", "limit": 5},
        )
        docs = response.json().get("docs", []) if response.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None, None
    match = next((d for d in docs if normalize(d.get("title", "")) == normalize(title)), None)
    if not match:
        return None, None
    cover_id = match.get("cover_i")
    cover = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg?default=false" if cover_id else None
    return match.get("first_publish_year"), cover

def parse_year(value):
    try:
        return int(str(value)[:4]) if value else None
    except ValueError:
        return None

async def sync_author(author_id: int):
    """Fetches the author's books published in Norwegian from the National Library."""
    with db() as con:
        author = con.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
        if not author:
            raise HTTPException(404, "Forfatter ikke funnet")
        log_id = con.execute(
            "INSERT INTO sync_log(author_id,author_name,started,status,message) VALUES(?,?,?,?,?)",
            (author_id, author["name"], now(), "running", None),
        ).lastrowid

    try:
        creator = nb_creator(author["name"])
        # 1. Fetch. The database is not held open while waiting on the network.
        async with httpx.AsyncClient(timeout=30, headers=headers()) as client:
            works = group_by_title(await fetch_norwegian_editions(client, creator))
            if not works:
                raise RuntimeError(
                    f"Fant ingen norske bøker for «{creator}» hos Nasjonalbiblioteket. "
                    "Sjekk stavemåten, eller skriv navnet som «Etternavn, Fornavn»."
                )

            with db() as con:
                existing = {
                    row["work_key"]: dict(row)
                    for row in con.execute("SELECT * FROM books WHERE author_id=?", (author_id,))
                }

            # 2. First publish year and covers, a few books at a time.
            limit = asyncio.Semaphore(COVER_DOWNLOADS_AT_ONCE)

            async def complete(work):
                async with limit:
                    ol_year, ol_cover = await open_library_lookup(client, work["title"], author["name"])
                    if ol_year and (work["year"] is None or ol_year < work["year"]):
                        work["year"] = ol_year
                    current = existing.get(work["work_key"], {}).get("cover_file")
                    if current:
                        return current
                    filename = cover_filename(work["work_key"])
                    for url in (work["cover_url"], ol_cover):
                        if url and (saved := await download_image(client, url, filename)):
                            return saved
                    return None

            covers = await asyncio.gather(*(complete(w) for w in works))

        # 3. Write everything in one short transaction.
        inserted = 0
        updated = 0
        with db() as con:
            for work, cover_file in zip(works, covers):
                current = existing.get(work["work_key"])
                if current:
                    # Titles/years the user has edited by hand are left alone.
                    if not current["manual_lock"]:
                        con.execute(
                            "UPDATE books SET title=?,publish_year=?,cover_file=COALESCE(?,cover_file) WHERE id=?",
                            (work["title"], work["year"], cover_file, current["id"]),
                        )
                    elif cover_file:
                        con.execute("UPDATE books SET cover_file=COALESCE(cover_file,?) WHERE id=?", (cover_file, current["id"]))
                    updated += 1
                else:
                    con.execute(
                        "INSERT INTO books(author_id,work_key,title,publish_year,cover_file) VALUES(?,?,?,?,?)",
                        (author_id, work["work_key"], work["title"], work["year"], cover_file),
                    )
                    inserted += 1
            # Books that are no longer in the result (e.g. fetched earlier from
            # Open Library in all languages) are removed, unless they carry
            # something of the user's own.
            current_keys = {w["work_key"] for w in works}
            removed = 0
            for key, book in existing.items():
                untouched = (
                    not book["read"] and not book["tags"] and book["series_name"] == NO_SERIES
                    and book["series_order"] is None and not book["hidden"] and not book["manual_lock"]
                )
                if key not in current_keys and untouched:
                    con.execute("DELETE FROM books WHERE id=?", (book["id"],))
                    removed += 1
            finished = now()
            con.execute("UPDATE authors SET openlibrary_key=?,last_sync=? WHERE id=?", (f"nb:{creator}", finished, author_id))
            message = f"{inserted} nye, {updated} oppdatert"
            if removed:
                message += f", {removed} fjernet"
            con.execute(
                "UPDATE sync_log SET finished=?,status='ok',message=? WHERE id=?",
                (finished, message, log_id),
            )
        return {"status": "ok", "inserted": inserted, "updated": updated, "removed": removed, "works": len(works)}
    except Exception as exc:
        with db() as con:
            con.execute(
                "UPDATE sync_log SET finished=?,status='error',message=? WHERE id=?",
                (now(), str(exc) or exc.__class__.__name__, log_id),
            )
        raise

async def sync_all():
    """Sync every author, then back up the database. Runs one at a time."""
    if sync_all_state["running"]:
        return
    sync_all_state["running"] = True
    try:
        with db() as con:
            ids = [row["id"] for row in con.execute("SELECT id FROM authors ORDER BY id")]
        for author_id in ids:
            try:
                await sync_author(author_id)
            except Exception:
                pass  # Already recorded in the sync log.
        backup_db()
    finally:
        sync_all_state["running"] = False

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
    title: Optional[str] = None
    publish_year: Optional[int] = None

# Fields that Open Library also fills in. Editing one by hand stops the sync
# from overwriting it; reading status, tags, series and hiding never do.
SYNCED_FIELDS = {"title", "publish_year"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.getenv("BOKHYLLA_DISABLE_SCHEDULER") != "1":
        day = os.getenv("SYNC_DAY", "sun")
        hour = int(os.getenv("SYNC_HOUR", "3"))
        scheduler.add_job(sync_all, "cron", day_of_week=day, hour=hour, minute=0, id="weekly-sync", replace_existing=True)
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)

app = FastAPI(title="Bokhylla", version="0.2.0", lifespan=lifespan)

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
def get_books(author_id: int, include_hidden: bool = False):
    with db() as con:
        rows = con.execute(f"""
          SELECT * FROM books WHERE author_id=? {"" if include_hidden else "AND hidden=0"}
          ORDER BY series_name COLLATE NOCASE, COALESCE(series_order,999999), COALESCE(publish_year,9999), title COLLATE NOCASE
        """, (author_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["read"] = bool(item["read"])
        item["hidden"] = bool(item["hidden"])
        item["tags"] = [x for x in item["tags"].split(",") if x]
        item["cover_url"] = f"/covers/{item['cover_file']}" if item["cover_file"] else None
        result.append(item)
    return result

@app.get("/api/series")
def get_series():
    """Series names in use, for suggestions when placing a book in a series."""
    with db() as con:
        rows = con.execute(
            "SELECT DISTINCT series_name FROM books WHERE series_name<>? ORDER BY series_name COLLATE NOCASE",
            (NO_SERIES,),
        ).fetchall()
    return [row["series_name"] for row in rows]

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
    """Starts syncing every author in the background; poll /api/sync-status."""
    if sync_all_state["running"]:
        return {"status": "running"}
    app.state.sync_task = asyncio.create_task(sync_all())
    # sync_all marks itself as running right away, before its first await.
    await asyncio.sleep(0)
    return {"status": "started"}

@app.get("/api/sync-status")
def get_sync_status():
    return {"running": sync_all_state["running"]}

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
        if key == "series_name":
            value = (value or "").strip() or NO_SERIES
        if key == "title":
            value = (value or "").strip()
            if not value:
                raise HTTPException(400, "Tittel kan ikke være tom")
        fields.append(f"{key}=?")
        values.append(value)
    if not fields:
        return {"status": "unchanged"}
    if SYNCED_FIELDS & data.keys():
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
