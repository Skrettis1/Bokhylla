import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main

JPEG = b"\xff\xd8\xff" + b"0" * 2000

def fake_sources(titles):
    """Answers like the National Library (Norwegian editions) and Open Library (covers).

    Each title becomes two editions (bokmål) so they must be merged; the first
    edition has a cover in the National Library, the others only in Open Library.
    """
    def handler(request: httpx.Request):
        url = request.url
        if url.host == "api.nb.no":
            filters = url.params.get_list("filter")
            if 'creator:"Nesbø, Jo"' not in filters or "languages:nob" not in filters:
                return httpx.Response(200, json={"_embedded": {"items": []}, "page": {"totalPages": 0}})
            items = []
            for i, t in enumerate(titles):
                for year in (2005 + i, 2001 + i):
                    links = {"thumbnail_custom": {"href": f"https://www.nb.no/img/{i}/full/{{width}},{{height}}/0/native.jpg"}} if i == 0 else {}
                    items.append({"metadata": {"title": t, "originInfo": {"issued": str(year)}, "languages": [{"code": "nob"}]}, "_links": links})
            items.append({"metadata": {"title": "Samlebind ; To bøker", "originInfo": {"issued": "2010"}, "languages": [{"code": "nob"}]}, "_links": {}})
            # A translation, catalogued with the original language too.
            items.append({"metadata": {"title": "Pansarhjärta", "originInfo": {"issued": "2016"}, "languages": [{"code": "nob"}, {"code": "swe"}]}, "_links": {}})
            return httpx.Response(200, json={"_embedded": {"items": items}, "page": {"totalPages": 1}})
        if url.host == "www.nb.no" or url.host == "covers.openlibrary.org":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        if url.path == "/search.json":
            title = url.params.get("title")
            year = 1997 if title == "Flaggermusmannen" else 2099
            return httpx.Response(200, json={"docs": [{"title": title, "cover_i": 7, "first_publish_year": year}]})
        return httpx.Response(404)
    return handler

@pytest.fixture
def client(monkeypatch):
    if main.DB.exists():
        main.DB.unlink()
    for f in main.COVERS.glob("*"):
        f.unlink()
    titles = {"value": ["Flaggermusmannen : kriminalroman", "Kakerlakkene"]}
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_sources(titles["value"]))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", patched_client)
    with TestClient(main.app) as c:
        c.titles = titles
        yield c

def nesbo(client):
    return next(a for a in client.get("/api/authors").json() if a["name"] == "Jo Nesbø")["id"]

def test_sync_adds_norwegian_books_once_each_with_covers(client):
    result = client.post(f"/api/authors/{nesbo(client)}/sync").json()
    # Two editions per title are merged; the collection volume and the
    # translation (Swedish/Norwegian) are skipped.
    assert result["inserted"] == 2
    books = client.get(f"/api/authors/{nesbo(client)}/books").json()
    assert [b["title"] for b in books] == ["Flaggermusmannen", "Kakerlakkene"]
    # Earliest year: Open Library's first publication (1997) beats the National
    # Library's earliest edition; otherwise the earliest Norwegian edition.
    assert [b["publish_year"] for b in books] == [1997, 2002]
    assert all(b["cover_url"] for b in books)  # NB cover, and Open Library as fallback
    assert client.get(books[0]["cover_url"]).status_code == 200

def test_old_open_library_books_are_cleaned_up(client):
    aid = nesbo(client)
    with main.db() as con:
        con.execute("INSERT INTO books(author_id,work_key,title) VALUES(?,?,?)", (aid, "/works/OL1W", "Snowman (English)"))
        con.execute("INSERT INTO books(author_id,work_key,title,read) VALUES(?,?,?,1)", (aid, "/works/OL2W", "Lest bok"))
    result = client.post(f"/api/authors/{aid}/sync").json()
    assert result["removed"] == 1
    titles = [b["title"] for b in client.get(f"/api/authors/{aid}/books").json()]
    assert "Snowman (English)" not in titles and "Lest bok" in titles

def test_author_without_norwegian_books_gives_clear_error(client):
    other = next(a for a in client.get("/api/authors").json() if a["name"] == "Dan Brown")["id"]
    response = client.post(f"/api/authors/{other}/sync")
    assert response.status_code == 502
    assert "Fant ingen norske bøker" in response.json()["detail"]

def test_reading_status_does_not_stop_updates(client):
    aid = nesbo(client)
    client.post(f"/api/authors/{aid}/sync")
    book = client.get(f"/api/authors/{aid}/books").json()[0]
    client.patch(f"/api/books/{book['id']}", json={"read": True, "tags": ["A"]})
    client.titles["value"] = ["Flaggermusmannen", "Kakerlakkene", "Rødstrupe"]
    client.post(f"/api/authors/{aid}/sync")
    titles = [b["title"] for b in client.get(f"/api/authors/{aid}/books").json()]
    assert "Rødstrupe" in titles
    updated = next(b for b in client.get(f"/api/authors/{aid}/books").json() if b["id"] == book["id"])
    assert updated["read"] is True and updated["tags"] == ["A"]

def test_edited_title_is_kept_on_sync(client):
    aid = nesbo(client)
    client.post(f"/api/authors/{aid}/sync")
    book = client.get(f"/api/authors/{aid}/books").json()[0]
    client.patch(f"/api/books/{book['id']}", json={"title": "Min tittel"})
    client.titles["value"] = ["Flaggermusmannen : ny utgave", "Kakerlakkene"]
    client.post(f"/api/authors/{aid}/sync")
    again = next(b for b in client.get(f"/api/authors/{aid}/books").json() if b["id"] == book["id"])
    assert again["title"] == "Min tittel"

def test_series_and_hidden_books(client):
    aid = nesbo(client)
    client.post(f"/api/authors/{aid}/sync")
    first, second = client.get(f"/api/authors/{aid}/books").json()
    client.patch(f"/api/books/{first['id']}", json={"series_name": "Harry Hole", "series_order": 1})
    client.patch(f"/api/books/{second['id']}", json={"hidden": True})
    visible = client.get(f"/api/authors/{aid}/books").json()
    assert [b["series_name"] for b in visible] == ["Harry Hole"]
    everything = client.get(f"/api/authors/{aid}/books?include_hidden=true").json()
    assert len(everything) == 2 and any(b["hidden"] for b in everything)
    assert client.get("/api/series").json() == ["Harry Hole"]
    client.patch(f"/api/books/{first['id']}", json={"series_name": "  "})
    assert client.get(f"/api/authors/{aid}/books").json()[0]["series_name"] == main.NO_SERIES

def test_sync_all_runs_in_background_and_backs_up(client):
    # Only Jo Nesbø has books in the fake National Library; the others report an error.
    assert client.post("/api/sync-all").json()["status"] == "started"
    for _ in range(100):
        if not client.get("/api/sync-status").json()["running"]:
            break
        time.sleep(0.05)
    assert not client.get("/api/sync-status").json()["running"]
    log = client.get("/api/sync-log?limit=50").json()
    assert len(log) == len(main.AUTHORS)
    assert [l["author_name"] for l in log if l["status"] == "ok"] == ["Jo Nesbø"]
    assert list(main.BACKUPS.glob("library-*.db"))
