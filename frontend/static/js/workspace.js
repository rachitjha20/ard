// Workspace controller: document tabs, viewer toolbar, side panels and page operations.
import {
  api, finishJob, h, icon, toast, showError, confirmDialog, formDialog, openMenu, importFiles, enableDrop,
  formatBytes, formatDate, plural, ApiError,
} from "./api.js";
import { PdfViewer } from "./viewer.js";
import { Annotator } from "./annotate.js";
import { changeCategory, editTags, startOcr, categoryReason } from "./library-actions.js";

const $ = (id) => document.getElementById(id);
const root = $("ws");
const viewer = new PdfViewer($("viewer"));
const TABS_KEY = "ard.workspace.tabs";
const annotator = new Annotator(viewer, { fileInput: $("ann-image-file") });

const state = {
  tabs: [],            // [{id, name}]
  activeId: null,
  doc: null,           // full document record of the active tab
  selected: new Set(), // selected page numbers
  anchor: null,        // last clicked page, for shift-selection
  busy: false,
};

// ---------------------------------------------------------------- tabs
function saveTabs() {
  try { localStorage.setItem(TABS_KEY, JSON.stringify({ tabs: state.tabs, active: state.activeId })); } catch { /* optional */ }
}

function loadTabs() {
  try {
    const saved = JSON.parse(localStorage.getItem(TABS_KEY) || "{}");
    if (Array.isArray(saved.tabs)) state.tabs = saved.tabs.filter((tab) => tab && typeof tab.id === "string");
    state.activeId = saved.active || null;
  } catch { /* start with no tabs */ }
}

function renderTabs() {
  $("ws-tabs").replaceChildren(...state.tabs.map((tab) => h("div", { class: "ws-tab", dataset: { active: String(tab.id === state.activeId) } },
    h("button", { type: "button", class: "ws-tab-main", role: "tab", "aria-selected": String(tab.id === state.activeId), title: tab.name,
      onclick: () => activate(tab.id) }, tab.name),
    h("button", { type: "button", class: "ws-tab-close", "aria-label": `Close ${tab.name}`, onclick: () => closeTab(tab.id) }, icon("close")))));
  const none = state.tabs.length === 0;
  $("ws-empty").hidden = !none;
  $("ws-main").hidden = none;
}

async function closeTab(id) {
  if (id === state.activeId && !(await leaveAnnotate())) return;
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index === -1) return;
  state.tabs.splice(index, 1);
  if (state.activeId === id) {
    const next = state.tabs[Math.min(index, state.tabs.length - 1)];
    state.activeId = null;
    if (next) { await activate(next.id); return; }
    state.doc = null;
    await viewer.close();
    history.replaceState(null, "", "/workspace");
  }
  saveTabs();
  renderTabs();
}

async function activate(id, { page = 1 } = {}) {
  if (id !== state.activeId && !(await leaveAnnotate())) return;
  state.activeId = id;
  state.selected.clear();
  state.anchor = null;
  renderTabs();
  saveTabs();
  history.replaceState(null, "", `/workspace?doc=${id}`);
  try {
    await loadDocument(page);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      toast("That document is no longer in the library.", "error");
      await closeTab(id);
    } else showError(error);
  }
}

// ---------------------------------------------------------------- document
function fileUrl(doc) {
  return `/api/documents/${doc.id}/file?version=${doc.current_version_id}`;
}

async function askPassword(retry) {
  const values = await formDialog({
    title: "Password required",
    intro: retry ? "That password did not work. Try again." : "This PDF is password-protected. The password is used only in this browser tab to display it and is not stored.",
    submitLabel: "Open",
    fields: [{ name: "password", label: "Password", type: "password", required: true }],
  });
  return values ? values.password : null;
}

async function loadDocument(page = 1) {
  const id = state.activeId;
  const doc = await api(`/api/documents/${id}`);
  if (id !== state.activeId) return;
  state.doc = doc;
  const tab = state.tabs.find((item) => item.id === id);
  if (tab && tab.name !== doc.display_name) { tab.name = doc.display_name; saveTabs(); renderTabs(); }
  document.title = `${doc.display_name} · ARD`;
  $("tb-total").textContent = `/ ${doc.page_count ?? "?"}`;
  renderThumbs();
  renderVersions();
  renderInfo();
  updateScope();
  $("outline").replaceChildren(h("p", { class: "ws-hint" }, "Loading…"));
  try {
    await viewer.open(fileUrl(doc), { askPassword, page });
  } catch (error) {
    if (id !== state.activeId) return;
    const text = error && error.name === "PasswordException" ? "A password is needed to display this document."
      : error && error.name === "InvalidPDFException" ? "This file is damaged and cannot be displayed."
      : "This document could not be displayed.";
    $("viewer").replaceChildren(h("div", { class: "empty", style: "margin-top:40px;background:var(--surface)" }, icon("alert"), h("h2", {}, text),
      error && error.name === "PasswordException" ? h("button", { class: "btn", type: "button", onclick: () => loadDocument(page) }, "Enter password") : null));
    $("outline").replaceChildren(h("p", { class: "ws-hint" }, "Not available."));
    return;
  }
  if (id !== state.activeId) return;
  renderOutline();
  if ($("tb-search").value.trim()) runSearch();
}

// ---------------------------------------------------------------- thumbnails and selection
function renderThumbs() {
  const doc = state.doc;
  const list = $("thumbs");
  if (!doc.page_count) {
    list.replaceChildren(h("li", { class: "ws-hint" }, "Page previews are not available for password-protected documents."));
    return;
  }
  const items = [];
  for (let number = 1; number <= doc.page_count; number += 1) {
    const button = h("button", {
      type: "button", class: "thumb", draggable: "true", dataset: { page: String(number) },
      "aria-label": `Page ${number}`, "aria-selected": String(state.selected.has(number)),
      "aria-current": String(number === viewer.currentPage),
    },
      h("img", { src: `/api/documents/${doc.id}/thumbnail?page=${number}&version=${doc.current_version_id}`, alt: "", loading: "lazy", decoding: "async" }),
      h("span", { class: "num" }, String(number)));
    items.push(h("li", {}, button));
  }
  list.replaceChildren(...items);
}

function thumbButton(page) { return $("thumbs").querySelector(`.thumb[data-page="${page}"]`); }

function refreshSelection() {
  for (const button of $("thumbs").querySelectorAll(".thumb")) {
    button.setAttribute("aria-selected", String(state.selected.has(Number(button.dataset.page))));
  }
  updateScope();
}

/** Pages an operation applies to: the selection, or the page being viewed. */
function targetPages() {
  return state.selected.size ? [...state.selected].sort((a, b) => a - b) : [viewer.currentPage];
}

function describe(pages) {
  if (pages.length === 1) return `page ${pages[0]}`;
  const contiguous = pages.every((page, index) => index === 0 || page === pages[index - 1] + 1);
  return contiguous ? `pages ${pages[0]}–${pages.at(-1)}` : `${pages.length} pages`;
}

function updateScope() {
  const pages = targetPages();
  const label = describe(pages);
  $("ed-scope").textContent = (state.selected.size ? "Selected: " : "Current: ") + label;
}

$("thumbs").addEventListener("click", (event) => {
  const button = event.target.closest(".thumb");
  if (!button) return;
  const page = Number(button.dataset.page);
  if (event.shiftKey && state.anchor) {
    const [from, to] = [Math.min(state.anchor, page), Math.max(state.anchor, page)];
    for (let number = from; number <= to; number += 1) state.selected.add(number);
  } else if (event.ctrlKey || event.metaKey) {
    if (state.selected.has(page)) state.selected.delete(page); else state.selected.add(page);
    state.anchor = page;
  } else {
    state.selected.clear();
    state.anchor = page;
    viewer.goToPage(page, false);
  }
  refreshSelection();
});

