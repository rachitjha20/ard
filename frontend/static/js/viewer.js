// PDF viewer built on the bundled PDF.js display layer.
//
// Pages are rendered only when they come near the viewport and are released
// again when they are far away, so a long document does not exhaust memory or
// block the interface. PDF scripting is never enabled.

import * as pdfjs from "/static/vendor/pdfjs/build/pdf.mjs";

const VENDOR = "/static/vendor/pdfjs";
pdfjs.GlobalWorkerOptions.workerSrc = `${VENDOR}/build/pdf.worker.mjs`;

const MAX_RENDERED = 14;          // canvases kept alive at once
const MAX_CANVAS_PIXELS = 16e6;   // per page, to bound memory at high zoom
export const ZOOM_STEPS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4];

export class PdfViewer extends EventTarget {
  constructor(container) {
    super();
    this.container = container;
    this.pdf = null;
    this.views = [];
    this.scale = 1;
    this.zoomMode = "fit-width";    // "fit-width" | "fit-page" | "custom"
    this.rotation = 0;              // view rotation only; does not change the file
    this.currentPage = 1;
    this.query = "";
    this._textCache = new Map();
    this._rendered = [];
    this._generation = 0;
    this._observer = new IntersectionObserver((entries) => this._onIntersect(entries), {
      root: container, rootMargin: "600px 0px",
    });
    this._onScroll = () => this._updateCurrentPage();
    container.addEventListener("scroll", this._onScroll, { passive: true });
    this._resize = new ResizeObserver(() => { if (this.pdf && this.zoomMode !== "custom") this._relayout(true); });
    this._resize.observe(container);
  }

  /** Load a document. `askPassword(retry)` must resolve to a password or null. */
  async open(url, { askPassword, page = 1 } = {}) {
    await this.close();
    const generation = ++this._generation;
    const task = pdfjs.getDocument({
      url,
      cMapUrl: `${VENDOR}/cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${VENDOR}/standard_fonts/`,
      wasmUrl: `${VENDOR}/wasm/`,
      iccUrl: `${VENDOR}/iccs/`,
      isEvalSupported: false,
      enableXfa: false,
      withCredentials: true,
    });
    this._task = task;
    task.onPassword = async (update, reason) => {
      const password = askPassword ? await askPassword(reason === pdfjs.PasswordResponses.INCORRECT_PASSWORD) : null;
      if (password === null || password === undefined) task.destroy();
      else update(password);
    };
    const pdf = await task.promise;
    if (generation !== this._generation) { pdf.destroy(); return; }
    this.pdf = pdf;

    const first = await pdf.getPage(1);
    this._baseSize = this._pageSize(first);
    this._build();
    this._relayout(false);
    this.goToPage(Math.min(page, pdf.numPages), false);
    this.dispatchEvent(new CustomEvent("loaded", { detail: { pages: pdf.numPages } }));
  }

  async close() {
    this._generation += 1;
    this._observer.disconnect();
    for (const view of this.views) this._release(view);
    this.views = [];
    this._rendered = [];
    this._textCache.clear();
    this.container.replaceChildren();
    const task = this._task;
    this._task = null;
    this.pdf = null;
    if (task) await task.destroy().catch(() => {});
  }

  get pageCount() { return this.pdf ? this.pdf.numPages : 0; }

  _pageSize(page) {
    const viewport = page.getViewport({ scale: 1, rotation: (page.rotate + this.rotation) % 360 });
    return { width: viewport.width, height: viewport.height };
  }

  _build() {
    const fragment = document.createDocumentFragment();
    for (let number = 1; number <= this.pdf.numPages; number += 1) {
      const element = document.createElement("div");
      element.className = "pdf-page";
      element.dataset.page = String(number);
      element.setAttribute("role", "region");
      element.setAttribute("aria-label", `Page ${number}`);
      fragment.append(element);
      this.views.push({ number, element, size: null, state: "blank", task: null, token: 0 });
    }
    this.container.append(fragment);
    for (const view of this.views) this._observer.observe(view.element);
  }

