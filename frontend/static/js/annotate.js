// Annotate mode: place text, images, stamps, highlights, notes, shapes and drawings.
//
// Items live in a pending list until the user saves; saving sends them to the
// server, which writes one new version. Nothing here touches the document.
// Coordinates are kept in PDF points, so items stay put when the zoom or the
// view rotation changes.

import { api, formDialog, toast } from "./api.js";

const SVG = "http://www.w3.org/2000/svg";
const DRAG_TOOLS = ["rect", "ellipse", "line", "arrow", "ink", "redact"];
const CLICK_TOOLS = ["text", "image", "stamp", "note", "signature", "edit_text"];
const STAMPS = ["APPROVED", "DRAFT", "CONFIDENTIAL", "REVIEWED", "COPY", "VOID"];
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

// Helvetica-Bold advance widths, matching the server, so a stamp preview has the same proportions.
const BOLD = { I: 278, J: 556, M: 833, W: 944, " ": 278, "-": 333 };
for (const ch of "ABCDHKNRU") BOLD[ch] = 722;
for (const ch of "EPSVXY") BOLD[ch] = 667;
for (const ch of "FLTZ") BOLD[ch] = 611;
for (const ch of "GOQ") BOLD[ch] = 778;
for (const ch of "0123456789") BOLD[ch] = 556;

// Characters the standard PDF fonts can encode (Windows-1252).
const WIN_ANSI = /^[\n -~ -ÿ€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ]*$/;

const FONT_CSS = { sans: "Helvetica, Arial, sans-serif", serif: '"Times New Roman", Times, serif', mono: '"Courier New", Courier, monospace' };

function el(name, attrs = {}, ...children) {
  const node = document.createElementNS(SVG, name);
  for (const [key, value] of Object.entries(attrs)) if (value !== null && value !== undefined) node.setAttribute(key, value);
  node.append(...children);
  return node;
}

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

/** Every point of an item, mapped through fn (used for moving). */
function mapPoints(item, fn) {
  const copy = structuredClone(item);
  if (copy.type === "edit_text") return copy;        // an edited line stays where the original line is
  if (copy.box) for (const key of ["tl", "tr", "bl"]) copy.box[key] = fn(copy.box[key]);
  if (copy.p1) { copy.p1 = fn(copy.p1); copy.p2 = fn(copy.p2); }
  if (copy.points) copy.points = copy.points.map(fn);
  if (copy.quads) copy.quads = copy.quads.map((quad) => quad.map(fn));
  if (copy.point) copy.point = fn(copy.point);
  return copy;
}

export class Annotator extends EventTarget {
  constructor(viewer, { fileInput }) {
    super();
    this.viewer = viewer;
    this.fileInput = fileInput;
    this.active = false;
    this.items = [];
    this.undoStack = [];
    this.redoStack = [];
    this.tool = "select";
    this.color = "#b3261e";
    this.width = 2;
    this.selectedId = null;
    this.documentId = null;       // set by the workspace when a document is opened
    this._nextId = 1;
    this._drag = null;

    viewer.addEventListener("pagerendered", (event) => this._attach(event.detail));
    document.addEventListener("pointerup", () => { if (this.active && this.tool === "highlight") setTimeout(() => this._highlightSelection(), 0); });
  }

  get count() { return this.items.length; }

  // ---- mode and tools -------------------------------------------------------
  setActive(active) {
    this.active = active;
    if (!active) { this.items = []; this.undoStack = []; this.redoStack = []; this.selectedId = null; }
    this.setTool("select");
    for (const view of this.viewer.renderedViews) this._attach({ number: view.number, element: view.element, viewport: view.viewport });
    this._changed();
  }

  setTool(tool) {
    this.tool = tool;
    const container = this.viewer.container;
    container.dataset.tool = tool;
    container.dataset.drawing = String(this.active && (DRAG_TOOLS.includes(tool) || CLICK_TOOLS.includes(tool)));
    if (tool !== "select") this.select(null);
    this.dispatchEvent(new CustomEvent("tool", { detail: { tool } }));
  }

  // ---- model ---------------------------------------------------------------
  _commit(mutate) {
    this.undoStack.push(structuredClone(this.items));
    if (this.undoStack.length > 100) this.undoStack.shift();
    this.redoStack = [];
    mutate();
    this._changed();
  }

  add(item) {
    const id = this._nextId++;
    this._commit(() => this.items.push({ ...item, id }));
    return id;
  }