// Drag a thumbnail (or the whole selection) to a new position.
let dragPages = null;
$("thumbs").addEventListener("dragstart", (event) => {
  const button = event.target.closest(".thumb");
  if (!button || state.busy) { event.preventDefault(); return; }
  const page = Number(button.dataset.page);
  dragPages = state.selected.has(page) ? [...state.selected].sort((a, b) => a - b) : [page];
  for (const number of dragPages) thumbButton(number)?.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", dragPages.join(","));
});
function clearDropMarks() {
  for (const element of $("thumbs").querySelectorAll(".drop-before, .drop-after")) element.classList.remove("drop-before", "drop-after");
}
function dropPosition(event) {
  const button = event.target.closest(".thumb");
  if (!button) return null;
  const box = button.getBoundingClientRect();
  return { button, page: Number(button.dataset.page), before: event.clientX < box.left + box.width / 2 };
}
$("thumbs").addEventListener("dragover", (event) => {
  if (!dragPages) return;
  const position = dropPosition(event);
  if (!position) return;
  event.preventDefault();
  clearDropMarks();
  position.button.classList.add(position.before ? "drop-before" : "drop-after");
});
$("thumbs").addEventListener("dragend", () => {
  clearDropMarks();
  for (const element of $("thumbs").querySelectorAll(".dragging")) element.classList.remove("dragging");
  dragPages = null;
});
$("thumbs").addEventListener("drop", (event) => {
  if (!dragPages) return;
  const position = dropPosition(event);
  if (!position) return;
  event.preventDefault();
  const moving = dragPages;
  const rest = [];
  for (let number = 1; number <= state.doc.page_count; number += 1) if (!moving.includes(number)) rest.push(number);
  let index = rest.indexOf(position.page);
  if (index === -1) {   // dropped on one of the dragged pages: nothing to do
    return;
  }
  if (!position.before) index += 1;
  const order = [...rest.slice(0, index), ...moving, ...rest.slice(index)];
  if (order.every((page, i) => page === i + 1)) return;
  operate("reorder", { order }, { goTo: index + 1, done: `Moved ${describe(moving)}.` });
});

// ---------------------------------------------------------------- side panels
for (const tab of document.querySelectorAll(".ws-side .tab")) {
  tab.addEventListener("click", () => showPanel(tab.dataset.panel));
}
function showPanel(name) {
  for (const tab of document.querySelectorAll(".ws-side .tab")) tab.setAttribute("aria-selected", String(tab.dataset.panel === name));
  for (const panel of document.querySelectorAll(".ws-panel")) panel.hidden = panel.dataset.panel !== name;
  if (document.querySelector(".ws-body").dataset.panel === "closed") togglePanel();
}
function togglePanel() {
  const body = document.querySelector(".ws-body");
  const closed = body.dataset.panel === "closed";
  body.dataset.panel = closed ? "open" : "closed";
  $("tb-panel").setAttribute("aria-pressed", String(closed));
}
$("tb-panel").addEventListener("click", togglePanel);

async function renderOutline() {
  const host = $("outline");
  let items = [];
  try { items = await viewer.outline(); } catch { /* shown as empty */ }
  if (!items.length) { host.replaceChildren(h("p", { class: "ws-hint" }, "This document has no bookmarks.")); return; }
  const build = (nodes) => h("ul", { class: "outline-list" }, nodes.map((node) => h("li", {},
    h("button", { type: "button", disabled: !node.page, onclick: () => viewer.goToPage(node.page) }, node.title),
    node.children.length ? build(node.children) : null)));
  host.replaceChildren(build(items));
}

function renderVersions() {
  const doc = state.doc;
  const versions = [...doc.versions].reverse();
  $("versions").replaceChildren(...versions.map((version) => h("li", { class: "version", dataset: { current: String(version.is_current) } },
    h("div", { class: "row" },
      h("strong", {}, `Version ${version.number}`),
      version.is_current ? h("span", { class: "badge badge-accent" }, "Current") : null,
      version.is_original ? h("span", { class: "badge" }, "Original") : null),
    h("span", {}, version.report?.summary || (version.is_original ? originLabel(doc.origin) : version.label)),
    h("span", { class: "meta num" }, `${version.page_count ? plural(version.page_count, "page") : "Pages unknown"} · ${formatBytes(version.size_bytes)}`),
    h("span", { class: "meta" }, `Created in ARD ${formatDate(version.created_at, true)}`),
    h("div", { class: "row", style: "margin-top:4px" },
      h("a", { class: "btn btn-sm", href: `/api/documents/${doc.id}/file?version=${version.id}&download=true` }, "Download"),
      version.is_current ? null : h("button", { type: "button", class: "btn btn-sm", onclick: () => revert(version) }, "Make current")))));
}

function originLabel(origin) {
  return { import: "Imported file, unchanged", merge: "Created by merging", split: "Created by splitting", extract: "Created by extracting pages" }[origin] || "Created";
}

