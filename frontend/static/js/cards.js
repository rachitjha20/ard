// Document card shared by Home and Library.
import { h, icon, formatBytes, formatRelative, formatDate, plural } from "./api.js";

export function thumbUrl(doc, width = 220) {
  return `/api/documents/${doc.id}/thumbnail?page=1&version=${doc.current_version_id}&width=${width}`;
}

export function documentCard(doc, { selectable = false, onSelect, onFavorite, onMenu, hit = null } = {}) {
  const thumb = h("div", { class: "doc-thumb" });
  if (doc.is_encrypted || !doc.page_count) {
    thumb.append(icon(doc.is_encrypted ? "lock" : "workspace"));
  } else {
    const image = h("img", { src: thumbUrl(doc), alt: "", loading: "lazy", decoding: "async" });
    image.addEventListener("error", () => thumb.replaceChildren(icon("workspace")));
    thumb.append(image);
  }

  const tags = h("div", { class: "doc-tags" });
  if (doc.is_encrypted) tags.append(h("span", { class: "badge badge-warn" }, "Password"));
  if (doc.version_count > 1) tags.append(h("span", { class: "badge badge-accent" }, `v${doc.current_version_number}`));
  if (doc.origin !== "import") tags.append(h("span", { class: "badge" }, { merge: "Merged", split: "Split", extract: "Extracted", convert: "Converted" }[doc.origin] || doc.origin));
  if (doc.category && doc.category !== "uncategorized") {
    const why = doc.category_source === "user" ? "Category set by you" : `Suggested by ARD${doc.category_confidence ? `, ${Math.round(doc.category_confidence * 100)}% confidence` : ""}`;
    tags.append(h("span", { class: "badge badge-ok", title: why }, doc.category_label + (doc.category_source === "user" ? "" : " ?")));
  }
  if (doc.ocr_status === "needed" || doc.ocr_status === "partial") tags.append(h("span", { class: "badge badge-warn", title: "Scanned pages without searchable text" }, "Needs OCR"));
  else if (doc.ocr_status === "done") tags.append(h("span", { class: "badge", title: "Text was recognised by OCR and may contain mistakes" }, "OCR text"));
  for (const tag of (doc.tags || []).slice(0, 4)) tags.append(h("span", { class: "badge badge-accent" }, `#${tag}`));

  // The date always says what it is: import date here, not "the" date of the document.
  const meta = [doc.page_count ? plural(doc.page_count, "page") : "Pages unknown", formatBytes(doc.size_bytes)].join(" · ");
  const when = doc.trashed
    ? `Moved to trash ${formatRelative(doc.dates.trashed_at)}`
    : `Imported ${formatRelative(doc.dates.imported_at)}`;

  const card = h("article", { class: "doc-card", dataset: { id: doc.id } },
    thumb,
    h("div", { class: "doc-body" },
      h("a", { class: "doc-name", href: `/workspace?doc=${doc.id}`, title: doc.display_name }, doc.display_name),
      h("span", { class: "doc-meta num" }, meta),
      h("span", { class: "doc-meta", title: formatDate(doc.dates.imported_at, true) }, when),
      // A search hit shows where the words were found, with the page as the citation.
      hit && hit.snippet ? h("span", { class: "doc-snippet" },
        h("a", { href: `/workspace?doc=${doc.id}&page=${hit.snippet.page}&find=${encodeURIComponent(hit.query)}` }, `p. ${hit.snippet.page}`),
        hit.page_hits > 1 ? ` (+${hit.page_hits - 1} more)` : "", ": ",
        hit.snippet.before, h("mark", {}, hit.snippet.match), hit.snippet.after,
        hit.snippet.from_ocr ? " [OCR]" : "") : null,
      hit && !hit.snippet && hit.name_match ? h("span", { class: "doc-snippet" }, "Matched in the name.") : null,
      tags.childElementCount ? tags : null));

  if (selectable) {
    card.append(h("input", {
      type: "checkbox", class: "doc-check", "aria-label": `Select ${doc.display_name}`,
      onchange: (event) => onSelect(doc, event.target.checked),
    }));
  }
  if (onFavorite && !doc.trashed) {
    card.append(h("button", {
      type: "button", class: "btn btn-ghost btn-sm btn-icon doc-fav", "aria-pressed": String(doc.favorite),
      "aria-label": doc.favorite ? "Remove from favorites" : "Add to favorites", onclick: () => onFavorite(doc),
    }, icon("star")));
  }
  if (onMenu) {
    card.append(h("button", {
      type: "button", class: "btn btn-ghost btn-sm btn-icon doc-more", "aria-label": `Actions for ${doc.display_name}`,
      onclick: (event) => onMenu(doc, event.currentTarget),
    }, icon("more")));
  }
  return card;
}
