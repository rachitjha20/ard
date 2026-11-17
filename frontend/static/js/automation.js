import { api, h, showError, formatDate, toast } from "./api.js";

const STATUS = { queued: "", running: "badge-accent", succeeded: "badge-ok", failed: "badge-bad", cancelled: "badge-warn" };

function duration(job) {
  if (!job.started_at) return "";
  const end = job.finished_at ? new Date(`${job.finished_at}Z`) : new Date();
  const seconds = Math.max(0, (end - new Date(`${job.started_at}Z`)) / 1000);
  return seconds < 1 ? "< 1 s" : seconds < 90 ? `${seconds.toFixed(1)} s` : `${Math.round(seconds / 60)} min`;
}

async function cancel(job) {
  try {
    await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
    toast(job.status === "running" ? "Cancel requested. The job will finish its current step and its result will be discarded." : "Job cancelled.");
    await load();
  } catch (error) { showError(error); }
}

async function load() {
  const [data, guardian] = await Promise.all([api("/api/jobs?limit=100"), api("/api/system/guardian")]);
  document.getElementById("q-running").textContent = data.queue.running;
  document.getElementById("q-limit").textContent = data.queue.worker_limit;
  document.getElementById("q-limit-note").textContent = `Guardian: ${guardian.state}, ${guardian.profile.name} profile`;
  document.getElementById("q-mode").textContent = data.queue.mode === "process" ? "Separate processes" : "Threads (test mode)";
  const rows = data.jobs.map((job) => {
    const active = job.status === "queued" || job.status === "running";
    const result = job.status === "failed" ? "Failed; nothing was changed" : job.result?.summary || (job.cancel_requested && active ? "Cancel requested" : "");
    return h("tr", {},
      h("td", {}, (job.operation || job.kind).replace(/_/g, " "), job.document_id ? h("div", {}, h("a", { class: "small", href: `/workspace?doc=${job.document_id}` }, "Open document")) : null),
      h("td", {}, h("span", { class: `badge ${STATUS[job.status] || ""}` }, job.status)),
      h("td", { class: "muted" }, job.priority),
      h("td", { class: "muted num" }, formatDate(job.started_at || job.created_at, true)),
      h("td", { class: "muted num" }, duration(job)),
      h("td", {}, result),
      h("td", {}, active ? h("button", { type: "button", class: "btn btn-sm", disabled: job.cancel_requested, onclick: () => cancel(job) }, "Cancel") : null));
  });
  document.getElementById("jobs").replaceChildren(...rows);
  document.getElementById("jobs-empty").hidden = rows.length > 0;
}

document.getElementById("jobs-refresh").addEventListener("click", () => load().catch(showError));
load().catch(showError);
setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 4000);