function renderInfo() {
  const doc = state.doc;
  const inspection = doc.inspection || {};
  const metadata = inspection.metadata || {};
  const kv = (rows) => h("dl", { class: "kv" }, rows.filter((row) => row[1] !== null && row[1] !== undefined && row[1] !== "")
    .flatMap(([label, value]) => [h("dt", {}, label), h("dd", {}, value)]));
  const flags = [];
  if (doc.is_encrypted) flags.push(h("span", { class: "badge badge-warn" }, "Password-protected"));
  if (inspection.has_javascript) flags.push(h("span", { class: "badge badge-warn", title: "ARD never runs scripts embedded in a PDF." }, "Contains scripts (never run)"));
  if (inspection.has_open_action) flags.push(h("span", { class: "badge badge-warn", title: "ARD never performs actions embedded in a PDF." }, "Has automatic actions (ignored)"));
  if (inspection.attachment_count) flags.push(h("span", { class: "badge badge-warn" }, `${plural(inspection.attachment_count, "attachment")} (not opened)`));
  if (inspection.has_forms) flags.push(h("span", { class: "badge" }, "Form fields"));
  if (doc.has_signatures) flags.push(h("span", { class: "badge badge-accent", title: "Use Check signatures below to see whether it verifies." }, "Digitally signed in this version"));
  else if (inspection.has_signature_fields) flags.push(h("span", { class: "badge", title: "A signature field says nothing about whether a signature is valid. Use Check signatures below." }, "Has signature fields"));

  const integrity = h("div", { class: "info-section" }, h("h3", {}, "Integrity"),
    h("p", { class: "ws-hint", style: "margin:0" }, "Compares the stored files with the fingerprints recorded when ARD wrote them."),
    h("button", { type: "button", class: "btn btn-sm", style: "justify-self:start", onclick: (event) => checkIntegrity(event.currentTarget.parentElement) }, "Check now"));

  $("info").replaceChildren(
    h("div", { class: "info-section" }, h("h3", {}, "Document"), kv([
      ["Name", doc.display_name],
      ["Pages", doc.page_count],
      ["Size (current version)", formatBytes(doc.size_bytes)],
      ["Versions", doc.version_count],
      ["PDF version", inspection.pdf_version],
    ]), flags.length ? h("div", { class: "row", style: "gap:4px" }, flags) : null),
    h("div", { class: "info-section" }, h("h3", {}, "Category"),
      h("span", { class: `badge ${doc.category === "uncategorized" ? "" : "badge-ok"}`, style: "justify-self:start" }, doc.category_label),
      h("p", { class: "ws-hint", style: "margin:0" }, categoryReason(doc)),
      doc.tags.length ? h("div", { class: "row", style: "gap:4px" }, doc.tags.map((tag) => h("span", { class: "badge badge-accent" }, `#${tag}`))) : null,
      h("div", { class: "row", style: "gap:6px" },
        h("button", { type: "button", class: "btn btn-sm", onclick: () => changeCategory(doc).then((updated) => updated && loadDocument(viewer.currentPage)).catch(showError) }, "Change"),
        h("button", { type: "button", class: "btn btn-sm", onclick: () => editTags(doc).then((updated) => updated && loadDocument(viewer.currentPage)).catch(showError) }, "Tags"))),
    h("div", { class: "info-section" }, h("h3", {}, "Searchable text"),
      h("span", { class: `badge ${doc.ocr_status === "needed" || doc.ocr_status === "partial" ? "badge-warn" : ""}`, style: "justify-self:start" },
        { needed: "Scanned: needs OCR", partial: "Some pages still need OCR", done: "Text recognised by OCR", not_needed: "Text came with the PDF", locked: "Password-protected", unknown: "Not analysed yet" }[doc.ocr_status] || doc.ocr_status),
      h("div", { class: "row", style: "gap:6px" },
        h("button", { type: "button", class: "btn btn-sm", onclick: (event) => showPageConfidence(event.currentTarget.closest(".info-section")) }, "Page details"),
        doc.ocr_status === "needed" || doc.ocr_status === "partial" ? h("button", { type: "button", class: "btn btn-sm", onclick: () => recogniseText().catch(showError) }, "Recognise text") : null)),
    // Four different dates, each named for what it is.
    h("div", { class: "info-section" }, h("h3", {}, "Dates"), kv([
      ["Imported into ARD", formatDate(doc.dates.imported_at, true)],
      ["Last edited in ARD", doc.dates.edited_at ? formatDate(doc.dates.edited_at, true) : "Never"],
      ["Created (from PDF metadata)", doc.dates.pdf_created_at ? formatDate(doc.dates.pdf_created_at, true) : "Not recorded"],
      ["Modified (from PDF metadata)", doc.dates.pdf_modified_at ? formatDate(doc.dates.pdf_modified_at, true) : "Not recorded"],
    ]), h("p", { class: "ws-hint", style: "margin:0" }, "Metadata dates come from the file and can be wrong or edited.")),
    h("div", { class: "info-section" }, h("h3", {}, "Metadata in the original file"),
      Object.keys(metadata).length
        ? kv(Object.entries(metadata).filter(([key]) => !["CreationDate", "ModDate"].includes(key)))
        : h("p", { class: "ws-hint", style: "margin:0" }, "None.")),
    integrity,
    h("div", { class: "info-section" }, h("h3", {}, "Digital signatures"),
      h("p", { class: "ws-hint", style: "margin:0" }, "Checks cryptographic signatures in this version, offline. A drawn signature picture is not one."),
      h("button", { type: "button", class: "btn btn-sm", style: "justify-self:start", onclick: (event) => checkSignatures(event.currentTarget.parentElement) }, "Check signatures")),
    h("div", { class: "info-section" }, h("h3", {}, "PDF/A (archiving)"),
      h("button", { type: "button", class: "btn btn-sm", style: "justify-self:start", onclick: (event) => checkPdfA(event.currentTarget.parentElement) }, "Check PDF/A")),
    h("div", { class: "info-section" }, h("h3", {}, "Fingerprint of the imported file"),
      h("code", { style: "overflow-wrap:anywhere;font-size:11.5px" }, `SHA-256 ${doc.sha256}`)),
  );
}

async function checkIntegrity(host) {
  try {
    const result = await api(`/api/documents/${state.doc.id}/integrity`);
    const good = result.checks.every((check) => check.state === "match");
    host.querySelector(".integrity-result")?.remove();
    host.append(h("div", { class: "integrity-result", style: "display:grid;gap:6px" },
      h("span", { class: `badge ${good ? "badge-ok" : "badge-bad"}`, style: "justify-self:start" }, result.status),
      h("span", { class: "ws-hint", style: "margin:0" }, result.note)));
  } catch (error) { showError(error); }
}

// ---------------------------------------------------------------- operations
function setBusy(busy, text = "Working…") {
  state.busy = busy;
  root.dataset.busy = String(busy);
  $("ed-busy").hidden = !busy;
  $("ed-busy-text").textContent = text;
}

/** Run an operation that creates a new version of the open document. */
function blockedByAnnotate() {
  if (root.dataset.mode !== "annotate") return false;
  toast("Save or close annotate mode first.", "error");
  return true;
}

/** Editing a signed document produces a version whose signatures no longer verify. Say so first. */
async function confirmIfSigned() {
  if (!state.doc || !state.doc.has_signatures) return true;
  return confirmDialog({
    title: "This version is digitally signed",
    message: "Changing it creates a new version in which the signature no longer verifies. The signed version is kept and can be downloaded from Versions.",
    confirmLabel: "Change anyway", danger: true,
  });
}

