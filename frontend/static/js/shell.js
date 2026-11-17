// Runs on every page: keeps the Guardian indicator and the safe-mode banner current.
import { api } from "./api.js";

const LABELS = { normal: "Guardian: normal", throttled: "Guardian: limiting work", paused: "Guardian: paused work" };

async function refresh() {
  try {
    const status = await api("/api/system/status");
    const dot = document.querySelector("#rail-guardian .dot");
    const text = document.getElementById("rail-guardian-text");
    if (dot && text) {
      dot.dataset.state = status.guardian_state;
      text.textContent = LABELS[status.guardian_state] || "Guardian";
    }
    const banner = document.getElementById("safe-mode-banner");
    if (banner) {
      banner.hidden = !status.safe_mode.active;
      document.getElementById("safe-mode-reason").textContent = status.safe_mode.reason || "";
    }
    document.dispatchEvent(new CustomEvent("ard:status", { detail: status }));
  } catch {
    const dot = document.querySelector("#rail-guardian .dot");
    if (dot) dot.dataset.state = "off";
  }
}

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
