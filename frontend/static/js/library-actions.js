// Actions shared by the Library and the Workspace: category, tags and OCR.
import { api, formDialog, toast, showError, plural } from "./api.js";

let overviewCache = null;
export async function libraryOverview(fresh = false) {
  if (!overviewCache || fresh) overviewCache = await api("/api/library/overview");
  return overviewCache;
}

/** One line describing why ARD suggested a category. */
export function categoryReason(doc) {
  if (doc.category_source === "user") return "Set by you.";
  if (doc.category === "uncategorized") return doc.indexed ? "ARD found no clear signs of a category." : "Not analysed yet.";
  const words = (doc.category_evidence || []).map((item) => `“${item.phrase}”${item.count > 1 ? ` ×${item.count}` : ""}${item.in_name ? " (in the name)" : ""}`);
  const confidence = doc.category_confidence ? `${Math.round(doc.category_confidence * 100)}% confidence. ` : "";
  return `Suggested by ARD. ${confidence}Found: ${words.join(", ")}.`;
}

export async function changeCategory(doc) {
  const overview = await libraryOverview();
  const values = await formDialog({
    title: "Category",
    intro: `${doc.display_name}: currently ${doc.category_label}. ${categoryReason(doc)} Your choice is kept on this computer and is never replaced by a suggestion.`,
    submitLabel: "Save",
    fields: [{ name: "category", label: "Category", type: "select", value: doc.category_source === "user" ? doc.category : "auto",
      options: [{ value: "auto", label: "Let ARD suggest" }, ...overview.categories.map((c) => ({ value: c.id, label: c.label }))] }],
  });
  if (!values) return null;
  const updated = await api(`/api/documents/${doc.id}/category`, { method: "PUT", json: { category: values.category } });
  toast(values.category === "auto" ? `ARD suggests: ${updated.category_label}.` : `Category set to ${updated.category_label}.`, "success");
  overviewCache = null;
  return updated;
}

export async function editTags(doc) {
  const values = await formDialog({
    title: "Tags",
    intro: doc.display_name,
    submitLabel: "Save",
    fields: [{ name: "tags", label: "Tags, separated by commas", value: (doc.tags || []).join(", "), placeholder: "clients, 2024", hint: "Up to 30 tags of up to 40 characters each." }],
  });
  if (!values) return null;
  const tags = String(values.tags || "").split(",").map((tag) => tag.trim()).filter(Boolean);
  const updated = await api(`/api/documents/${doc.id}/tags`, { method: "PUT", json: { tags } });
  toast(tags.length ? `Saved ${plural(tags.length, "tag")}.` : "Tags removed.", "success");
  overviewCache = null;
  return updated;
}

const LANGUAGE_NAMES = { eng: "English", hin: "Hindi", mar: "Marathi", ben: "Bengali", tam: "Tamil", tel: "Telugu", guj: "Gujarati", kan: "Kannada", mal: "Malayalam", pan: "Punjabi", urd: "Urdu", fra: "French", deu: "German", spa: "Spanish" };

/**
 * Ask for the language and queue OCR. Returns immediately; `onDone(result)` runs when the job has
 * finished successfully. OCR runs in the background, so the user can keep working.
 */
export async function startOcr(doc, onDone) {
  const overview = await libraryOverview(true);
  if (!overview.ocr.available) {
    toast("Text recognition needs Tesseract and OCRmyPDF. See Trust & Settings → Integrations for how to install them.", "error", 9000);
    return;
  }
  const languages = overview.ocr.languages;
  const label = (code) => LANGUAGE_NAMES[code] || code;
  const options = languages.map((code) => ({ value: code, label: label(code) }));
  if (languages.includes("eng") && languages.includes("hin")) options.push({ value: "hin+eng", label: "Hindi and English together" });
  const values = await formDialog({
    title: "Recognise text (OCR)",
    intro: "Adds an invisible, searchable text layer to scanned pages and saves it as a new version. Pages that already have text are left alone and the page images are not changed. Recognised text is a best guess and can contain mistakes.",
    submitLabel: "Start",
    fields: [{ name: "language", label: "Language of the document", type: "select", value: languages.includes("eng") ? "eng" : languages[0], options,
      hint: `Installed language packs: ${languages.map(label).join(", ")}. Choosing the right language matters for accuracy.` }],
  });
  if (!values) return;
  let job;
  try {
    job = (await api(`/api/documents/${doc.id}/ocr`, { method: "POST", json: { languages: values.language.split("+") } })).job;
  } catch (error) { showError(error); return; }
  const note = toast(`Recognising text in “${doc.display_name}”… This runs in the background; you can keep working.`, "info", 0);
  try {
    while (job.status === "queued" || job.status === "running") {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      job = await api(`/api/jobs/${job.id}`);
    }
  } catch (error) { note.remove(); showError(error); return; }
  note.remove();
  if (job.status !== "succeeded") {
    const reason = (job.error || "").replace(/^PdfTaskError: /, "");
    toast(job.status === "cancelled" ? "Text recognition was cancelled. Nothing was changed." : `Text recognition did not complete: ${reason || "nothing was changed."}`, "error", 10000);
    return;
  }
  const report = job.result.report;
  const low = report.low_confidence_pages.length;
  toast(`Recognised text on ${plural(report.pages_recognised.length, "page")} of “${doc.display_name}”`
    + (report.mean_confidence !== null ? ` (average confidence ${Math.round(report.mean_confidence)}%)` : "")
    + `. Saved as version ${job.result.version_number}.`
    + (low ? ` ${plural(low, "page")} scored low and should be checked: ${report.low_confidence_pages.slice(0, 8).join(", ")}.` : ""),
    low ? "info" : "success", 10000);
  overviewCache = null;
  if (onDone) onDone(job.result);
}
