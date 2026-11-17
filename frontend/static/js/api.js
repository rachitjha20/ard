// Shared helpers: API client, DOM builder, dialogs, toasts, formatting.
// Plain ES modules; nothing is loaded from outside this computer.

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body || {};
  }
}

/** Call the JSON API. `confirm: true` marks a destructive action the user has confirmed. */
export async function api(path, { method = "GET", json, body, confirm = false, signal } = {}) {
  const headers = {};
  if (json !== undefined) headers["Content-Type"] = "application/json";
  if (confirm) headers["X-ARD-Confirm"] = "yes";
  let response;
  try {
    response = await fetch(path, {
      method, headers, signal, credentials: "same-origin",
      body: json !== undefined ? JSON.stringify(json) : body,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("ARD is not responding. Is it still running?", 0);
  }
  let data = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) data = await response.json().catch(() => null);
  if (!response.ok) {
    let message = data && data.detail;
    if (Array.isArray(message)) message = message.map((item) => item.msg).join("; ");
    if (response.status === 401) message = "Your session has expired. Reload the page.";
    throw new ApiError(message || `Request failed (${response.status}).`, response.status, data);
  }
  return data;
}

/** Wait for a job started by an operation endpoint. Short jobs are already done. */
export async function finishJob(response, onProgress) {
  if (response.done) return response.result;
  let job = response.job;
  while (job.status === "queued" || job.status === "running") {
    if (onProgress) onProgress(job);
    await new Promise((resolve) => setTimeout(resolve, 700));
    job = await api(`/api/jobs/${job.id}`);
  }
  if (job.status === "succeeded") return job.result;
  if (job.status === "cancelled") throw new ApiError("The operation was cancelled. Nothing was changed.", 0);
  throw new ApiError("The operation failed and nothing was changed. The original document is intact.", 0);
}

/** Tiny DOM builder: h("button", {class: "btn", onclick: fn}, "Label"). Text is never parsed as HTML. */
export function h(tag, attrs, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2), value);
    else if (key === "class") element.className = value;
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key in element && !["list", "form", "type"].includes(key) && typeof value !== "string") element[key] = value;
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

export function toast(message, kind = "info", timeout = 5000) {
  const host = document.getElementById("toasts");
  const close = h("button", { type: "button", "aria-label": "Dismiss" }, icon("close"));
  const item = h("div", { class: "toast", dataset: { kind } }, h("span", {}, message), close);
  const remove = () => item.remove();
  close.addEventListener("click", remove);
  host.append(item);
  if (timeout) setTimeout(remove, kind === "error" ? timeout * 2 : timeout);
  return item;
}

export function showError(error) {
  if (error && error.name === "AbortError") return;
  console.error(error);
  toast(error instanceof ApiError ? error.message : "Something went wrong. Nothing was changed.", "error");
}

function openDialog(dialog) {
  return new Promise((resolve) => {
    dialog.returnValue = "";
    dialog.addEventListener("close", () => {
      const value = dialog.returnValue;
      dialog.returnValue = "";
      resolve(value);
    }, { once: true });
    dialog.showModal();
  });
}

/** Ask the user to confirm. Resolves true only if they chose the confirm button. */
export async function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false }) {
  const dialog = document.getElementById("confirm-dialog");
  dialog.querySelector("#confirm-title").textContent = title;
  dialog.querySelector("#confirm-message").textContent = message;
  const ok = dialog.querySelector("#confirm-ok");
  ok.textContent = confirmLabel;
  ok.className = danger ? "btn btn-danger" : "btn btn-primary";
  return (await openDialog(dialog)) === "ok";
}

/**
 * Show a small form. `fields`: [{name, label, type, value, hint, options, min, max, required, placeholder}].
 * type is text | number | password | select | radio. Resolves to {name: value} or null if cancelled.
 */
export async function formDialog({ title, intro = "", fields, submitLabel = "OK", validate }) {
  const dialog = document.getElementById("form-dialog");
  const form = dialog.querySelector("form");
  dialog.querySelector("#form-title").textContent = title;
  const introElement = dialog.querySelector("#form-intro");
  introElement.textContent = intro;
  introElement.hidden = !intro;
  dialog.querySelector("#form-ok").textContent = submitLabel;
  const error = dialog.querySelector("#form-error");
  error.textContent = "";
  const host = dialog.querySelector("#form-fields");
  host.replaceChildren();

  fields.forEach((field, index) => {
    const id = `ff-${field.name}`;
    let control;
    if (field.type === "radio") {
      control = h("div", { class: "fields", role: "radiogroup", "aria-label": field.label },
        field.options.map((option) => h("label", { class: "choice" },
          h("input", { type: "radio", name: field.name, value: option.value, checked: option.value === field.value }),
          h("div", {}, h("strong", {}, option.label), option.hint ? h("span", {}, option.hint) : null))));
      host.append(h("div", { class: "field" }, h("span", { class: "label" }, field.label), control));
      return;
    }
    if (field.type === "file") {
      control = h("input", { id, name: field.name, type: "file", accept: field.accept, required: field.required });
    } else if (field.type === "textarea") {
      control = h("textarea", { id, name: field.name, rows: field.rows || 4, required: field.required, placeholder: field.placeholder, maxlength: field.maxlength });
      control.value = field.value ?? "";
    } else if (field.type === "select") {
      control = h("select", { id, name: field.name },
        field.options.map((option) => h("option", { value: option.value, selected: option.value === field.value }, option.label)));
    } else {
      control = h("input", {
        id, name: field.name, type: field.type || "text", value: field.value ?? "", required: field.required,
        min: field.min, max: field.max, placeholder: field.placeholder, maxlength: field.maxlength,
        autocomplete: field.type === "password" ? "new-password" : "off",
      });
    }
    if (index === 0) control.setAttribute("autofocus", "");
    host.append(h("div", { class: "field" }, h("label", { for: id }, field.label), control,
      field.hint ? h("span", { class: "hint" }, field.hint) : null));
  });

  const read = () => {
    const values = {};
    for (const field of fields) {
      const data = new FormData(form).get(field.name);
      values[field.name] = field.type === "number" && data !== "" && data !== null ? Number(data) : data;
    }
    return values;
  };

  // Validation keeps the dialog open and explains why, instead of closing and failing later.
  const onSubmit = (event) => {
    if (event.submitter && event.submitter.value !== "ok") return;
    const problem = validate ? validate(read()) : null;
    if (problem) {
      event.preventDefault();
      error.textContent = problem;
    }
  };
  form.addEventListener("submit", onSubmit);
  const result = await openDialog(dialog);
  form.removeEventListener("submit", onSubmit);
  return result === "ok" ? read() : null;
}