async function operate(operation, params, { goTo, done } = {}) {
  if (state.busy || !state.doc || blockedByAnnotate()) return;
  if (!(await confirmIfSigned())) return;
  const doc = state.doc;
  setBusy(true);
  try {
    const result = await finishJob(await api(`/api/documents/${doc.id}/operations`, { method: "POST", json: { operation, params } }),
      () => setBusy(true, "Still working… you can keep reading."));
    if (state.activeId !== doc.id) return;
    state.selected.clear();
    await loadDocument(Math.max(1, Math.min(goTo ?? viewer.currentPage, result.page_count)));
    if (done !== false) toast(done || `${result.summary}. Saved as version ${result.version_number}.`, "success");
    return result;
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

function requireEditable() {
  if (state.doc && (state.doc.is_encrypted || !state.doc.page_count)) {
    toast("This PDF is password-protected. Use More → Remove password protection to edit it.", "error", 7000);
    return false;
  }
  return Boolean(state.doc);
}

async function deletePages() {
  const pages = targetPages();
  if (pages.length >= state.doc.page_count) { toast("A document needs at least one page.", "error"); return; }
  const ok = await confirmDialog({
    title: `Delete ${describe(pages)}?`,
    message: "This creates a new version without those pages. Earlier versions keep them, and you can go back at any time.",
    confirmLabel: "Delete", danger: true,
  });
  if (ok) operate("delete_pages", { pages }, { goTo: pages[0] });
}

async function extractPages() {
  const pages = targetPages();
  setBusy(true);
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/split`, { method: "POST", json: { mode: "pages", pages } }));
    const created = result.documents[0];
    toast(`Extracted ${describe(pages)} into a new document. This one is unchanged.`, "success");
    openDocuments([created.id], created.id);
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function splitDocument() {
  const total = state.doc.page_count;
  const values = await formDialog({
    title: "Split into separate documents",
    intro: `This document has ${plural(total, "page")}. Each part becomes a new document in the library; this one is unchanged.`,
    submitLabel: "Split",
    fields: [
      { name: "mode", label: "How to split", type: "radio", value: "ranges", options: [
        { value: "ranges", label: "By page ranges", hint: "One document per range, for example 1-3, 4-6" },
        { value: "every", label: "Every N pages", hint: "Equal parts of N pages each" },
      ] },
      { name: "ranges", label: "Page ranges", placeholder: "1-3, 4-6", hint: "Used with “By page ranges”." },
      { name: "n", label: "Pages per part", type: "number", min: 1, max: total, value: 1, hint: "Used with “Every N pages”." },
    ],
    validate: (v) => {
      if (v.mode === "ranges" && !String(v.ranges || "").trim()) return "Give at least one page range.";
      if (v.mode === "every" && !(v.n >= 1)) return "Give the number of pages per part.";
      if (v.mode === "every" && Math.ceil(total / v.n) > 500) return "That would create more than 500 documents.";
      return null;
    },
  });
  if (!values) return;
  setBusy(true);
  try {
    const body = values.mode === "ranges" ? { mode: "ranges", ranges: values.ranges } : { mode: "every", n: values.n };
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/split`, { method: "POST", json: body }));
    const item = toast(`${result.summary}. `, "success", 9000);
    item.querySelector("span").append(h("a", { href: "/library", style: "color:inherit" }, "Show in library"));
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function insertBlank() {
  const total = state.doc.page_count;
  const values = await formDialog({
    title: "Insert blank pages", submitLabel: "Insert",
    fields: [
      { name: "after", label: "Insert after page", type: "number", min: 0, max: total, value: viewer.currentPage, hint: "Use 0 to insert at the very beginning." },
      { name: "count", label: "How many", type: "number", min: 1, max: 500, value: 1 },
    ],
    validate: (v) => (!(v.after >= 0 && v.after <= total) ? `Choose a page between 0 and ${total}.` : !(v.count >= 1 && v.count <= 500) ? "Insert between 1 and 500 pages." : null),
  });
  if (values) operate("insert_blank", values, { goTo: values.after + 1 });
}

async function compress() {
  const values = await formDialog({
    title: "Compress",
    intro: `Current size: ${formatBytes(state.doc.size_bytes)}. The result is saved as a new version; nothing is overwritten.`,
    submitLabel: "Compress",
    fields: [{ name: "preset", label: "Preset", type: "radio", value: "balanced", options: [
      { value: "quality", label: "Maximum quality", hint: "Lossless. Reorganises the file only; images are untouched." },
      { value: "balanced", label: "Balanced", hint: "Re-encodes large photos. Slight, usually invisible quality loss." },
      { value: "smaller", label: "Smaller file", hint: "Stronger image compression. Visible quality loss is possible." },
    ] }],
  });
  if (!values) return;
  const result = await operate("compress", values, { done: false });
  if (!result) return;
  const report = result.report;
  if (report.saved_bytes > 0) {
    toast(`Compressed from ${formatBytes(report.size_before)} to ${formatBytes(report.size_after)} (${report.saved_percent}% smaller). Saved as version ${result.version_number}.`, "success", 9000);
  } else {
    toast(`This file could not be made smaller with that preset (${formatBytes(report.size_before)} → ${formatBytes(report.size_after)}). The result was still saved as version ${result.version_number}; use Versions to go back.`, "info", 10000);
  }
}

async function crop() {
  const pages = targetPages();
  const values = await formDialog({
    title: `Crop ${describe(pages)}`,
    intro: "Margins are in points (72 points = 1 inch). Cropping hides the outer area; the hidden content stays in the file. It is not redaction.",
    submitLabel: "Crop",
    fields: ["top", "bottom", "left", "right"].map((side) => ({ name: side, label: `${side[0].toUpperCase()}${side.slice(1)} margin`, type: "number", min: 0, value: 0 })),
    validate: (v) => (["top", "bottom", "left", "right"].some((side) => v[side] > 0) ? null : "Give at least one margin."),
  });
  if (values) operate("crop", { pages, ...values });
}

async function insertFromDocument() {
  const list = await api("/api/documents?limit=500&sort=name");
  const others = list.documents.filter((doc) => doc.id !== state.doc.id && doc.page_count && !doc.is_encrypted);
  if (!others.length) { toast("There is no other document in the library to take pages from.", "error"); return; }
  const total = state.doc.page_count;
  const values = await formDialog({
    title: "Insert pages from another document", submitLabel: "Insert",
    fields: [
      { name: "from", label: "Take pages from", type: "select", value: others[0].id, options: others.map((doc) => ({ value: doc.id, label: `${doc.display_name} (${plural(doc.page_count, "page")})` })) },
      { name: "pages", label: "Which pages", placeholder: "All pages", hint: "For example 1-3, 7. Leave empty for all pages." },
      { name: "after", label: "Insert after page", type: "number", min: 0, max: total, value: viewer.currentPage, hint: "Use 0 to insert at the very beginning." },
    ],
    validate: (v) => (!(v.after >= 0 && v.after <= total) ? `Choose a page between 0 and ${total}.` : null),
  });
  if (!values) return;
  const params = { from_document_id: values.from, after: values.after };
  if (String(values.pages || "").trim()) params.pages = values.pages.trim();
  operate("insert_pages", params, { goTo: values.after + 1 });
}

async function stripMetadata() {
  const ok = await confirmDialog({
    title: "Remove metadata?",
    message: "Creates a new version without title, author, dates and other document-level metadata. Metadata inside embedded images or attachments is not touched, and earlier versions still contain it.",
    confirmLabel: "Remove metadata",
  });
  if (ok) operate("strip_metadata", {});
}

async function revert(version) {
  if (blockedByAnnotate()) return;
  const ok = await confirmDialog({
    title: `Make version ${version.number} current?`,
    message: "A copy of that version becomes the newest version. Nothing in the history is removed.",
    confirmLabel: "Make current",
  });
  if (!ok) return;
  setBusy(true);
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/versions/${version.id}/revert`, { method: "POST" }));
    await loadDocument(1);
    toast(`${result.summary}. Saved as version ${result.version_number}.`, "success");
  } catch (error) { showError(error); } finally { setBusy(false); }
}

const ACTIONS = {
  "rotate-left": () => operate("rotate", { pages: targetPages(), degrees: 270 }),
  "rotate-right": () => operate("rotate", { pages: targetPages(), degrees: 90 }),
  duplicate: () => operate("duplicate_pages", { pages: targetPages() }),
  extract: extractPages,
  delete: deletePages,
  "insert-blank": insertBlank,
  split: splitDocument,
  compress,
};
document.querySelector(".ws-editbar").addEventListener("click", (event) => {
  const button = event.target.closest("[data-op]");
  if (!button || state.busy || !requireEditable()) return;
  ACTIONS[button.dataset.op]();
});
$("ed-more").addEventListener("click", (event) => {
  if (state.busy || !state.doc) return;
  if (state.doc.is_encrypted) {
    openMenu(event.currentTarget, [{ label: "Remove password protection…", icon: "lock", onSelect: unlockDocument }]);
    return;
  }
  if (!requireEditable()) return;
  openMenu(event.currentTarget, [
    { label: "Watermark…", icon: "blank", onSelect: addWatermark },
    { label: "Headers, footers and page numbers…", icon: "list", onSelect: addHeaderFooter },
    { label: "Password-protect…", icon: "lock", onSelect: protectDocument },
    { label: "Sign with a certificate…", icon: "trust", onSelect: signDocument },
    { label: "Recognise text (OCR)…", icon: "search", onSelect: () => recogniseText().catch(showError) },
    "separator",
    { label: "Fill in form…", icon: "check", onSelect: () => fillForm().catch(showError) },
    { label: "Import form values…", icon: "upload", onSelect: importFormValues },
    { label: "Export form values (JSON)", icon: "download", onSelect: () => { location.href = `/api/documents/${state.doc.id}/form/values?format=json`; } },
    { label: "Export form values (CSV)", icon: "download", onSelect: () => { location.href = `/api/documents/${state.doc.id}/form/values?format=csv`; } },
    "separator",
    { label: "Reverse page order", icon: "history", onSelect: () => operate("reverse", {}) },
    { label: "Crop pages…", icon: "fit", onSelect: crop },
    { label: "Insert pages from another document…", icon: "merge", onSelect: () => insertFromDocument().catch(showError) },
    "separator",
    { label: "Remove metadata…", icon: "info", onSelect: stripMetadata },
    { label: "Remove annotations…", icon: "trash", onSelect: removeAnnotations },
    { label: "Select all pages", icon: "check", onSelect: () => { for (let n = 1; n <= state.doc.page_count; n += 1) state.selected.add(n); refreshSelection(); } },
    { label: "Clear selection", icon: "close", disabled: !state.selected.size, onSelect: () => { state.selected.clear(); refreshSelection(); } },
  ]);
});

// ---------------------------------------------------------------- annotate mode
const DRAW_HINTS = {
  select: "Click an item you added to select it, drag to move it, Delete to remove it.",
  text: "Click where the text should start.",
  image: "Click where the image's top-left corner should go.",
  stamp: "Click where the stamp should be centred.",
  highlight: "Select text on the page to highlight it.",
  note: "Click where the note should go.",
  rect: "Drag to draw a rectangle.", ellipse: "Drag to draw an ellipse.",
  line: "Drag to draw a line.", arrow: "Drag from the tail to the point of the arrow.",
  ink: "Hold and drag to draw freehand.",
  signature: "Click where the signature should go.",
  redact: "Drag over what should be removed.",
};

function setAnnotateMode(on, submode = "annotate") {
  root.dataset.mode = on ? "annotate" : "view";
  root.dataset.submode = submode;
  $("tb-annotate").setAttribute("aria-pressed", String(on && submode === "annotate"));
  $("tb-redact").setAttribute("aria-pressed", String(on && submode === "redact"));
  $("ann-save").textContent = submode === "redact" ? "Redact and verify…" : "Save as new version";
  annotator.setActive(on);
  if (on && submode === "redact") annotator.setTool("redact");
}

/** Leave annotate mode. Asks first if there are unsaved items. Resolves false if the user stays. */
async function leaveAnnotate() {
  if (root.dataset.mode !== "annotate") return true;
  if (annotator.count) {
    const ok = await confirmDialog({
      title: "Discard what you added?",
      message: `${plural(annotator.count, root.dataset.submode === "redact" ? "marked area" : "item")} not saved yet. The document itself has not been changed.`,
      confirmLabel: "Discard", danger: true,
    });
    if (!ok) return false;
  }
  setAnnotateMode(false);
  return true;
}

async function saveAnnotations() {
  if (state.busy || !annotator.count) return;
  const doc = state.doc;
  const page = viewer.currentPage;
  setBusy(true, "Saving…");
  try {
    const result = await finishJob(await api(`/api/documents/${doc.id}/annotations`, { method: "POST", json: annotator.payload() }));
    setAnnotateMode(false);
    await loadDocument(page);
    const report = result.report;
    const parts = [];
    if (report.overlays) parts.push(`${plural(report.overlays, "item")} drawn onto the page`);
    if (report.annotations) parts.push(`${plural(report.annotations, "annotation")} added`);
    toast(`${parts.join(" and ")}. Saved as version ${result.version_number}.`, "success", 7000);
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function saveRedactions() {
  if (state.busy || !annotator.count) return;
  const areas = annotator.areas();
  const pages = [...new Set(areas.map((area) => area.page))].sort((a, b) => a - b);
  const ok = await confirmDialog({
    title: `Redact ${plural(areas.length, "area")} on ${plural(pages.length, "page")}?`,
    message: [
      `Page${pages.length === 1 ? "" : "s"} ${pages.join(", ")} will be replaced by a picture with the marked areas blacked out, and any annotations on ${pages.length === 1 ? "it" : "them"} removed. The content under each box is deleted from the new version, not just covered.`,
      "The remaining text on those pages will no longer be selectable or searchable.",
      "This is saved as a new version. ARD keeps the earlier versions, which still contain the original content; share only the redacted file you download.",
    ].join("\n\n"),
    confirmLabel: "Redact", danger: true,
  });
  if (!ok) return;
  const doc = state.doc;
  setBusy(true, "Redacting and verifying…");
  try {
    const result = await finishJob(await api(`/api/documents/${doc.id}/redactions`, { method: "POST", json: { areas } }));
    setAnnotateMode(false);
    await loadDocument(pages[0]);
    await confirmDialog({
      title: `Redaction verified. Saved as version ${result.version_number}`,
      message: ["ARD re-opened the result and confirmed: no text remains on the redacted pages, their annotations are gone, and the marked areas are solid black.",
        "What you still need to know:", ...result.report.warnings.map((warning) => `• ${warning}`)].join("\n\n"),
      confirmLabel: "OK",
    });
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function enterMode(submode) {
  if (root.dataset.mode === "annotate") {
    const same = root.dataset.submode === submode;
    if (!(await leaveAnnotate()) || same) return;
  }
  if (state.busy || !requireEditable() || !(await confirmIfSigned())) return;
  setAnnotateMode(true, submode);
  toast(submode === "redact"
    ? "Redact mode. Drag over what should be removed, then choose Redact and verify."
    : "Annotate mode. Nothing changes in the document until you choose Save as new version.", "info", 6000);
}
$("tb-annotate").addEventListener("click", () => enterMode("annotate"));
$("tb-redact").addEventListener("click", () => enterMode("redact"));
$("annbar").addEventListener("click", (event) => {
  const button = event.target.closest("[data-tool]");
  if (button) annotator.setTool(button.dataset.tool);
});
annotator.addEventListener("tool", (event) => {
  for (const button of $("annbar").querySelectorAll("[data-tool]")) button.setAttribute("aria-pressed", String(button.dataset.tool === event.detail.tool));
  $("ann-pending").title = DRAW_HINTS[event.detail.tool] || "";
  if (root.dataset.mode === "annotate" && event.detail.tool !== "select") toast(DRAW_HINTS[event.detail.tool], "info", 3500);
});
annotator.addEventListener("change", () => {
  const count = annotator.count;
  const redacting = root.dataset.submode === "redact";
  $("ann-pending").textContent = count ? `${plural(count, redacting ? "area" : "item")} ${redacting ? "marked" : "not saved yet"}` : (redacting ? "Nothing marked yet" : "Nothing added yet");
  $("ann-save").disabled = !count;
  $("ann-discard").textContent = count ? "Discard" : "Close";
  $("ann-undo").disabled = !annotator.undoStack.length;
  $("ann-redo").disabled = !annotator.redoStack.length;
  $("ann-delete").disabled = annotator.selectedId === null;
});
$("ann-color").addEventListener("input", (event) => { annotator.color = event.target.value; });
$("ann-width").addEventListener("change", (event) => { annotator.width = Number(event.target.value); });
$("ann-undo").addEventListener("click", () => annotator.undo());
$("ann-redo").addEventListener("click", () => annotator.redo());
$("ann-delete").addEventListener("click", () => annotator.removeSelected());
$("ann-discard").addEventListener("click", () => leaveAnnotate());
$("ann-save").addEventListener("click", () => (root.dataset.submode === "redact" ? saveRedactions() : saveAnnotations()));
window.addEventListener("beforeunload", (event) => { if (annotator.count) event.preventDefault(); });

// Comments on existing annotations: shown as plain text in a small popover.
viewer.addEventListener("annotation", (event) => {
  const { anchor, text, author, subtype } = event.detail;
  document.querySelectorAll(".note-pop").forEach((node) => node.remove());
  const label = { Text: "Note", Highlight: "Highlighted text", Square: "Rectangle", Circle: "Ellipse", Ink: "Drawing" }[subtype] || subtype;
  const pop = h("div", { class: "note-pop", popover: "auto" }, h("strong", {}, author ? `${label} · ${author}` : label), h("p", {}, text));
  document.body.append(pop);
  pop.addEventListener("toggle", (toggle) => { if (toggle.newState === "closed") pop.remove(); });
  pop.showPopover();
  const box = anchor.getBoundingClientRect(), size = pop.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - size.width - 8))}px`;
  pop.style.top = `${box.bottom + 6 + size.height < window.innerHeight ? box.bottom + 6 : Math.max(8, box.top - size.height - 6)}px`;
});