  _fitScale() {
    const style = getComputedStyle(this.container);
    const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const width = Math.max(100, this.container.clientWidth - padX - 2);
    const height = Math.max(100, this.container.clientHeight - 24);
    const base = this._baseSize;
    const byWidth = width / base.width;
    return this.zoomMode === "fit-page" ? Math.min(byWidth, height / base.height) : byWidth;
  }

  _relayout(keepPage) {
    if (!this.pdf) return;
    const page = this.currentPage;
    if (this.zoomMode !== "custom") this.scale = Math.max(0.1, Math.min(6, this._fitScale()));
    for (const view of this.views) {
      const size = view.size || this._baseSize;
      view.element.style.width = `${Math.floor(size.width * this.scale)}px`;
      view.element.style.height = `${Math.floor(size.height * this.scale)}px`;
      if (view.state !== "blank") this._release(view);
    }
    this._rendered = [];
    // Re-observing makes the observer report what is visible at the new size.
    this._observer.disconnect();
    for (const view of this.views) this._observer.observe(view.element);
    if (keepPage) this.goToPage(page, false);
    this.dispatchEvent(new CustomEvent("scale", { detail: { scale: this.scale, mode: this.zoomMode } }));
  }

  setZoom(mode, scale) {
    this.zoomMode = mode;
    if (mode === "custom") this.scale = Math.max(0.1, Math.min(6, scale));
    this._relayout(true);
  }

  zoomBy(direction) {
    const current = this.scale;
    const next = direction > 0
      ? ZOOM_STEPS.find((step) => step > current + 0.01) ?? ZOOM_STEPS.at(-1)
      : [...ZOOM_STEPS].reverse().find((step) => step < current - 0.01) ?? ZOOM_STEPS[0];
    this.setZoom("custom", next);
  }

  rotateView(delta) {
    this.rotation = (this.rotation + delta + 360) % 360;
    for (const view of this.views) view.size = null;
    this.pdf.getPage(1).then((page) => {
      this._baseSize = this._pageSize(page);
      this._relayout(true);
    });
  }

  goToPage(number, smooth = true) {
    const view = this.views[Math.max(1, Math.min(number, this.views.length)) - 1];
    if (!view) return;
    this.container.scrollTo({ top: view.element.offsetTop - 12, behavior: smooth ? "smooth" : "instant" });
    this._setCurrent(view.number);
  }

  _setCurrent(number) {
    if (number === this.currentPage) return;
    this.currentPage = number;
    this.dispatchEvent(new CustomEvent("page", { detail: { page: number } }));
  }

  _updateCurrentPage() {
    if (!this.views.length) return;
    const middle = this.container.scrollTop + this.container.clientHeight * 0.35;
    let low = 0, high = this.views.length - 1;
    while (low < high) {               // pages are in order, so binary search by offset
      const mid = (low + high + 1) >> 1;
      if (this.views[mid].element.offsetTop <= middle) low = mid; else high = mid - 1;
    }
    this._setCurrent(this.views[low].number);
  }

  _onIntersect(entries) {
    for (const entry of entries) {
      const view = this.views[Number(entry.target.dataset.page) - 1];
      if (!view) continue;
      view.visible = entry.isIntersecting;
      if (entry.isIntersecting && view.state === "blank") this._render(view);
    }
  }

