import { api, h, toast, showError, formatDate, formatBytes } from "./api.js";
import { capabilityList } from "./capabilities.js";

const $ = (id) => document.getElementById(id);

// ---- section tabs (deep-linkable with #audit, #guardian, ...) ----
function show(section) {
  for (const tab of document.querySelectorAll("#trust-tabs .tab")) tab.setAttribute("aria-selected", String(tab.dataset.section === section));
  for (const panel of document.querySelectorAll("section[data-section]")) panel.hidden = panel.dataset.section !== section;
  history.replaceState(null, "", `#${section}`);
  ({ audit: loadAudit, guardian: loadGuardian, integrations: () => loadCaps(false), settings: loadDiagnostics })[section]().catch(showError);
}
for (const tab of document.querySelectorAll("#trust-tabs .tab")) tab.addEventListener("click", () => show(tab.dataset.section));

// ---- audit ----
function summarise(event) {
  const d = event.details || {};
  if (d.operation) return `${String(d.operation).replace(/_/g, " ")}${d.version ? ` → version ${d.version}` : ""}`;
  if (d.reason) return d.reason;
  if (d.page_count) return `${d.page_count} pages, ${formatBytes(d.size_bytes)}`;
  if (d.display_name) return `renamed to “${d.display_name.to}”`;
  if (d.guardian_profile) return `Guardian profile: ${d.guardian_profile}`;
  if (d.files_kept) return "files kept on disk";
  return "";
}

function renderVerification(result) {
  const status = $("audit-status");
  status.textContent = `${result.status} · ${result.records} record${result.records === 1 ? "" : "s"}`;
  status.className = `badge ${result.ok ? "badge-ok" : "badge-bad"}`;
  $("audit-problem").textContent = result.ok ? "" : `First problem at record ${result.first_bad_seq}: ${result.problem} ARD has switched to safe mode. Nothing was repaired or deleted.`;
}

async function loadAudit() {
  const data = await api("/api/system/audit?limit=200");
  renderVerification(data.verification);
  $("audit-events").replaceChildren(...data.events.map((event) => h("tr", {},
    h("td", { class: "muted num" }, event.seq),
    h("td", { class: "muted num", style: "white-space:nowrap" }, formatDate(event.timestamp, true)),
    h("td", {}, event.action, event.document_id ? h("div", {}, h("a", { class: "small", href: `/workspace?doc=${event.document_id}` }, "Open document")) : null),
    h("td", {}, h("span", { class: `badge ${event.outcome === "success" ? "badge-ok" : event.outcome === "denied" ? "badge-warn" : "badge-bad"}` }, event.outcome)),
    h("td", { class: "muted" }, event.interface),
    h("td", { class: "muted" }, summarise(event)))));
  $("audit-empty").hidden = data.events.length > 0;
}
$("audit-verify").addEventListener("click", async () => {
  try {
    const result = await api("/api/system/audit/verify", { method: "POST" });
    renderVerification(result);
    toast(result.ok ? "The audit chain is intact." : "The audit chain failed verification.", result.ok ? "success" : "error");
  } catch (error) { showError(error); }
});

