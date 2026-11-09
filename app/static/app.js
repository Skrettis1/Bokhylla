let authors = [];
let selected = null;
let books = [];
let editing = null;

const $ = (s) => document.querySelector(s);
const NO_SERIES = "Ikke plassert i serie";

async function api(url, options) {
  const r = await fetch(url, options);
  if (!r.ok) throw new Error((await r.json().catch(() => ({ detail: r.statusText }))).detail);
  return r.json();
}

const jsonBody = (method, data) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

function escapeHtml(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setNotice(t) {
  $("#notice").textContent = t;
}

// --- Authors ---

async function loadAuthors() {
  authors = await api("/api/authors");
  if (selected && !authors.some((a) => a.id === selected)) selected = null;
  renderAuthors();
  const total = authors.reduce((n, a) => n + a.book_count, 0);
  const read = authors.reduce((n, a) => n + a.read_count, 0);
  $("#stats").textContent = `${read} av ${total} lest`;
  if (!selected && authors.length) await selectAuthor(authors[0].id);
  else if (!authors.length) {
    books = [];
    renderBooks();
    $("#authorName").textContent = "Ingen forfattere";
  }
}

function renderAuthors() {
  $("#authors").innerHTML = authors.length
    ? authors
        .map(
          (a) => `<div class="authorRow"><button class="author ${selected === a.id ? "active" : ""}" onclick="selectAuthor(${a.id})"><b>${escapeHtml(a.name)}</b><small>${a.read_count} av ${a.book_count} lest</small></button><button class="removeAuthor" title="Fjern forfatter" onclick="event.stopPropagation();removeAuthor(${a.id})">×</button></div>`,
        )
        .join("")
    : '<div class="empty">Ingen forfattere ennå.</div>';
}

async function selectAuthor(id) {
  selected = id;
  const a = authors.find((x) => x.id === id);
  $("#authorName").textContent = a.name;
  await loadBooks();
  renderAuthors();
}

async function addAuthor(event) {
  event.preventDefault();
  const input = $("#newAuthorName");
  const name = input.value.trim();
  if (!name) return;
  try {
    const a = await api("/api/authors", jsonBody("POST", { name }));
    input.value = "";
    await loadAuthors();
    await selectAuthor(a.id);
  } catch (e) {
    setNotice("Feil: " + e.message);
  }
}

async function removeAuthor(id) {
  const a = authors.find((x) => x.id === id);
  if (!a || !confirm(`Fjerne "${a.name}" og alle bøkene?`)) return;
  try {
    await api(`/api/authors/${id}`, { method: "DELETE" });
    await loadAuthors();
  } catch (e) {
    setNotice("Feil: " + e.message);
  }
}

// --- Books ---

async function loadBooks() {
  if (!selected) return;
  const hidden = $("#showHidden").checked ? "?include_hidden=true" : "";
  books = await api(`/api/authors/${selected}/books${hidden}`);
  renderBooks();
}

function renderBooks() {
  const q = $("#search").value.toLowerCase();
  const visible = books.filter((b) => b.title.toLowerCase().includes(q));
  $("#books").innerHTML = visible.length
    ? visible
        .map((b, i) => {
          const series = b.series_name !== NO_SERIES ? `${escapeHtml(b.series_name)}${b.series_order != null ? " #" + b.series_order : ""}` : escapeHtml(b.series_name);
          return `<article class="book${b.hidden ? " isHidden" : ""}"><img class="cover" src="${b.cover_url || ""}" onerror="this.remove()" alt=""><div class="bookInfo"><div class="meta">#${i + 1}${b.publish_year ? " · " + b.publish_year : ""} · ${series}${b.hidden ? " · skjult" : ""}</div><h3>${escapeHtml(b.title)}</h3><div class="tags">${["A", "H", "T"].map((t) => `<button class="tag ${b.tags.includes(t) ? "on" : ""}" data-tag="${t}" onclick="toggleTag(${b.id},'${t}')">${t}</button>`).join("")}<button class="edit" title="Rediger tittel, serie og nummer" onclick="openEdit(${b.id})">✎</button></div></div><button class="read ${b.read ? "on" : ""}" onclick="toggleRead(${b.id})">✓</button></article>`;
        })
        .join("")
    : '<div class="empty">Ingen bøker. Trykk «Hent fra Open Library».</div>';
}

async function patch(id, data) {
  await api(`/api/books/${id}`, jsonBody("PATCH", data));
}

async function toggleRead(id) {
  const b = books.find((x) => x.id === id);
  b.read = !b.read;
  renderBooks();
  await patch(id, { read: b.read });
  await loadAuthors();
}

async function toggleTag(id, t) {
  const b = books.find((x) => x.id === id);
  b.tags = b.tags.includes(t) ? b.tags.filter((x) => x !== t) : [...b.tags, t];
  renderBooks();
  await patch(id, { tags: b.tags });
}

// --- Edit dialog (title, year, series) ---

async function openEdit(id) {
  editing = books.find((x) => x.id === id);
  if (!editing) return;
  $("#editTitle").value = editing.title;
  $("#editYear").value = editing.publish_year ?? "";
  $("#editSeries").value = editing.series_name === NO_SERIES ? "" : editing.series_name;
  $("#editOrder").value = editing.series_order ?? "";
  $("#editHide").textContent = editing.hidden ? "Vis boka igjen" : "Skjul bok";
  try {
    const series = await api("/api/series");
    $("#seriesList").innerHTML = series.map((s) => `<option value="${escapeHtml(s)}">`).join("");
  } catch {}
  $("#editDialog").showModal();
}

async function saveEdit(event) {
  event.preventDefault();
  if (!editing) return;
  const data = {
    series_name: $("#editSeries").value.trim(),
    series_order: $("#editOrder").value === "" ? null : Number($("#editOrder").value),
  };
  const title = $("#editTitle").value.trim();
  const year = $("#editYear").value === "" ? null : Number($("#editYear").value);
  // Only send title/year when changed, since that stops the sync from updating them.
  if (title !== editing.title) data.title = title;
  if (year !== (editing.publish_year ?? null)) data.publish_year = year;
  try {
    await patch(editing.id, data);
    $("#editDialog").close();
    await loadBooks();
  } catch (e) {
    setNotice("Feil: " + e.message);
  }
}

async function toggleHidden() {
  if (!editing) return;
  try {
    await patch(editing.id, { hidden: !editing.hidden });
    $("#editDialog").close();
    await loadBooks();
    await loadAuthors();
  } catch (e) {
    setNotice("Feil: " + e.message);
  }
}

// --- Sync ---

async function syncAuthor() {
  if (!selected) return;
  setNotice("Henter bøker og omslag ...");
  try {
    const r = await api(`/api/authors/${selected}/sync`, { method: "POST" });
    setNotice(`${r.works} verk behandlet. ${r.inserted} nye og ${r.updated} oppdatert.`);
    await loadAuthors();
    await loadBooks();
  } catch (e) {
    setNotice("Feil: " + e.message);
  }
  await loadSyncLog();
}

async function syncAll() {
  const button = $("#syncAll");
  try {
    await api("/api/sync-all", { method: "POST" });
  } catch (e) {
    setNotice("Feil: " + e.message);
    return;
  }
  button.disabled = true;
  setNotice("Oppdaterer alle forfattere i bakgrunnen ...");
  // Runs in the background on the server; follow it through the sync log.
  while (true) {
    await new Promise((r) => setTimeout(r, 2000));
    await loadSyncLog();
    try {
      const status = await api("/api/sync-status");
      if (!status.running) break;
    } catch {
      break;
    }
  }
  button.disabled = false;
  setNotice("Alle forfattere er behandlet.");
  await loadAuthors();
  await loadBooks();
}

const STATUS_LABEL = { ok: "OK", error: "Feil", running: "Pågår" };

async function loadSyncLog() {
  try {
    const log = await api("/api/sync-log?limit=15");
    $("#syncLog").innerHTML = log.length
      ? log
          .map(
            (l) => `<div class="logEntry"><b>${escapeHtml(l.author_name || "Ukjent")}</b><span class="logStatus ${l.status}">${STATUS_LABEL[l.status] || l.status}</span> · ${(l.started || "").replace("T", " ")}${l.message ? " · " + escapeHtml(l.message) : ""}</div>`,
          )
          .join("")
      : '<div class="empty">Ingen synk ennå.</div>';
  } catch {}
}

$("#search").addEventListener("input", renderBooks);
$("#showHidden").addEventListener("change", loadBooks);
$("#syncAuthor").addEventListener("click", syncAuthor);
$("#syncAll").addEventListener("click", syncAll);
$("#addAuthor").addEventListener("submit", addAuthor);
$("#editForm").addEventListener("submit", saveEdit);
$("#editCancel").addEventListener("click", () => $("#editDialog").close());
$("#editHide").addEventListener("click", toggleHidden);

loadAuthors().catch((e) => setNotice("Kunne ikke starte appen: " + e.message));
loadSyncLog();
fetch("/api/sync-status")
  .then((r) => r.json())
  .then((s) => {
    if (s.running) syncAll();
  })
  .catch(() => {});