  async _render(view) {
    const generation = this._generation;
    const token = ++view.token;   // a newer render or release of this page supersedes this one
    view.state = "rendering";
    try {
      const page = await this.pdf.getPage(view.number);
      if (generation !== this._generation || view.token !== token) return;
      const viewport = page.getViewport({ scale: this.scale, rotation: (page.rotate + this.rotation) % 360 });

      // Correct the placeholder if this page is not the same size as page 1.
      const size = { width: viewport.width / this.scale, height: viewport.height / this.scale };
      view.size = size;
      view.element.style.width = `${Math.floor(viewport.width)}px`;
      view.element.style.height = `${Math.floor(viewport.height)}px`;
      view.element.style.setProperty("--total-scale-factor", String(this.scale));

      let ratio = Math.min(window.devicePixelRatio || 1, 2);
      const pixels = viewport.width * viewport.height * ratio * ratio;
      if (pixels > MAX_CANVAS_PIXELS) ratio *= Math.sqrt(MAX_CANVAS_PIXELS / pixels);
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      const textLayer = document.createElement("div");
      textLayer.className = "textLayer";

      view.task = page.render({
        canvas, canvasContext: canvas.getContext("2d", { alpha: false }), viewport,
        transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null,
      });
      await view.task.promise;
      if (generation !== this._generation || view.token !== token) return;
      view.element.replaceChildren(canvas, textLayer);

      const layer = new pdfjs.TextLayer({ textContentSource: page.streamTextContent(), container: textLayer, viewport });
      await layer.render();
      if (generation !== this._generation || view.token !== token) return;
      view.state = "rendered";
      view.task = null;
      view.viewport = viewport;
      this._rendered.push(view);
      this._highlight(view);
      this._annotationTargets(view, page, viewport, token);
      this.dispatchEvent(new CustomEvent("pagerendered", { detail: { number: view.number, element: view.element, viewport } }));
      this._evict();
    } catch (error) {
      if (error && error.name === "RenderingCancelledException") return;
      if (generation !== this._generation || view.token !== token) return;
      view.state = "error";
      const note = document.createElement("p");
      note.className = "pdf-page-error";
      note.textContent = `Page ${view.number} could not be displayed.`;
      view.element.replaceChildren(note);
    }
  }

  _release(view) {
    if (view.task) { try { view.task.cancel(); } catch { /* already finished */ } }
    view.task = null;
    view.token += 1;
    view.viewport = null;
    view.state = "blank";
    const canvas = view.element.querySelector("canvas");
    if (canvas) { canvas.width = 0; canvas.height = 0; }   // frees the backing store promptly
    view.element.replaceChildren();
  }

  _evict() {
    while (this._rendered.length > MAX_RENDERED) {
      const index = this._rendered.findIndex((view) => !view.visible);
      if (index === -1) return;
      this._release(this._rendered.splice(index, 1)[0]);
    }
  }

  /** The rendered view of a page ({element, viewport}) or null if it is not on screen. */
  renderedView(number) {
    const view = this.views[number - 1];
    return view && view.state === "rendered" && view.viewport ? view : null;
  }

  get renderedViews() { return this._rendered.filter((view) => view.viewport); }

  // Existing annotations are painted on the canvas by PDF.js. For those that carry a comment,
  // add an invisible button over them so the comment can be read. Text is shown as text; links
  // and actions inside annotations are never followed.
  async _annotationTargets(view, page, viewport, token) {
    let annotations = [];
    try { annotations = await page.getAnnotations(); } catch { return; }
    if (view.token !== token) return;
    for (const annotation of annotations) {
      const text = (annotation.contentsObj && annotation.contentsObj.str) || "";
      if (!text.trim() || ["Link", "Widget", "Popup"].includes(annotation.subtype) || !annotation.rect) continue;
      const [x1, y1] = viewport.convertToViewportPoint(annotation.rect[0], annotation.rect[1]);
      const [x2, y2] = viewport.convertToViewportPoint(annotation.rect[2], annotation.rect[3]);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "annot-hit";
      button.setAttribute("aria-label", `${annotation.subtype === "Text" ? "Note" : annotation.subtype}: ${text.slice(0, 80)}`);
      Object.assign(button.style, {
        left: `${Math.min(x1, x2)}px`, top: `${Math.min(y1, y2)}px`,
        width: `${Math.abs(x2 - x1)}px`, height: `${Math.abs(y2 - y1)}px`,
      });
      const detail = { anchor: button, text, subtype: annotation.subtype, author: (annotation.titleObj && annotation.titleObj.str) || "" };
      button.addEventListener("click", () => this.dispatchEvent(new CustomEvent("annotation", { detail })));
      view.element.append(button);
    }
  }

