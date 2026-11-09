import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main

JPEG = b"\xff\xd8\xff" + b"0" * 2000

def fake_open_library(titles):
    """Answers like Open Library: one author, the given works, and cover images."""
    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/search/authors.json":
            return httpx.Response(200, json={"docs": [{"key": "OL1A", "name": "Jo Nesbø"}]})
        if path.endswith("/works.json"):
            entries = [
                {"key": f"/works/OL{i}W", "title": t, "first_publish_date": "2001", "covers": [100 + i]}
                for i, t in enumerate(titles)
            ]
            return httpx.Response(200, json={"entries": entries})
        if request.url.host == "covers.openlibrary.org":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    return handler

@pytest.fixture
def client(monkeypatch):
    if main.DB.exists():
        main.DB.unlink()
    for f in main.COVERS.glob("*"):
        f.unlink()
    titles = {"value": ["Flaggermusmannen", "Kakerlakkene"]}
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_open_library(titles["value"]))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", patched_client)
    with TestClient(main.app) as c:
        c.titles = titles
        yield c

def nesbo(client):
    return next(a for a in client.get("/api/authors").json() if a["name"] == "Jo Nesbø")["id"]

def test_sync_adds_books_with_covers(client):
    result = client.post(f"/api/authors/{nesbo(client)}/sync").json()
    assert result["inserted"] == 2
    books = client.get(f"/api/authors/{nesbo(client)}/books").json()
    assert [b["title"] for b in books] == ["Flaggermusmannen", "Kakerlakkene"]
    assert all(b["cover_url"] for b in books)
    assert client.get(books[0]["cover_url"]).status_code == 200

def test_reading_status_does_not_stop_updates(client):
    aid = nesbo(client)
    client.post(f"/api/authors/{aid}/sync")
    book = client.get(f"/api/authors/{aid}/books").json()[0]
    client.patch(f"/api/books/{book['id']}", json={"read": True, "tags": ["A"]})
    client.titles["value"] = ["Flaggermusmannen (ny utgave)", "Kakerlakkene"]
    client.post(f"/api/authors/{aid}/sync")
    titles = [b["title"] for b in client.get(f"/api/authors/{aid}/books").json()]
    assert "Flaggermusmannen (ny utgave)" in titles
    updated = next(b for b in client.get(f"/api/authors/{aid}/books").json() if b["id"] == book["id"])
    assert updated["read"] is True and updated["tags"] == ["A"]

def test_edited_title_is_kept_on_sync(client):
    aid = nesbo(client)
    client.post(f"/api/authors/{aid}/sync")
    book = client.get(f"/api/authors/{aid}/books").json()[0]
    client.patch(f"/api/books/{book['id']}", json={"title": "Min tittel"})
    client.titles["value"] = ["Noe annet", "Kakerlakkene"]
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
    assert client.post("/api/sync-all").json()["status"] == "started"
    for _ in range(100):
        if not client.get("/api/sync-status").json()["running"]:
            break
        time.sleep(0.05)
    assert not client.get("/api/sync-status").json()["running"]
    log = client.get("/api/sync-log?limit=50").json()
    assert len(log) == len(main.AUTHORS) and all(l["status"] == "ok" for l in log)
    assert list(main.BACKUPS.glob("library-*.db"))