  undo() {
    if (!this.undoStack.length) return;
    this.redoStack.push(structuredClone(this.items));
    this.items = this.undoStack.pop();
    this.selectedId = null;
    this._changed();
  }

  redo() {
    if (!this.redoStack.length) return;
    this.undoStack.push(structuredClone(this.items));
    this.items = this.redoStack.pop();
    this.selectedId = null;
    this._changed();
  }

  removeSelected() {
    if (this.selectedId === null) return;
    const id = this.selectedId;
    this.selectedId = null;
    this._commit(() => { this.items = this.items.filter((item) => item.id !== id); });
  }

  select(id) {
    this.selectedId = id;
    for (const node of this.viewer.container.querySelectorAll(".edit-item")) node.classList.toggle("selected", Number(node.dataset.id) === id);
    this.dispatchEvent(new CustomEvent("change"));
  }

  _changed() {
    for (const view of this.viewer.renderedViews) this._redraw(view.number);
    this.dispatchEvent(new CustomEvent("change"));
  }

  /** Redaction areas for the server (redact mode). */
  areas() {
    return this.items.filter((item) => item.type === "redact").map((item) => ({ page: item.page, box: item.box }));
  }

  /** Build the request body for the server. Images are sent once each, base64-encoded. */
  payload() {
    const images = [];
    const items = this.items.map((item) => {
      const { id, href, alt, preview, ...rest } = item;
      if (item.type === "image") {
        let index = images.indexOf(href);
        if (index === -1) { images.push(href); index = images.length - 1; }
        return { ...rest, image: index };
      }
      return rest;
    });
    return { items, images: images.map((href) => href.split(",", 2)[1]) };
  }

  // ---- drawing pending items -----------------------------------------------
  _attach({ number, element }) {
    element.querySelector(".edit-layer")?.remove();
    if (!this.active) return;
    const layer = el("svg", { class: "edit-layer", "data-page": number });
    layer.addEventListener("pointerdown", (event) => this._pointerDown(event, number, layer));
    layer.addEventListener("click", (event) => {
      const view = this.viewer.renderedView(number);
      if (!this.active || !view || !CLICK_TOOLS.includes(this.tool)) return;
      this._place(this.tool, number, view.viewport, this._local(event, layer))
        .catch((error) => toast(error.message || "That could not be added.", "error"));
    });
    element.append(layer);
    this._redraw(number);
  }

  _redraw(number) {
    const view = this.viewer.renderedView(number);
    const layer = view && view.element.querySelector(".edit-layer");
    if (!layer) return;
    layer.replaceChildren(...this.items.filter((item) => item.page === number).map((item) => {
      const node = this._draw(item, view.viewport);
      node.classList.add("edit-item");
      node.dataset.id = item.id;
      if (item.id === this.selectedId) node.classList.add("selected");
      return node;
    }));
  }