/** A popover menu anchored to a button. items: [{label, icon, onSelect, danger} | "separator"]. */
export function openMenu(anchor, items) {
  document.querySelectorAll(".menu[data-transient]").forEach((menu) => menu.remove());
  const menu = h("ul", { class: "menu", popover: "auto", role: "menu", dataset: { transient: "1" } });
  for (const item of items) {
    if (!item) continue;
    if (item === "separator") { menu.append(h("li", { role: "none" }, h("hr"))); continue; }
    menu.append(h("li", { role: "none" }, h("button", {
      type: "button", role: "menuitem", class: item.danger ? "danger" : "", disabled: item.disabled,
      onclick: () => { menu.hidePopover(); item.onSelect(); },
    }, item.icon ? icon(item.icon) : null, item.label)));
  }
  document.body.append(menu);
  menu.addEventListener("toggle", (event) => { if (event.newState === "closed") menu.remove(); });
  menu.addEventListener("keydown", (event) => {
    const buttons = [...menu.querySelectorAll("button:not(:disabled)")];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); buttons[(index + 1) % buttons.length].focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); buttons[(index - 1 + buttons.length) % buttons.length].focus(); }
  });
  menu.showPopover();
  const box = anchor.getBoundingClientRect();
  const size = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(box.left, window.innerWidth - size.width - 8));
  const below = box.bottom + 4 + size.height < window.innerHeight;
  menu.style.left = `${left}px`;
  menu.style.top = `${below ? box.bottom + 4 : Math.max(8, box.top - size.height - 4)}px`;
  menu.querySelector("button:not(:disabled)")?.focus();
  return menu;
}

// ---- formatting -----------------------------------------------------------
export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const dateTimeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

function parseDate(value) {
  if (!value) return null;
  // Timestamps stored without a zone are UTC.
  const text = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value, withTime = false) {
  const date = parseDate(value);
  return date ? (withTime ? dateTimeFormat : dateFormat).format(date) : "";
}

export function formatRelative(value) {
  const date = parseDate(value);
  if (!date) return "";
  const seconds = (date.getTime() - Date.now()) / 1000;
  const steps = [[60, "second"], [3600, "minute"], [86400, "hour"], [604800, "day"]];
  let divisor = 1;
  for (const [limit, unit] of steps) {
    if (Math.abs(seconds) < limit) return relative.format(Math.round(seconds / divisor), unit);
    divisor = limit;
  }
  return dateFormat.format(date);
}

export function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** Upload PDF files. Returns {imported, rejected}; tells the user about each outcome. */
export async function importFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return null;
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  const office = files.some((file) => /\.(docx?|odt|rtf|xlsx?|ods|pptx?|odp)$/i.test(file.name));
  const busy = toast(`Importing ${plural(files.length, "file")}…` + (office ? " Converting office documents can take a little while." : ""), "info", 0);
  try {
    const result = await api("/api/documents", { method: "POST", body: form });
    const fresh = result.imported.filter((item) => !item.already_imported).length;
    const known = result.imported.length - fresh;
    const converted = result.imported.filter((item) => item.converted_from).length;
    if (fresh) toast(`Imported ${plural(fresh, "document")}.` + (converted ? ` ${converted} converted to PDF with LibreOffice; the layout may differ from the original.` : ""), "success", converted ? 9000 : 5000);
    if (known) toast(`${plural(known, "file")} already in your library; no copy was made.`);
    for (const item of result.rejected) toast(`${item.filename}: ${item.reason}`, "error");
    return result;
  } finally {
    busy.remove();
  }
}

/** Make an element accept dropped PDF files. */
export function enableDrop(element, onFiles) {
  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  element.addEventListener("dragenter", (event) => { if (hasFiles(event)) { depth += 1; element.classList.add("drop-active"); } });
  element.addEventListener("dragleave", (event) => { if (hasFiles(event) && --depth <= 0) { depth = 0; element.classList.remove("drop-active"); } });
  element.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  element.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    element.classList.remove("drop-active");
    onFiles(event.dataTransfer.files);
  });
}