async function addWatermark() {
  const values = await formDialog({
    title: "Add a watermark",
    intro: "Drawn across every page of a new version. A watermark is a visible mark, not a security feature.",
    submitLabel: "Add watermark",
    fields: [
      { name: "text", label: "Text", value: "CONFIDENTIAL", required: true, maxlength: 60, hint: "Latin characters only for now." },
      { name: "angle", label: "Direction", type: "select", value: "45", options: [{ value: "45", label: "Diagonal" }, { value: "0", label: "Horizontal" }] },
      { name: "opacity", label: "Strength", type: "select", value: "0.2", options: [{ value: "0.12", label: "Faint" }, { value: "0.2", label: "Normal" }, { value: "0.4", label: "Strong" }] },
    ],
    validate: (v) => (String(v.text || "").trim() ? null : "Type the watermark text."),
  });
  if (values) operate("watermark", { text: values.text, angle: Number(values.angle), opacity: Number(values.opacity), color: $("ann-color").value });
}

async function addHeaderFooter() {
  const values = await formDialog({
    title: "Headers, footers and page numbers",
    intro: "Use {page} for the page number, {pages} for the total, {date} for today's date and {name} for the document name. Leave a field empty to skip it.",
    submitLabel: "Add",
    fields: [
      { name: "hl", label: "Header, left", value: "" },
      { name: "hr", label: "Header, right", value: "" },
      { name: "fc", label: "Footer, centre", value: "Page {page} of {pages}" },
      { name: "fr", label: "Footer, right", value: "" },
      { name: "first", label: "Start on page", type: "number", min: 1, max: state.doc.page_count, value: 1, hint: "For example 2 to leave a cover page without numbers; numbering then starts at 1 on that page." },
    ],
    validate: (v) => (![v.hl, v.hr, v.fc, v.fr].some((text) => String(text || "").trim()) ? "Fill in at least one field."
      : !(v.first >= 1 && v.first <= state.doc.page_count) ? `Start page must be between 1 and ${state.doc.page_count}.` : null),
  });
  if (!values) return;
  operate("header_footer", { header: { left: values.hl, right: values.hr }, footer: { center: values.fc, right: values.fr }, first_page: values.first, number_from: 1 });
}

