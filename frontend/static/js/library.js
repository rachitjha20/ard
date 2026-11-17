import { api, finishJob, toast, showError, confirmDialog, formDialog, openMenu, importFiles, enableDrop, plural } from "./api.js";
import { documentCard } from "./cards.js";
import { libraryOverview, changeCategory, editTags, startOcr } from "./library-actions.js";
import { h } from "./api.js";

const grid = document.getElementById("library-grid");
const empty = document.getElementById("library-empty");
const search = document.getElementById("library-search");
const sort = document.getElementById("library-sort");
const fileInput = document.getElementById("library-file");
const bar = document.getElementById("select-bar");

const category = document.getElementById("library-category");
const tagSelect = document.getElementById("library-tag");
const state = { view: "all", selected: new Map(), documents: [], hits: new Map(), groups: null };
const monthFormat = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
let pending = null;

async function load() {
  pending?.abort();
  pending = new AbortController();
  const signal = pending.signal;
  const query = search.value.trim();
  state.hits = new Map();
  state.groups = null;
  let total;
  if (state.view === "duplicates") {
    const result = await api("/api/library/duplicates", { signal });
    state.groups = result.groups;
    state.documents = result.groups.flatMap((group) => group.documents);
    total = result.groups.length;
  } else if (query && state.view !== "trash") {
    // Searching looks inside documents as well as at their names, across the whole library.
    const result = await api(`/api/search?${new URLSearchParams({ q: query })}`, { signal });
    let rows = result.results;
    if (category.value) rows = rows.filter((row) => row.document.category === category.value);
    if (tagSelect.value) rows = rows.filter((row) => row.document.tags.includes(tagSelect.value));
    state.documents = rows.map((row) => row.document);
    for (const row of rows) state.hits.set(row.document.id, { ...row, query });
    total = rows.length;
  } else {
    const params = new URLSearchParams({ q: query, view: state.view, sort: sort.value, limit: "500", category: category.value, tag: tagSelect.value });
    const result = await api(`/api/documents?${params}`, { signal });
    state.documents = result.documents;
    total = result.total;
  }
  state.selected.clear();
  render(total);
  refreshOverview().catch(() => {});
}

function render(total) {
  const trash = state.view === "trash";
  const card = (doc) => documentCard(doc, {
    selectable: !trash, onSelect: toggleSelect, onFavorite: toggleFavorite, onMenu: showMenu, hit: state.hits.get(doc.id) || null,
  });
  const nodes = [];
  if (state.groups) {
    for (const group of state.groups) {
      nodes.push(h("h3", { class: "group-head" }, group.label + (group.similarity ? ` (about ${group.similarity}% alike)` : ""),
        h("span", {}, `${group.documents.length} documents`)));
      nodes.push(...group.documents.map(card));
    }
  } else if (!state.hits.size && sort.value === "imported" && !search.value.trim() && !trash) {
    // Timeline: grouped by the month each document was imported into ARD (not the document's own date).
    let current = "";
    for (const doc of state.documents) {
      const month = monthFormat.format(new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(doc.dates.imported_at) ? doc.dates.imported_at : `${doc.dates.imported_at}Z`));
      if (month !== current) { current = month; nodes.push(h("h3", { class: "timeline-head" }, month, h("span", {}, "imported"))); }
      nodes.push(card(doc));
    }
  } else {
    nodes.push(...state.documents.map(card));
  }
  grid.replaceChildren(...nodes);
  const count = document.getElementById("library-count");
  if (state.groups) count.textContent = total ? `${plural(total, "group")} of possible duplicates to review. ARD never deletes or merges documents on its own.` : "No duplicate candidates found.";
  else if (state.hits.size || (search.value.trim() && !trash)) count.textContent = `${plural(total, "document")} matching “${search.value.trim()}” in names or text.`;
  else count.textContent = trash ? `${plural(total, "document")} in trash. Files are kept until you decide otherwise.` : plural(total, "document");
  empty.hidden = state.documents.length > 0;
  if (!empty.hidden) {
    const filtered = search.value || state.view !== "all" || category.value || tagSelect.value;
    document.getElementById("library-empty-title").textContent = filtered ? "No documents match" : "Nothing here yet";
    document.getElementById("library-empty-text").textContent = trash ? "The trash is empty."
      : filtered ? "Try a different search or filter." : "Drop PDF files on this page, or use Import PDFs.";
  }
  updateBar();
}

function toggleSelect(doc, checked) {
  if (checked) state.selected.set(doc.id, doc); else state.selected.delete(doc.id);
  updateBar();
}

function updateBar() {
  const count = state.selected.size;
  bar.hidden = count === 0;
  document.getElementById("select-count").textContent = `${count} selected`;
  document.getElementById("select-merge").disabled = count < 2;
}

async function toggleFavorite(doc) {
  try {
    await api(`/api/documents/${doc.id}`, { method: "PATCH", json: { favorite: !doc.favorite } });
    await load();
  } catch (error) { showError(error); }
}

async function rename(doc) {
  const values = await formDialog({
    title: "Rename document", submitLabel: "Rename",
    fields: [{ name: "name", label: "Name", value: doc.display_name, required: true }],
    validate: (v) => (v.name.trim() ? null : "Give the document a name."),
  });
  if (!values) return;
  try {
    await api(`/api/documents/${doc.id}`, { method: "PATCH", json: { display_name: values.name } });
    await load();
  } catch (error) { showError(error); }
}