  _draw(item, viewport) {
    const P = (point) => viewport.convertToViewportPoint(point[0], point[1]);
    const scale = viewport.scale;
    const opacity = item.opacity ?? 1;

    if (item.type === "edit_text") {
      // Preview only: the saved file has the old line removed, not covered.
      const start = P(item.origin);
      const end = P([item.origin[0] + item.direction[0] * item.width, item.origin[1] + item.direction[1] * item.width]);
      const ex = [(end[0] - start[0]) / item.width, (end[1] - start[1]) / item.width];
      const size = item.preview.size, family = FONT_CSS[item.preview.family] || FONT_CSS.sans;
      const measure = document.createElement("canvas").getContext("2d");
      measure.font = `${item.preview.bold ? "bold " : ""}${size}px ${family}`;
      const cover = Math.max(item.width, measure.measureText(item.text).width) + 3;
      return el("g", { transform: `matrix(${ex[0]} ${ex[1]} ${-ex[1]} ${ex[0]} ${start[0]} ${start[1]})` },
        el("rect", { class: "edit-cover", x: -1.5, y: -size * 0.9, width: cover, height: size * 1.2, "stroke-width": 1 / scale }),
        el("text", { x: 0, y: 0, "font-size": size, fill: item.preview.color || "#111", "font-family": family, "font-weight": item.preview.bold ? 700 : 400,
          "font-style": item.preview.italic ? "italic" : "normal", "xml:space": "preserve" }, item.text),
        el("title", {}, `Was: ${item.preview.oldText}`));
    }

    if (item.box) {
      const tl = P(item.box.tl), tr = P(item.box.tr), bl = P(item.box.bl);
      const w = dist(item.box.tl, item.box.tr), h = dist(item.box.tl, item.box.bl);
      // Local frame in points: x across the box, y down the screen.
      const matrix = `matrix(${(tr[0] - tl[0]) / w} ${(tr[1] - tl[1]) / w} ${(bl[0] - tl[0]) / h} ${(bl[1] - tl[1]) / h} ${tl[0]} ${tl[1]})`;
      const group = el("g", { transform: matrix, opacity });
      if (item.type === "text") {
        const text = el("text", { "font-size": item.size, fill: item.color, "font-family": FONT_CSS[item.font || "sans"], "xml:space": "preserve" });
        item.text.split("\n").forEach((line, index) => text.append(el("tspan", { x: 0, y: item.size * (0.92 + 1.2 * index) }, line || " ")));
        group.append(el("rect", { width: w, height: h, fill: "transparent" }), text);
      } else if (item.type === "image") {
        group.append(el("image", { href: item.href, width: w, height: h, preserveAspectRatio: "none" }));
      } else if (item.type === "stamp") {
        const border = Math.max(1, h * 0.07);
        const units = [...item.text].reduce((sum, ch) => sum + (BOLD[ch] || 700), 0) / 1000;
        const size = Math.min(h * 0.52, (w - 4 * border) / units);
        group.append(
          el("rect", { x: border / 2, y: border / 2, width: w - border, height: h - border, fill: "transparent", stroke: item.color, "stroke-width": border }),
          el("text", { x: w / 2, y: h / 2 + size * 0.36, "text-anchor": "middle", "font-size": size, "font-weight": 700, fill: item.color, "font-family": FONT_CSS.sans, textLength: units * size, lengthAdjust: "spacingAndGlyphs" }, item.text));
      } else if (item.type === "redact") {
        // Shown semi-transparent while editing so the user can see what will be removed.
        group.append(el("rect", { width: w, height: h, fill: "#000", "fill-opacity": 0.55, stroke: "#b3261e", "stroke-width": 1.5 / scale, "stroke-dasharray": `${5 / scale} ${3 / scale}` }));
      } else if (item.type === "rect") {
        group.append(el("rect", { width: w, height: h, fill: item.fill || "transparent", "fill-opacity": item.fill ? 1 : 0, stroke: item.color, "stroke-width": item.width, "stroke-linejoin": "round" }));
      } else if (item.type === "ellipse") {
        group.append(el("ellipse", { cx: w / 2, cy: h / 2, rx: w / 2, ry: h / 2, fill: "transparent", stroke: item.color, "stroke-width": item.width }));
      }
      return group;
    }

    if (item.type === "line" || item.type === "arrow") {
      const a = P(item.p1), b = P(item.p2);
      const group = el("g", { opacity });
      const width = item.width * scale;
      group.append(el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: item.color, "stroke-width": width, "stroke-linecap": "round" }),
        el("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: "transparent", "stroke-width": Math.max(width, 12) }));
      if (item.type === "arrow") {
        const length = dist(a, b) || 1;
        const d = [(b[0] - a[0]) / length, (b[1] - a[1]) / length];
        const head = Math.max(8, item.width * 4) * scale, wing = head * 0.45;
        const base = [b[0] - d[0] * head, b[1] - d[1] * head];
        group.append(el("polygon", { fill: item.color, points: `${base[0] - d[1] * wing},${base[1] + d[0] * wing} ${b[0]},${b[1]} ${base[0] + d[1] * wing},${base[1] - d[0] * wing}` }));
      }
      return group;
    }

    if (item.type === "ink") {
      const points = item.points.map(P).map((p) => p.join(",")).join(" ");
      return el("g", { opacity },
        el("polyline", { points, fill: "none", stroke: item.color, "stroke-width": item.width * scale, "stroke-linecap": "round", "stroke-linejoin": "round" }),
        el("polyline", { points, fill: "none", stroke: "transparent", "stroke-width": Math.max(item.width * scale, 12) }));
    }

    if (item.type === "highlight") {
      const group = el("g", { style: "mix-blend-mode:multiply" });
      for (const quad of item.quads) {
        const [tl, tr, bl, br] = quad.map(P);
        group.append(el("polygon", { fill: item.color, "fill-opacity": 0.45, points: [tl, tr, br, bl].map((p) => p.join(",")).join(" ") }));
      }
      return group;
    }

    // note
    const [x, y] = P(item.point);
    const r = 9 * scale;
    const group = el("g", {}, el("circle", { cx: x, cy: y, r, fill: item.color, stroke: "#333", "stroke-width": scale }));
    for (const [dy, length] of [[-3.4, 9], [-0.7, 9], [2, 6]]) {
      group.append(el("rect", { x: x - 4.5 * scale, y: y + dy * scale, width: length * scale, height: 1.4 * scale, fill: "#333" }));
    }
    group.append(el("title", {}, item.text));
    return group;
  }