async function protectDocument() {
  const values = await formDialog({
    title: "Password-protect",
    intro: "Saves an AES-256 encrypted version. ARD does not store these passwords and cannot recover them. A protected version must be unlocked again before it can be edited here.",
    submitLabel: "Protect",
    fields: [
      { name: "owner", label: "Permissions password", type: "password", required: true, hint: "Needed later to remove the protection. At least 6 characters." },
      { name: "owner2", label: "Repeat permissions password", type: "password", required: true },
      { name: "user", label: "Password to open (optional)", type: "password", hint: "If set, the document cannot be read without it. This is what actually keeps the content private." },
      { name: "restrict", label: "Ask PDF software to", type: "select", value: "none", options: [
        { value: "none", label: "allow everything" }, { value: "copy", label: "block copying text" },
        { value: "print", label: "block printing" }, { value: "all", label: "block copying, printing and changes" }] },
    ],
    validate: (v) => (String(v.owner || "").length < 6 ? "Use a permissions password of at least 6 characters."
      : v.owner !== v.owner2 ? "The two permissions passwords do not match." : null),
  });
  if (!values) return;
  const off = { none: [], copy: ["copy"], print: ["print"], all: ["copy", "print", "modify", "annotate"] }[values.restrict];
  const permissions = Object.fromEntries(["print", "copy", "modify", "annotate", "fill_forms"].map((name) => [name, !off.includes(name)]));
  setBusy(true, "Encrypting…");
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/protect`, {
      method: "POST", json: { owner_password: values.owner, user_password: values.user || null, permissions } }));
    await loadDocument(1);
    toast(`Protected with AES-256. Saved as version ${result.version_number}. Restrictions on copying or printing rely on PDF software honouring them.`, "success", 9000);
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function unlockDocument() {
  const values = await formDialog({
    title: "Remove password protection",
    intro: "Saves an unprotected version so the document can be edited. Enter the document's permissions (owner) password. The protected versions are kept.",
    submitLabel: "Remove protection",
    fields: [{ name: "password", label: "Password", type: "password", required: true }],
  });
  if (!values) return;
  setBusy(true, "Unlocking…");
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/unlock`, { method: "POST", json: { password: values.password } }));
    await loadDocument(1);
    toast(`Protection removed. Saved as version ${result.version_number}.`, "success");
  } catch (error) { showError(error); } finally { setBusy(false); }
}

$("tb-download").addEventListener("click", (event) => {
  if (!state.doc) return;
  const base = `/api/documents/${state.doc.id}`;
  const go = (url) => { location.href = url; };
  const convertible = !state.doc.is_encrypted && state.doc.page_count;
  openMenu(event.currentTarget, [
    { label: "PDF (current version)", icon: "download", onSelect: () => go(`${base}/file?download=true`) },
    "separator",
    { label: "Text only (.txt)", icon: "list", disabled: !convertible, onSelect: () => { toast("Text export keeps the words but not the layout, images or fonts. Scanned pages come out empty.", "info", 7000); go(`${base}/export?format=txt`); } },
    { label: "Page images, PNG (.zip)", icon: "grid", disabled: !convertible, onSelect: () => { toast("Preparing page images…", "info", 4000); go(`${base}/export?format=png`); } },
    { label: "Page images, JPEG (.zip)", icon: "grid", disabled: !convertible, onSelect: () => { toast("Preparing page images…", "info", 4000); go(`${base}/export?format=jpg`); } },
  ]);
});

// ---------------------------------------------------------------- forms
async function fillForm() {
  const form = await api(`/api/documents/${state.doc.id}/form`);
  if (form.xfa) { toast("This is an XFA form, which ARD cannot fill in. Only standard PDF form fields are supported.", "error", 8000); return; }
  if (!form.has_form) { toast("This document has no form fields.", "info"); return; }
  const editable = form.fields.filter((field) => !field.read_only && field.type !== "signature");
  const locked = form.fields.length - editable.length;
  if (!editable.length) { toast("Every field in this form is read-only.", "info"); return; }
  const fields = editable.map((field, index) => {
    const base = { name: `f${index}`, label: (field.label && field.label !== field.name ? `${field.label}` : field.name) + (field.required ? " (required)" : "") };
    if (field.type === "checkbox") return { ...base, type: "select", value: String(field.value), options: [{ value: "false", label: "Not ticked" }, { value: "true", label: "Ticked" }] };
    if (field.type === "radio" || (field.type === "choice" && !field.editable)) {
      const options = field.options.map((option) => ({ value: option, label: option }));
      if (field.type === "choice" || field.can_clear || !field.value) options.unshift({ value: "", label: "(nothing chosen)" });
      return { ...base, type: "select", value: field.value || "", options };
    }
    if (field.type === "multiline") return { ...base, type: "textarea", value: field.value, maxlength: field.max_length || undefined };
    return { ...base, type: field.password ? "password" : "text", value: field.value, maxlength: field.max_length || undefined,
      hint: field.type === "choice" ? `Suggestions: ${field.options.join(", ")}` : undefined };
  });
  const values = await formDialog({
    title: "Fill in form",
    intro: `${plural(editable.length, "field")}.` + (locked ? ` ${plural(locked, "field")} that cannot be changed here (read-only or signature) ${locked === 1 ? "is" : "are"} not shown.` : "") + " Saved as a new version.",
    submitLabel: "Save form", fields,
  });
  if (!values) return;
  const changed = {};
  editable.forEach((field, index) => {
    const raw = values[`f${index}`];
    const value = field.type === "checkbox" ? raw === "true" : (raw ?? "");
    if (value !== field.value && !(value === "" && !field.value)) changed[field.name] = value;
  });
  if (!Object.keys(changed).length) { toast("Nothing was changed.", "info"); return; }
  await saveFormValues(changed);
}

