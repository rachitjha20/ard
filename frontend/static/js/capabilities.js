import { h } from "./api.js";

/** Render integration entries from /api/system/capabilities. */
export function capabilityList(integrations) {
  return integrations.map((item) => h("div", { class: "cap" },
    h("h3", {}, item.name,
      h("span", { class: `badge ${item.available ? "badge-ok" : ""}` }, item.available ? "Available" : "Not installed"),
      item.required ? h("span", { class: "badge badge-accent" }, "Core") : h("span", { class: "badge" }, "Optional"),
      item.version ? h("span", { class: "muted small mono" }, item.version) : null),
    h("span", { class: "muted small" }, item.license),
    h("p", {}, item.purpose, item.requires_internet ? " Needs internet." : " Works offline."),
    item.languages ? h("p", {}, `Languages installed: ${item.languages.join(", ") || "none"}`) : null,
    item.needs_download ? h("p", {}, `Separate download: ${item.needs_download}`) : null,
    !item.available && item.fallback ? h("p", {}, `Without it: ${item.fallback}`) : null,
    !item.available && item.enable_hint ? h("p", {}, "To enable: ", h("code", {}, item.enable_hint)) : null));
}