  // ---- outline ------------------------------------------------------------
  async outline() {
    if (!this.pdf) return [];
    const items = (await this.pdf.getOutline()) || [];
    const resolve = async (item) => {
      let page = null;
      try {
        const dest = typeof item.dest === "string" ? await this.pdf.getDestination(item.dest) : item.dest;
        if (Array.isArray(dest) && dest[0] !== null && dest[0] !== undefined) {
          page = typeof dest[0] === "object" ? (await this.pdf.getPageIndex(dest[0])) + 1 : Number(dest[0]) + 1;
        }
      } catch { /* a bookmark with no usable target is shown but not clickable */ }
      // Only in-document targets are followed. External links and actions are never opened or run.
      return { title: item.title || "Untitled", page, children: await Promise.all((item.items || []).map(resolve)) };
    };
    return Promise.all(items.map(resolve));
  }

  // ---- search -------------------------------------------------------------
  async _pageText(number) {
    if (!this._textCache.has(number)) {
      const page = await this.pdf.getPage(number);
      const content = await page.getTextContent();
      this._textCache.set(number, content.items.map((item) => item.str + (item.hasEOL ? "\n" : "")).join(""));
    }
    return this._textCache.get(number);
  }

  /** Find `query` in every page. Calls onProgress(done, total). Returns [{page, snippet, count}]. */
  async search(query, { onProgress, signal } = {}) {
    this.query = query.trim();
    this._applyHighlights();
    if (!this.query || !this.pdf) return [];
    const needle = this.query.toLowerCase();
    const results = [];
    const pdf = this.pdf;
    for (let number = 1; number <= pdf.numPages; number += 1) {
      if (signal?.aborted || pdf !== this.pdf) return results;
      const text = await this._pageText(number);
      const lower = text.toLowerCase();
      let index = lower.indexOf(needle), count = 0, first = -1;
      while (index !== -1) { if (first === -1) first = index; count += 1; index = lower.indexOf(needle, index + needle.length); }
      if (count) {
        const start = Math.max(0, first - 40);
        results.push({
          page: number, count,
          before: (start > 0 ? "…" : "") + text.slice(start, first).replace(/\s+/g, " "),
          match: text.slice(first, first + needle.length),
          after: text.slice(first + needle.length, first + needle.length + 60).replace(/\s+/g, " ") + "…",
        });
      }
      if (onProgress && number % 10 === 0) onProgress(number, pdf.numPages);
    }
    return results;
  }

  clearSearch() { this.query = ""; this._applyHighlights(); }

  _applyHighlights() {
    if (!("highlights" in CSS)) return;
    CSS.highlights.delete("ard-search");
    this._ranges = [];
    for (const view of this._rendered) this._highlight(view, false);
    this._commitHighlights();
  }

  _highlight(view, commit = true) {
    if (!this.query || !("highlights" in CSS)) return;
    const layer = view.element.querySelector(".textLayer");
    if (!layer) return;
    const needle = this.query.toLowerCase();
    const walker = document.createTreeWalker(layer, NodeFilter.SHOW_TEXT);
    this._ranges ||= [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.data.toLowerCase();
      for (let index = text.indexOf(needle); index !== -1; index = text.indexOf(needle, index + needle.length)) {
        const range = new Range();
        range.setStart(node, index);
        range.setEnd(node, index + needle.length);
        this._ranges.push(range);
      }
    }
    if (commit) this._commitHighlights();
  }

  _commitHighlights() {
    if (!("highlights" in CSS)) return;
    const live = (this._ranges || []).filter((range) => range.startContainer.isConnected);
    this._ranges = live;
    if (live.length) CSS.highlights.set("ard-search", new Highlight(...live)); else CSS.highlights.delete("ard-search");
  }
}