async function trash(docs) {
  const one = docs.length === 1;
  const ok = await confirmDialog({
    title: one ? "Move to trash?" : `Move ${docs.length} documents to trash?`,
    message: "The files stay on disk and can be restored from the Trash view at any time.",
    confirmLabel: "Move to trash", danger: true,
  });
  if (!ok) return;
  try {
    for (const doc of docs) await api(`/api/documents/${doc.id}`, { method: "DELETE", confirm: true });
    toast(one ? "Moved to trash." : `Moved ${docs.length} documents to trash.`, "success");
    await load();
  } catch (error) { showError(error); await load(); }
}

async function restore(doc) {
  try {
    await api(`/api/documents/${doc.id}/restore`, { method: "POST" });
    toast("Restored.", "success");
    await load();
  } catch (error) { showError(error); }
}

async function merge() {
  // Merge order is the order shown on screen, which the user can see.
  const docs = state.documents.filter((doc) => state.selected.has(doc.id));
  const values = await formDialog({
    title: `Merge ${docs.length} documents`,
    intro: `They are joined in the order shown: ${docs.map((d) => d.display_name).join(" → ")}. The originals are kept.`,
    submitLabel: "Merge",
    fields: [{ name: "name", label: "Name of the merged document", value: `${docs[0].display_name} (merged)`, required: true }],
  });
  if (!values) return;
  try {
    const result = await finishJob(await api("/api/documents/merge", {
      method: "POST", json: { document_ids: docs.map((d) => d.id), name: values.name },
    }));
    toast("Merged into a new document.", "success");
    location.href = `/workspace?doc=${result.documents[0].id}`;
  } catch (error) { showError(error); }
}

function showMenu(doc, anchor) {
  if (doc.trashed) {
    openMenu(anchor, [{ label: "Restore", icon: "restore", onSelect: () => restore(doc) }]);
    return;
  }
  openMenu(anchor, [
    { label: "Open", icon: "workspace", onSelect: () => { location.href = `/workspace?doc=${doc.id}`; } },
    { label: "Rename", icon: "info", onSelect: () => rename(doc) },
    { label: `Category: ${doc.category_label}…`, icon: "library", onSelect: () => changeCategory(doc).then((updated) => updated && load()).catch(showError) },
    { label: "Tags…", icon: "list", onSelect: () => editTags(doc).then((updated) => updated && load()).catch(showError) },
    doc.ocr_status === "needed" || doc.ocr_status === "partial"
      ? { label: "Recognise text (OCR)…", icon: "search", onSelect: () => startOcr(doc, () => load().catch(() => {})).catch(showError) } : null,
    { label: "Download", icon: "download", onSelect: () => { location.href = `/api/documents/${doc.id}/file?download=true`; } },
    "separator",
    { label: "Move to trash", icon: "trash", danger: true, onSelect: () => trash([doc]) },
  ]);
}

async function onFiles(files) {
  try { await importFiles(files); await load(); } catch (error) { showError(error); }
}

let overviewTimer = null;
async function refreshOverview() {
  const overview = await libraryOverview(true);
  const keep = category.value;
  category.replaceChildren(h("option", { value: "" }, "All categories"),
    ...overview.categories.filter((c) => c.count || c.id === keep).map((c) => h("option", { value: c.id }, `${c.label} (${c.count})`)));
  category.value = keep;
  const keepTag = tagSelect.value;
  document.getElementById("library-tag-wrap").hidden = overview.tags.length === 0;
  tagSelect.replaceChildren(h("option", { value: "" }, "Any tag"), ...overview.tags.map((t) => h("option", { value: t.name }, `${t.name} (${t.count})`)));
  tagSelect.value = overview.tags.some((t) => t.name === keepTag) ? keepTag : "";
  const counts = Object.fromEntries(overview.albums.map((album) => [album.id, album.count]));
  for (const chip of document.querySelectorAll("#library-views .chip")) {
    const base = chip.dataset.label || (chip.dataset.label = chip.textContent);
    chip.textContent = counts[chip.dataset.view] ? `${base} ${counts[chip.dataset.view]}` : base;
  }
  const indexing = document.getElementById("library-indexing");
  indexing.hidden = !overview.indexing;
  indexing.textContent = overview.indexing ? `Reading text from ${plural(overview.indexing, "document")} for search and categories…` : "";
  // While indexing is still running, look again shortly so new categories and search results appear.
  clearTimeout(overviewTimer);
  if (overview.indexing) overviewTimer = setTimeout(() => load().catch(() => {}), 2500);
}

let timer = null;
search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load().catch(showError), 200); });
sort.addEventListener("change", () => load().catch(showError));
category.addEventListener("change", () => load().catch(showError));
tagSelect.addEventListener("change", () => load().catch(showError));
document.getElementById("library-views").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  state.view = chip.dataset.view;
  for (const other of chip.parentElement.children) other.setAttribute("aria-pressed", String(other === chip));
  load().catch(showError);
});
document.getElementById("library-import").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { onFiles(fileInput.files); fileInput.value = ""; });
document.getElementById("select-merge").addEventListener("click", merge);
document.getElementById("select-trash").addEventListener("click", () => trash([...state.selected.values()]));
document.getElementById("select-open").addEventListener("click", () => {
  location.href = `/workspace?doc=${[...state.selected.keys()].join(",")}`;
});
document.getElementById("select-clear").addEventListener("click", () => {
  state.selected.clear();
  grid.querySelectorAll(".doc-check").forEach((box) => { box.checked = false; });
  updateBar();
});
enableDrop(document.getElementById("library-drop"), onFiles);
load().catch(showError);