// ---- guardian ----
const STATE = {
  normal: ["Normal", "Background work runs freely."],
  throttled: ["Limiting work", "One job at a time until load drops."],
  paused: ["Paused", "Background work is paused. Interactive work still runs."],
};
function meter(id, percent, high, critical) {
  const element = $(id);
  element.firstElementChild.style.width = `${Math.min(100, percent)}%`;
  element.dataset.level = percent >= critical ? "critical" : percent >= high ? "high" : "ok";
}
function sparkPath(values) {
  if (values.length < 2) return "";
  const step = 100 / (values.length - 1);
  return values.map((value, index) => `${index ? "L" : "M"}${(index * step).toFixed(2)},${(100 - Math.min(100, value)).toFixed(2)}`).join(" ");
}
let profilesBuilt = false;
async function loadGuardian() {
  const data = await api("/api/system/guardian");
  const latest = data.latest, profile = data.profile;
  const [label, note] = STATE[data.state] || [data.state, ""];
  $("g-state").textContent = data.enabled ? label : "Off";
  $("g-state-note").textContent = data.enabled ? note : "Monitoring is disabled in configuration.";
  $("g-cpu").textContent = `${Math.round(latest.cpu_percent)}%`;
  $("g-mem").textContent = `${Math.round(latest.mem_percent)}%`;
  $("g-mem-note").textContent = `${formatBytes(latest.mem_available_mb * 1048576)} available`;
  $("g-disk").textContent = formatBytes(latest.disk_free_mb * 1048576);
  $("g-proc").textContent = `ARD itself uses ${formatBytes(latest.process_rss_mb * 1048576)} of memory` + (latest.temperature_c ? ` · ${latest.temperature_c}°C` : "");
  meter("g-cpu-meter", latest.cpu_percent, profile.cpu_high, 101);
  meter("g-mem-meter", latest.mem_percent, profile.mem_high, profile.mem_critical);
  $("g-spark-cpu").setAttribute("d", sparkPath(data.history.map((s) => s.cpu_percent)));
  $("g-spark-mem").setAttribute("d", sparkPath(data.history.map((s) => s.mem_percent)));

  if (!profilesBuilt) {
    profilesBuilt = true;
    $("g-profiles").replaceChildren(...data.profiles.filter((p) => p.name !== "custom").map((p) => h("label", { class: "choice" },
      h("input", { type: "radio", name: "profile", value: p.name, checked: p.name === profile.name, onchange: () => setProfile(p.name) }),
      h("div", {}, h("strong", {}, p.name[0].toUpperCase() + p.name.slice(1)),
        h("span", {}, `${p.description} Up to ${p.max_workers} job${p.max_workers === 1 ? "" : "s"} at once; limits work above ${p.cpu_high}% CPU or ${p.mem_high}% memory, pauses at ${p.mem_critical}% memory or under ${formatBytes(p.disk_min_free_mb * 1048576)} free disk.`)))));
  }
  $("g-interventions").replaceChildren(...data.interventions.map((item) => h("li", {},
    h("strong", {}, formatDate(item.timestamp, true)), ` — ${item.explanation}`)));
  $("g-interventions-empty").hidden = data.interventions.length > 0;
}
async function setProfile(name) {
  try {
    await api("/api/system/guardian", { method: "PUT", json: { profile: name } });
    toast(`Guardian profile set to ${name}.`, "success");
    await loadGuardian();
  } catch (error) { showError(error); profilesBuilt = false; loadGuardian().catch(() => {}); }
}

// ---- integrations ----
async function loadCaps(refresh) {
  const data = await api(`/api/system/capabilities${refresh ? "?refresh=true" : ""}`);
  $("caps").replaceChildren(...capabilityList(data.integrations));
}
$("caps-refresh").addEventListener("click", () => loadCaps(true).then(() => toast("Checked again.")).catch(showError));

// ---- appearance ----
const meta = document.querySelector('meta[name="color-scheme"]');
const systemDark = matchMedia("(prefers-color-scheme: dark)");
function readTheme() { try { return localStorage.getItem("color-scheme"); } catch { return null; } }
function syncTheme() {
  const saved = readTheme();
  meta.content = saved === "light" || saved === "dark" ? saved : "light dark";
  const value = saved === "light" || saved === "dark" ? saved : "system";
  for (const input of document.querySelectorAll('input[name="theme"]')) input.checked = input.value === value;
  $("theme-system-label").textContent = `System (currently ${systemDark.matches ? "dark" : "light"})`;
}
$("theme").addEventListener("change", (event) => {
  try {
    if (event.target.value === "system") localStorage.removeItem("color-scheme"); else localStorage.setItem("color-scheme", event.target.value);
  } catch { meta.content = event.target.value === "system" ? "light dark" : event.target.value; return; }
  syncTheme();
});
systemDark.addEventListener("change", syncTheme);
window.addEventListener("storage", (event) => { if (event.key === "color-scheme") syncTheme(); });
syncTheme();

// ---- diagnostics ----
let diagnostics = null;
async function loadDiagnostics() {
  diagnostics = await api("/api/system/diagnostics");
  const d = diagnostics;
  const rows = [
    ["ARD", d.ard_version], ["Python", d.python], ["System", `${d.os} (${d.machine})`], ["Listening on", `${d.bind} (this computer only)`],
    ["Data folder", d.data_dir], ["Database check", d.database.quick_check], ["Audit chain", d.audit.status],
    ["Safe mode", d.safe_mode.active ? `On: ${d.safe_mode.reason}` : "Off"],
    ["Originals", formatBytes(d.storage_bytes.originals)], ["Versions", formatBytes(d.storage_bytes.versions)],
    ["Thumbnails", formatBytes(d.storage_bytes.thumbnails)], ["Telemetry", "None"],
  ];
  $("diag").replaceChildren(...rows.flatMap(([label, value]) => [h("dt", {}, label), h("dd", {}, String(value))]));
}
$("diag-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(JSON.stringify(diagnostics, null, 2)); toast("Diagnostics copied.", "success"); }
  catch { toast("Could not copy to the clipboard.", "error"); }
});

const SECTIONS = ["audit", "guardian", "integrations", "settings"];
const fromHash = () => (SECTIONS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "audit");
show(fromHash());
window.addEventListener("hashchange", () => show(fromHash()));
setInterval(() => { if (!document.hidden && !document.querySelector('section[data-section="guardian"]').hidden) loadGuardian().catch(() => {}); }, 2500);