  // ---- pointer interaction ---------------------------------------------------
  _local(event, layer) {
    const box = layer.getBoundingClientRect();
    return [event.clientX - box.left, event.clientY - box.top];
  }

  _pointerDown(event, number, layer) {
    if (event.button !== 0 || !this.active) return;
    const view = this.viewer.renderedView(number);
    if (!view) return;
    const viewport = view.viewport;
    const toPdf = (point) => viewport.convertToPdfPoint(point[0], point[1]);
    const start = this._local(event, layer);

    if (this.tool === "select") {
      const node = event.target.closest(".edit-item");
      if (!node) { this.select(null); return; }
      event.preventDefault();
      const id = Number(node.dataset.id);
      this.select(id);
      const original = this.items.find((item) => item.id === id);
      if (original.type === "edit_text") return;       // select only; it cannot be dragged away from its line
      const before = structuredClone(this.items);
      let moved = false;
      const move = (moveEvent) => {
        const now = this._local(moveEvent, layer);
        const dx = now[0] - start[0], dy = now[1] - start[1];
        if (!moved && Math.hypot(dx, dy) < 3) return;
        moved = true;
        const shifted = mapPoints(original, (point) => {
          const screen = viewport.convertToViewportPoint(point[0], point[1]);
          return toPdf([screen[0] + dx, screen[1] + dy]);
        });
        this.items = this.items.map((item) => (item.id === id ? shifted : item));
        this._redraw(number);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        if (moved) { this.undoStack.push(before); this.redoStack = []; this.dispatchEvent(new CustomEvent("change")); }
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      return;
    }

    // One-click tools open a dialog. They act on "click", not here: a dialog opened on pointerdown
    // would be dismissed at once by the release of the same click landing outside it.
    if (CLICK_TOOLS.includes(this.tool)) return;

    if (!DRAG_TOOLS.includes(this.tool)) return;
    event.preventDefault();
    layer.setPointerCapture(event.pointerId);
    const tool = this.tool;
    const base = { page: number, color: this.color, width: this.width };
    const trail = [start];
    let preview = null;
    const build = (end) => {
      if (tool === "ink") return { ...base, type: "ink", points: trail.map(toPdf) };
      if (tool === "line" || tool === "arrow") return { ...base, type: tool, p1: toPdf(start), p2: toPdf(end) };
      const x0 = Math.min(start[0], end[0]), x1 = Math.max(start[0], end[0]);
      const y0 = Math.min(start[1], end[1]), y1 = Math.max(start[1], end[1]);
      return { ...base, type: tool, box: { tl: toPdf([x0, y0]), tr: toPdf([x1, y0]), bl: toPdf([x0, y1]) } };
    };
    const move = (moveEvent) => {
      const now = this._local(moveEvent, layer);
      if (tool === "ink" && dist(now, trail.at(-1)) >= 2) trail.push(now);
      if (dist(now, start) < 3 && tool !== "ink") return;
      preview?.remove();
      preview = this._draw(build(now), viewport);
      preview.classList.add("edit-preview");
      layer.append(preview);
    };
    const up = (upEvent) => {
      layer.removeEventListener("pointermove", move);
      layer.removeEventListener("pointerup", up);
      layer.removeEventListener("pointercancel", up);
      preview?.remove();
      const end = this._local(upEvent, layer);
      const tooSmall = tool === "ink" ? trail.length < 2 : dist(start, end) < 5;
      if (upEvent.type === "pointercancel" || tooSmall) return;
      this.add(build(end));
    };
    layer.addEventListener("pointermove", move);
    layer.addEventListener("pointerup", up);
    layer.addEventListener("pointercancel", up);
  }

  /** Tools that place an item with one click and a short dialog. */
  async _place(tool, number, viewport, at) {
    const scale = viewport.scale;
    const boxAt = (x, y, widthPts, heightPts) => ({
      tl: viewport.convertToPdfPoint(x, y),
      tr: viewport.convertToPdfPoint(x + widthPts * scale, y),
      bl: viewport.convertToPdfPoint(x, y + heightPts * scale),
    });

    if (tool === "edit_text") { await this._editLine(number, viewport, at); return; }

    if (tool === "note") {
      const values = await formDialog({
        title: "Add a note", submitLabel: "Add note",
        intro: "The note is stored as a standard PDF comment. Any language is fine.",
        fields: [{ name: "text", label: "Note", type: "textarea", required: true, maxlength: 5000 }],
        validate: (v) => (String(v.text || "").trim() ? null : "Write something in the note."),
      });
      if (values) this.add({ type: "note", page: number, point: viewport.convertToPdfPoint(at[0], at[1]), text: values.text, color: "#ffd84d" });
      return;
    }

    if (tool === "stamp") {
      const values = await formDialog({
        title: "Add a stamp", submitLabel: "Add stamp",
        fields: [
          { name: "preset", label: "Stamp", type: "select", value: STAMPS[0], options: [...STAMPS.map((s) => ({ value: s, label: s })), { value: "", label: "Custom text…" }] },
          { name: "custom", label: "Custom text", placeholder: "Used when “Custom text…” is chosen", hint: "Letters A–Z, digits, spaces and hyphens; up to 40 characters." },
        ],
        validate: (v) => {
          if (v.preset) return null;
          const text = String(v.custom || "").trim().toUpperCase();
          return !text ? "Type the custom stamp text." : !/^[A-Z0-9 -]{1,40}$/.test(text) ? "Use only letters A–Z, digits, spaces and hyphens (up to 40)." : null;
        },
      });
      if (!values) return;
      const text = values.preset || values.custom.trim().toUpperCase();
      const height = 40, width = Math.max(110, [...text].reduce((sum, ch) => sum + (BOLD[ch] || 700), 0) / 1000 * height * 0.52 + 30);
      this.add({ type: "stamp", page: number, box: boxAt(at[0] - width * scale / 2, at[1] - height * scale / 2, width, height), text, color: this.color });
      return;
    }

    if (tool === "text") {
      const values = await formDialog({
        title: "Add text", submitLabel: "Add text",
        intro: "The text is drawn on top of the page. It does not edit or replace text that is already there.",
        fields: [
          { name: "text", label: "Text", type: "textarea", required: true, maxlength: 5000 },
          { name: "size", label: "Size (points)", type: "number", min: 4, max: 400, value: 14 },
          { name: "font", label: "Font", type: "select", value: "sans", options: [{ value: "sans", label: "Sans-serif (Helvetica)" }, { value: "serif", label: "Serif (Times)" }, { value: "mono", label: "Monospace (Courier)" }] },
        ],
        validate: (v) => (!String(v.text || "").trim() ? "Type the text to add." : !(v.size >= 4 && v.size <= 400) ? "Size must be between 4 and 400 points." : null),
      });
      if (!values) return;
      const text = values.text.replace(/\r\n?/g, "\n").replace(/\t/g, "    ");
      const lines = text.split("\n");
      const measure = document.createElement("canvas").getContext("2d");
      measure.font = `${values.size}px ${FONT_CSS[values.font]}`;
      const width = Math.max(4, ...lines.map((line) => measure.measureText(line).width)) + 2;
      const height = lines.length * values.size * 1.2;
      const box = boxAt(at[0], at[1], width, height);
      if (WIN_ANSI.test(text)) {
        this.add({ type: "text", page: number, box, text, size: values.size, font: values.font, color: this.color });
      } else {
        // The standard PDF fonts cannot encode this script. Draw it with the system's fonts and place it as an image.
        const ratio = 3;
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(width * ratio);
        canvas.height = Math.ceil(height * ratio);
        const context = canvas.getContext("2d");
        context.font = `${values.size * ratio}px ${FONT_CSS[values.font]}`;
        context.fillStyle = this.color;
        lines.forEach((line, index) => context.fillText(line, 0, values.size * ratio * (0.92 + 1.2 * index)));
        this.add({ type: "image", page: number, box, href: canvas.toDataURL("image/png"), alt: text });
        toast("This text uses characters the built-in PDF fonts cannot encode, so it was added as an image. It will look right but will not be searchable or selectable.", "info", 9000);
      }
      return;
    }

    if (tool === "signature") {
      const href = await drawSignature();
      if (!href) return;
      const picture = new Image();
      await new Promise((resolve, reject) => { picture.onload = resolve; picture.onerror = reject; picture.src = href; });
      const width = Math.min(180, viewport.width / scale * 0.5);
      const height = width * picture.naturalHeight / picture.naturalWidth;
      this.add({ type: "image", page: number, box: boxAt(at[0] - width * scale / 2, at[1] - height * scale / 2, width, height), href, alt: "Visual signature" });
      return;
    }

    // image
    const file = await new Promise((resolve) => {
      const input = this.fileInput;
      input.value = "";
      const done = () => { input.removeEventListener("change", done); input.removeEventListener("cancel", done); resolve(input.files[0] || null); };
      input.addEventListener("change", done);
      input.addEventListener("cancel", done);
      input.click();
    });
    if (!file) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) throw new Error("Choose a PNG or JPEG image.");
    if (file.size > MAX_IMAGE_BYTES) throw new Error("That image is larger than the 10 MB limit.");
    const href = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("The image could not be read."));
      reader.readAsDataURL(file);
    });
    const image = new Image();
    await new Promise((resolve, reject) => { image.onload = resolve; image.onerror = () => reject(new Error("That file is not a readable image.")); image.src = href; });
    const pageWidth = viewport.width / scale;
    const width = Math.min(240, pageWidth * 0.6, image.naturalWidth);
    const height = width * image.naturalHeight / image.naturalWidth;
    this.add({ type: "image", page: number, box: boxAt(at[0], at[1], width, height), href });
  }

  /**
   * Edit text tool. Finds the run of text under the click, shows it with its detected font and size,
   * and queues a replacement. On save the server removes the original line and draws the new one.
   */
  async _editLine(number, viewport, at) {
    const page = await this.viewer.pdf.getPage(number);
    const content = await page.getTextContent();
    const point = viewport.convertToPdfPoint(at[0], at[1]);
    const runs = content.items.filter((item) => item.str && item.str.trim() && item.width > 0).map((item) => {
      const [a, b, c, d, e, f] = item.transform;
      const along = Math.hypot(a, b) || 1, size = Math.hypot(c, d) || item.height || 1;
      return { item, origin: [e, f], ex: [a / along, b / along], up: [c / size, d / size], size, width: item.width };
    });
    const project = (run, p) => [(p[0] - run.origin[0]) * run.ex[0] + (p[1] - run.origin[1]) * run.ex[1],
      (p[0] - run.origin[0]) * run.up[0] + (p[1] - run.origin[1]) * run.up[1]];
    const hit = runs.find((run) => { const [x, y] = project(run, point); return x >= -2 && x <= run.width + 2 && y >= -0.3 * run.size && y <= run.size; });
    if (!hit) throw new Error("There is no text there to edit. Click directly on a line of text. Scanned pages have no editable text.");

    // Grow the run sideways over neighbouring pieces on the same baseline in the same font.
    const same = runs.filter((run) => run.item.fontName === hit.item.fontName
      && Math.abs(run.ex[0] * hit.ex[0] + run.ex[1] * hit.ex[1]) > 0.999
      && Math.abs(project(hit, run.origin)[1]) < 0.25 * hit.size && Math.abs(run.size - hit.size) < 0.05 * hit.size)
      .map((run) => ({ run, start: project(hit, run.origin)[0] })).sort((p, q) => p.start - q.start);
    let first = same.findIndex((entry) => entry.run === hit), last = first;
    const gapOk = (left, right) => right.start - (left.start + left.run.width) < 1.2 * hit.size;
    while (first > 0 && gapOk(same[first - 1], same[first])) first -= 1;
    while (last < same.length - 1 && gapOk(same[last], same[last + 1])) last += 1;
    const pieces = same.slice(first, last + 1);
    let oldText = "";
    pieces.forEach((entry, index) => {
      if (index) {
        const gap = entry.start - (pieces[index - 1].start + pieces[index - 1].run.width);
        if (gap > 0.15 * hit.size && !oldText.endsWith(" ") && !entry.run.item.str.startsWith(" ")) oldText += " ";
      }
      oldText += entry.run.item.str;
    });
    const origin = pieces[0].run.origin;
    const width = pieces.at(-1).start + pieces.at(-1).run.width - pieces[0].start;

    // Ask the server what this line really is in the file: its font, size and colour. This also tells us
    // up front if the line cannot be edited, instead of failing when the user saves.
    const direction = pieces[0].run.ex;
    const waiting = toast("Reading the line's font…", "info", 0);
    let found;
    try {
      found = await api(`/api/documents/${this.documentId}/text-line`, {
        method: "POST", json: { page: number, line: { origin, direction, width, height: hit.size } } });
    } finally { waiting.remove(); }
    if (!found.found) throw new Error(found.problem);
    const fontName = found.font || "";
    const family = found.family;
    const detectedSize = Math.round(found.size * 10) / 10;
    const look = [found.bold ? "bold" : "", found.italic ? "italic" : ""].filter(Boolean).join(" ");
    const fontNote = found.can_keep_font
      ? "ARD keeps this font as long as it already contains every character you type; a font stored in a PDF usually holds only the characters the document uses."
      : `This font is stored in a form ARD cannot add new text to, so the closest standard font (${found.standard_equivalent}) will be used, at the same size and colour.`;

    const existing = this.items.find((item) => item.type === "edit_text" && item.page === number
      && Math.hypot(item.origin[0] - origin[0], item.origin[1] - origin[1]) < 0.5);
    const values = await formDialog({
      title: "Edit text",
      intro: `Detected: ${fontName || "an unnamed font"}${look ? ` (${look})` : ""}, ${detectedSize} pt, colour ${found.color}. The original line is removed and your text is drawn in its place, starting at the same spot. Other lines do not move, so a much longer line may run into what is next to it.`,
      submitLabel: "Apply",
      fields: [
        { name: "text", label: "Text", value: existing ? existing.text : oldText, required: true, maxlength: 2000 },
        { name: "font", label: "Font", type: "select", value: existing ? existing.font : "original", options: [
          { value: "original", label: `Same as the original${fontName ? ` (${fontName})` : ""}` },
          { value: "sans", label: "Sans-serif (Helvetica)" }, { value: "serif", label: "Serif (Times)" }, { value: "mono", label: "Monospace (Courier)" }],
          hint: fontNote },
        { name: "size", label: "Size (points)", type: "number", min: 2, max: 800, value: existing && existing.size ? existing.size : detectedSize },
      ],
      validate: (v) => (!String(v.text || "").trim() ? "Type the new text, or cancel." : !WIN_ANSI.test(v.text)
        ? "Editing text in place supports Latin characters for now. For other scripts, use Add text, which places the text as an image."
        : !(v.size >= 2 && v.size <= 800) ? "Size must be between 2 and 800 points." : null),
    });
    if (!values) return;
    const sizeChanged = Math.abs(values.size - detectedSize) > 0.05;
    const item = {
      type: "edit_text", page: number, origin, direction, width, height: hit.size, text: values.text, font: values.font,
      preview: { oldText, size: values.size, family: values.font === "original" ? family : values.font, bold: found.bold, italic: found.italic, color: found.color },
    };
    if (sizeChanged) item.size = values.size;
    if (values.text === oldText && values.font === "original" && !sizeChanged) {
      if (existing) { const id = existing.id; this._commit(() => { this.items = this.items.filter((entry) => entry.id !== id); }); }
      toast("Nothing was changed on that line.", "info");
      return;
    }
    if (existing) { const id = existing.id; this._commit(() => { this.items = this.items.map((entry) => (entry.id === id ? { ...item, id } : entry)); }); }
    else this.add(item);
  }

  /** Highlight tool: turn the current text selection into highlight items, one per page. */
  _highlightSelection() {
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!this.viewer.container.contains(range.commonAncestorContainer)) return;
    const text = selection.toString().trim().slice(0, 5000);
    const byPage = new Map();
    for (const rect of range.getClientRects()) {
      if (rect.width < 1 || rect.height < 1) continue;
      const view = this.viewer.renderedViews.find((candidate) => {
        const box = candidate.element.getBoundingClientRect();
        const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
        return cx >= box.left && cx <= box.right && cy >= box.top && cy <= box.bottom;
      });
      if (!view) continue;
      const box = view.element.getBoundingClientRect();
      // Skip the full-page rectangle some browsers report for the text layer container itself.
      if (rect.width > box.width * 0.98 && rect.height > box.height * 0.5) continue;
      const local = { left: rect.left - box.left, right: rect.right - box.left, top: rect.top - box.top, bottom: rect.bottom - box.top };
      const rows = byPage.get(view) || [];
      // Merge pieces that sit on the same line into one strip.
      const same = rows.find((row) => Math.abs(row.top - local.top) < rect.height * 0.5 && Math.abs(row.bottom - local.bottom) < rect.height * 0.5
        && local.left <= row.right + rect.height && local.right >= row.left - rect.height);
      if (same) { same.left = Math.min(same.left, local.left); same.right = Math.max(same.right, local.right); same.top = Math.min(same.top, local.top); same.bottom = Math.max(same.bottom, local.bottom); }
      else rows.push(local);
      byPage.set(view, rows);
    }
    if (!byPage.size) return;
    const before = structuredClone(this.items);
    for (const [view, rows] of byPage) {
      const toPdf = (x, y) => view.viewport.convertToPdfPoint(x, y);
      const quads = rows.slice(0, 500).map((row) => [toPdf(row.left, row.top), toPdf(row.right, row.top), toPdf(row.left, row.bottom), toPdf(row.right, row.bottom)]);
      this.items.push({ id: this._nextId++, type: "highlight", page: view.number, quads, color: "#ffd84d", text });
    }
    this.undoStack.push(before);
    this.redoStack = [];
    selection.removeAllRanges();
    this._changed();
  }
}


