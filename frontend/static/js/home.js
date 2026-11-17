import { api, h, formatBytes, importFiles, enableDrop, showError } from "./api.js";
import { documentCard } from "./cards.js";

const fileInput = document.getElementById("home-file");

async function load() {
  const [status, list, caps] = await Promise.all([
    api("/api/system/status"),
    api("/api/documents?limit=6&sort=imported"),
    api("/api/system/capabilities"),
  ]);
  document.getElementById("stat-documents").textContent = status.documents;
  document.getElementById("stat-trashed").textContent = status.trashed ? `${status.trashed} in trash` : "none in trash";
  document.getElementById("stat-size").textContent = formatBytes(status.original_bytes);
  document.getElementById("stat-jobs").textContent = status.active_jobs;
  document.getElementById("stat-guardian").textContent =
    { normal: "Normal", throttled: "Limiting", paused: "Paused" }[status.guardian_state] || status.guardian_state;

  const recent = document.getElementById("recent");
  recent.replaceChildren(...list.documents.map((doc) => documentCard(doc)));
  document.getElementById("recent-empty").hidden = list.documents.length > 0;

  const has = (capability) => caps.capabilities.includes(capability);
  const rows = [
    ["View PDFs offline", has("pdf.render"), "Bundled PDF.js viewer"],
    ["Page operations", has("pdf.pages"), "Merge, split, extract, delete, reorder, rotate, crop"],
    ["Compression and metadata removal", has("pdf.compress"), "Always as a new version"],
    ["Thumbnails", has("pdf.rasterize"), "Rendered locally"],
    ["OCR for scanned pages", has("ocr.tesseract"), "Needs Tesseract; planned for a later phase"],
    ["Local AI", false, "Planned for a later phase; always optional"],
  ];
  document.getElementById("ready").replaceChildren(...rows.flatMap(([label, ok, note]) => [
    h("dt", {}, label),
    h("dd", {}, h("span", { class: `badge ${ok ? "badge-ok" : ""}` }, ok ? "Available" : "Not yet"), " ", h("span", { class: "muted" }, note)),
  ]));
}

async function onFiles(files) {
  try {
    const result = await importFiles(files);
    if (result && result.imported.length === 1 && !result.rejected.length) {
      location.href = `/workspace?doc=${result.imported[0].document.id}`;
      return;
    }
    await load();
  } catch (error) { showError(error); }
}

document.getElementById("home-import").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { onFiles(fileInput.files); fileInput.value = ""; });
enableDrop(document.getElementById("home-drop"), onFiles);
load().catch(showError);