async function saveFormValues(values) {
  if (state.busy || blockedByAnnotate() || !(await confirmIfSigned())) return;
  setBusy(true, "Saving form…");
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/form`, { method: "POST", json: { values } }));
    await loadDocument(viewer.currentPage);
    toast(`${result.summary}. Saved as version ${result.version_number}.` + (result.report.note ? ` ${result.report.note}` : ""), "success", result.report.note ? 10000 : 5000);
  } catch (error) { showError(error); } finally { setBusy(false); }
}

async function importFormValues() {
  const picked = await formDialog({
    title: "Import form values",
    intro: "Choose a JSON file of field names and values, such as one exported from ARD. Values that are already the same are left alone.",
    submitLabel: "Import",
    fields: [{ name: "file", label: "JSON file", type: "file", accept: "application/json,.json", required: true }],
  });
  if (!picked || !picked.file || !picked.file.size) return;
  let values;
  try {
    if (picked.file.size > 5_000_000) throw new Error("too large");
    values = JSON.parse(await picked.file.text());
    if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("shape");
  } catch { toast("That file is not a JSON object of field names and values.", "error"); return; }
  await saveFormValues(values);
}

async function signDocument() {
  const values = await formDialog({
    title: "Sign with a certificate",
    intro: "Adds a cryptographic signature using your certificate file. The file and passphrase are used for this one signature and are not stored. The signature is invisible: it is recorded in the file, not drawn on a page.",
    submitLabel: "Sign",
    fields: [
      { name: "certificate", label: "Certificate file (.p12 or .pfx)", type: "file", accept: ".p12,.pfx,application/x-pkcs12", required: true },
      { name: "passphrase", label: "Certificate passphrase", type: "password" },
      { name: "reason", label: "Reason (optional)", placeholder: "For example: Approved", maxlength: 200 },
      { name: "location", label: "Location (optional)", maxlength: 200 },
    ],
    validate: (v) => (!v.certificate || !v.certificate.size ? "Choose your certificate file." : v.certificate.size > 1_000_000 ? "That file is too large to be a certificate." : null),
  });
  if (!values) return;
  const body = new FormData();
  body.append("certificate", values.certificate, "certificate.p12");
  body.append("passphrase", values.passphrase || "");
  body.append("reason", values.reason || "");
  body.append("location", values.location || "");
  setBusy(true, "Signing…");
  try {
    const result = await finishJob(await api(`/api/documents/${state.doc.id}/sign`, { method: "POST", body }));
    await loadDocument(viewer.currentPage);
    const report = result.report;
    await confirmDialog({
      title: `Signed as ${report.signer || "the certificate holder"}. Saved as version ${result.version_number}`,
      message: [`How this signature will look to others: ${report.status}.`, report.explanation, report.note].filter(Boolean).join("\n\n"),
      confirmLabel: "OK",
    });
  } catch (error) { showError(error); } finally { setBusy(false); }
}

const SIGNATURE_BADGE = { "Signature Valid": "badge-ok", "Signature Invalid": "badge-bad", "Signature Unknown": "badge-warn" };

async function checkSignatures(host) {
  host.querySelector(".check-result")?.remove();
  const box = h("div", { class: "check-result", style: "display:grid;gap:10px" }, h("span", { class: "ws-hint", style: "margin:0" }, "Checking…"));
  host.append(box);
  try {
    const report = await api(`/api/documents/${state.doc.id}/signatures`);
    const rows = report.signatures.map((signature) => h("div", { style: "display:grid;gap:3px;font-size:12.5px" },
      h("span", { class: `badge ${SIGNATURE_BADGE[signature.status] || ""}`, style: "justify-self:start" }, signature.status),
      h("strong", {}, signature.subject || signature.field || "Unnamed signer"),
      signature.issuer ? h("span", { class: "muted" }, signature.self_signed ? "Self-signed certificate" : `Certificate issued by ${signature.issuer}`) : null,
      signature.signing_time_claimed ? h("span", { class: "muted" }, `Signing time ${signature.has_timestamp ? "" : "claimed by signer: "}${formatDate(signature.signing_time_claimed, true)}`) : null,
      h("span", {}, signature.explanation || "")));
    const parts = [...rows];
    if (report.unreadable || report.locked) parts.push(h("span", { class: "badge badge-warn", style: "justify-self:start" }, "Could not be examined"));
    parts.push(h("span", { class: "ws-hint", style: "margin:0" }, report.note || ""));
    box.replaceChildren(...parts);
  } catch (error) { box.remove(); showError(error); }
}

async function checkPdfA(host) {
  host.querySelector(".check-result")?.remove();
  try {
    const report = await api(`/api/documents/${state.doc.id}/pdfa`);
    host.append(h("div", { class: "check-result", style: "display:grid;gap:6px;font-size:12.5px" },
      h("span", { class: `badge ${report.verdict === "Conforms" ? "badge-ok" : report.validated || report.obstacles.length ? "badge-bad" : "badge-warn"}`, style: "justify-self:start" }, report.verdict),
      h("span", {}, report.claim ? `The file claims to be ${report.claim}.` : "The file does not claim to be PDF/A."),
      report.obstacles.length ? h("ul", { style: "margin:0;padding-left:16px;display:grid;gap:3px" }, report.obstacles.map((item) => h("li", {}, item))) : null,
      h("span", { class: "ws-hint", style: "margin:0" }, report.note)));
  } catch (error) { showError(error); }
}

async function recogniseText() {
  if (blockedByAnnotate() || !(await confirmIfSigned())) return;
  const doc = state.doc;
  if (doc.indexed && (doc.ocr_status === "not_needed" || doc.ocr_status === "done")) {
    toast("Every page of this document already has text, so there is nothing to recognise.", "info");
    return;
  }
  await startOcr(doc, () => { if (state.activeId === doc.id) loadDocument(viewer.currentPage).catch(showError); });
}

async function showPageConfidence(host) {
  host.querySelector(".check-result")?.remove();
  try {
    const data = await api(`/api/documents/${state.doc.id}/text`);
    const ocr = data.pages.filter((page) => page.source === "ocr");
    const waiting = data.pages.filter((page) => page.needs_ocr);
    const box = h("div", { class: "check-result", style: "display:grid;gap:6px;font-size:12.5px" });
    if (!data.indexed) box.append(h("span", { class: "ws-hint", style: "margin:0" }, "ARD is still reading this version. Try again in a moment."));
    if (waiting.length) box.append(h("span", {}, `${plural(waiting.length, "page")} look scanned and have no searchable text: ${waiting.slice(0, 20).map((p) => p.page).join(", ")}${waiting.length > 20 ? "…" : ""}.`));
    if (ocr.length) {
      box.append(h("span", { class: "ws-hint", style: "margin:0" }, "Pages read by OCR, with Tesseract's own confidence. Low scores deserve a look; a high score is not a guarantee."),
        h("ol", { class: "conf-list" }, ocr.map((page) => h("li", {}, h("button", { type: "button", onclick: () => viewer.goToPage(page.page, false) },
          h("span", {}, `Page ${page.page}`),
          h("span", { class: `badge ${page.low_confidence ? "badge-warn" : "badge-ok"}` }, page.confidence === null ? "unknown" : `${Math.round(page.confidence)}%${page.low_confidence ? " · check" : ""}`))))));
    }
    if (!ocr.length && !waiting.length && data.indexed) box.append(h("span", {}, "Every page has text that came with the PDF. No OCR was involved."));
    host.append(box);
  } catch (error) { showError(error); }
}

async function removeAnnotations() {
  const values = await formDialog({
    title: "Remove annotations",
    intro: "Creates a new version without the chosen annotations. Links, form fields and attachments are kept. Text, images and stamps drawn onto pages are part of the page and stay; use Versions to go back to before they were added.",
    submitLabel: "Remove",
    fields: [{ name: "scope", label: "Which annotations", type: "radio", value: "ard", options: [
      { value: "ard", label: "Only those added in ARD", hint: "Highlights, notes, shapes and drawings you added here." },
      { value: "all", label: "All annotations", hint: "Also comments and markup made in other software." },
    ] }],
  });
  if (values) operate("remove_annotations", { scope: values.scope });
}

// ---------------------------------------------------------------- viewer toolbar
viewer.addEventListener("page", (event) => {
  const page = event.detail.page;
  if (document.activeElement !== $("tb-page")) $("tb-page").value = page;
  for (const button of $("thumbs").querySelectorAll('.thumb[aria-current="true"]')) button.setAttribute("aria-current", "false");
  const current = thumbButton(page);
  if (current) {
    current.setAttribute("aria-current", "true");
    if (!$("thumbs").closest(".ws-panel").hidden) current.scrollIntoView({ block: "nearest" });
  }
  updateScope();
});
viewer.addEventListener("loaded", (event) => {
  $("tb-page").value = viewer.currentPage;
  $("tb-total").textContent = `/ ${event.detail.pages}`;
  thumbButton(viewer.currentPage)?.setAttribute("aria-current", "true");
  updateScope();
});
viewer.addEventListener("scale", (event) => {
  const select = $("tb-zoom");
  const custom = select.querySelector('option[value="custom"]');
  if (event.detail.mode !== "custom") { select.value = event.detail.mode; custom.hidden = true; return; }
  const match = [...select.options].find((option) => Math.abs(Number(option.value) - event.detail.scale) < 0.005);
  if (match) { select.value = match.value; custom.hidden = true; }
  else { custom.hidden = false; custom.textContent = `${Math.round(event.detail.scale * 100)}%`; select.value = "custom"; }
});

$("tb-prev").addEventListener("click", () => viewer.goToPage(viewer.currentPage - 1, false));
$("tb-next").addEventListener("click", () => viewer.goToPage(viewer.currentPage + 1, false));
$("tb-page").addEventListener("change", () => {
  const page = Number.parseInt($("tb-page").value, 10);
  if (page >= 1 && page <= viewer.pageCount) viewer.goToPage(page, false); else $("tb-page").value = viewer.currentPage;
});
$("tb-page").addEventListener("keydown", (event) => { if (event.key === "Enter") event.target.blur(); });
$("tb-zoom").addEventListener("change", (event) => {
  const value = event.target.value;
  if (value === "fit-width" || value === "fit-page") viewer.setZoom(value); else if (value !== "custom") viewer.setZoom("custom", Number(value));
});
$("tb-zoom-in").addEventListener("click", () => viewer.zoomBy(1));
$("tb-zoom-out").addEventListener("click", () => viewer.zoomBy(-1));
$("tb-view-rotate").addEventListener("click", () => { if (viewer.pdf) viewer.rotateView(90); });
$("tb-print").addEventListener("click", () => {
  // Printing goes through the browser's own PDF handling so every page prints, not just the ones on screen.
  if (state.doc) window.open(fileUrl(state.doc), "_blank", "noopener");
});

// ---------------------------------------------------------------- search
let searchAbort = null;
let searchTimer = null;
async function runSearch() {
  searchAbort?.abort();
  searchAbort = new AbortController();
  const query = $("tb-search").value.trim();
  const status = $("search-status");
  const list = $("search-results");
  if (!query) { viewer.clearSearch(); list.replaceChildren(); status.textContent = "Type in the Find box above."; return; }
  if (!viewer.pdf) return;
  showPanel("search");
  status.textContent = "Searching…";
  const signal = searchAbort.signal;
  const results = await viewer.search(query, { signal, onProgress: (done, total) => { if (!signal.aborted) status.textContent = `Searching… page ${done} of ${total}`; } });
  if (signal.aborted) return;
  const matches = results.reduce((sum, result) => sum + result.count, 0);
  status.textContent = results.length
    ? `${plural(matches, "match")} on ${plural(results.length, "page")}.`
    : "No matches. Scanned pages have no searchable text until OCR is available.";
  list.replaceChildren(...results.map((result) => h("li", {}, h("button", { type: "button", onclick: () => viewer.goToPage(result.page, false) },
    h("strong", {}, `Page ${result.page}${result.count > 1 ? ` · ${result.count} matches` : ""}`),
    h("span", {}, result.before, h("mark", {}, result.match), result.after)))));
}
$("tb-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch().catch(showError), 300); });
$("tb-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { clearTimeout(searchTimer); runSearch().catch(showError); } });

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]")) return;
  const typing = event.target.matches("input, select, textarea");
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f" && state.doc) {
    event.preventDefault(); $("tb-search").focus(); $("tb-search").select(); return;
  }
  if (!typing && root.dataset.mode === "annotate") {
    const key = event.key.toLowerCase();
    if ((event.ctrlKey || event.metaKey) && key === "z") { event.preventDefault(); if (event.shiftKey) annotator.redo(); else annotator.undo(); return; }
    if ((event.ctrlKey || event.metaKey) && key === "y") { event.preventDefault(); annotator.redo(); return; }
    if ((event.key === "Delete" || event.key === "Backspace") && annotator.selectedId !== null) { event.preventDefault(); annotator.removeSelected(); return; }
    if (event.key === "Escape") { annotator.setTool("select"); return; }
  }
  if (typing || !viewer.pdf || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === "ArrowRight" || event.key === "PageDown") { event.preventDefault(); viewer.goToPage(viewer.currentPage + 1, false); }
  else if (event.key === "ArrowLeft" || event.key === "PageUp") { event.preventDefault(); viewer.goToPage(viewer.currentPage - 1, false); }
  else if (event.key === "Home") { event.preventDefault(); viewer.goToPage(1, false); }
  else if (event.key === "End") { event.preventDefault(); viewer.goToPage(viewer.pageCount, false); }
  else if (event.key === "+" || event.key === "=") viewer.zoomBy(1);
  else if (event.key === "-") viewer.zoomBy(-1);
  else if (event.key === "Escape" && state.selected.size) { state.selected.clear(); refreshSelection(); }
});

// ---------------------------------------------------------------- opening documents
async function openDocuments(ids, activeId) {
  for (const id of ids) if (!state.tabs.some((tab) => tab.id === id)) state.tabs.push({ id, name: "Loading…" });
  await activate(activeId || ids.at(-1));
}

async function onFiles(files) {
  try {
    const result = await importFiles(files);
    if (result && result.imported.length) await openDocuments(result.imported.map((item) => item.document.id));
  } catch (error) { showError(error); }
}
$("ws-import").addEventListener("click", () => $("ws-file").click());
$("ws-file").addEventListener("change", () => { onFiles($("ws-file").files); $("ws-file").value = ""; });
enableDrop(root, onFiles);

async function start() {
  loadTabs();
  const params = new URLSearchParams(location.search);
  const requested = (params.get("doc") || "").split(",").filter((id) => /^[0-9a-f]{32}$/.test(id));
  renderTabs();
  // A library search result links straight to the page where the words were found.
  const page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
  if (params.get("find")) $("tb-search").value = params.get("find").slice(0, 200);
  if (requested.length) {
    for (const id of requested) if (!state.tabs.some((tab) => tab.id === id)) state.tabs.push({ id, name: "Loading…" });
    await activate(requested[0], { page });
  }
  if (!requested.length && state.tabs.length) await activate(state.tabs.some((tab) => tab.id === state.activeId) ? state.activeId : state.tabs[0].id);
}
start().catch(showError);