/** Signature pad. Resolves to a PNG data URL trimmed to the ink, or null if cancelled. */
function drawSignature() {
  const dialog = document.getElementById("sign-dialog");
  const canvas = document.getElementById("sign-pad");
  const error = document.getElementById("sign-error");
  const context = canvas.getContext("2d");
  let bounds = null, last = null;
  const clear = () => { context.clearRect(0, 0, canvas.width, canvas.height); bounds = null; error.textContent = ""; };
  clear();
  context.lineCap = "round";
  context.lineJoin = "round";
  context.strokeStyle = "#0b1f3a";
  context.lineWidth = 5;

  const position = (event) => {
    const box = canvas.getBoundingClientRect();
    return [(event.clientX - box.left) * canvas.width / box.width, (event.clientY - box.top) * canvas.height / box.height];
  };
  const grow = ([x, y]) => {
    bounds = bounds ? [Math.min(bounds[0], x), Math.min(bounds[1], y), Math.max(bounds[2], x), Math.max(bounds[3], y)] : [x, y, x, y];
  };
  const down = (event) => { canvas.setPointerCapture(event.pointerId); last = position(event); grow(last); context.beginPath(); context.arc(last[0], last[1], 2.5, 0, Math.PI * 2); context.fillStyle = "#0b1f3a"; context.fill(); error.textContent = ""; };
  const move = (event) => {
    if (!last) return;
    const now = position(event);
    context.beginPath(); context.moveTo(last[0], last[1]); context.lineTo(now[0], now[1]); context.stroke();
    last = now; grow(now);
  };
  const up = () => { last = null; };
  const submit = (event) => {
    if (event.submitter && event.submitter.value === "ok" && !bounds) { event.preventDefault(); error.textContent = "Draw your signature first."; }
  };
  const clearButton = document.getElementById("sign-clear");
  const form = dialog.querySelector("form");
  canvas.addEventListener("pointerdown", down);
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  clearButton.addEventListener("click", clear);
  form.addEventListener("submit", submit);

  return new Promise((resolve) => {
    dialog.returnValue = "";
    dialog.addEventListener("close", () => {
      canvas.removeEventListener("pointerdown", down);
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", up);
      clearButton.removeEventListener("click", clear);
      form.removeEventListener("submit", submit);
      const ok = dialog.returnValue === "ok" && bounds;
      dialog.returnValue = "";
      if (!ok) { resolve(null); return; }
      const pad = 8;
      const x = Math.max(0, Math.floor(bounds[0] - pad)), y = Math.max(0, Math.floor(bounds[1] - pad));
      const w = Math.min(canvas.width - x, Math.ceil(bounds[2] - bounds[0] + 2 * pad)), h = Math.min(canvas.height - y, Math.ceil(bounds[3] - bounds[1] + 2 * pad));
      const out = document.createElement("canvas");
      out.width = w; out.height = h;
      out.getContext("2d").drawImage(canvas, x, y, w, h, 0, 0, w, h);
      resolve(out.toDataURL("image/png"));        // transparent background: only the ink is placed
    }, { once: true });
    dialog.showModal();
  });
}
