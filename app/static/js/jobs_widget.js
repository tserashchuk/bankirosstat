/**
 * Уведомления о фоновых задачах — колокольчик в шапке.
 */
(() => {
  const POLL_MS = 3000;
  const ACK_KEY = "jobsWidget:acknowledgedFinishedIds";
  const READ_KEY = "jobsWidget:readIds";

  const TYPE_META = {
    "report.generate": { icon: "⚡", label: "Отчёт" },
    "roadmap.save": { icon: "🗺️", label: "Roadmap", href: "/projects" },
    "sync.attach": { icon: "📝", label: "Синк", href: "/history" },
    "sync.reextract": { icon: "♻️", label: "Задачи синка", href: "/history" },
  };

  const bellBtn = document.getElementById("notifications-bell");
  const badgeEl = document.getElementById("notifications-badge");
  const dropdown = document.getElementById("notifications-dropdown");
  const listEl = document.getElementById("notifications-list");
  const emptyEl = document.getElementById("notifications-empty");
  const cancelAllBtn = document.getElementById("notifications-cancel-all");

  if (!bellBtn || !dropdown || !listEl) return;

  let timer = null;
  let dropdownOpen = false;
  let lastJobs = [];
  let cancellingAll = false;

  function loadAcked() {
    try {
      return new Set(JSON.parse(localStorage.getItem(ACK_KEY) || "[]"));
    } catch (e) {
      return new Set();
    }
  }

  function saveAcked(set) {
    try {
      localStorage.setItem(ACK_KEY, JSON.stringify(Array.from(set)));
    } catch (e) {
      // ignore
    }
  }

  function loadRead() {
    try {
      return new Set(JSON.parse(localStorage.getItem(READ_KEY) || "[]"));
    } catch (e) {
      return new Set();
    }
  }

  function saveRead(set) {
    try {
      localStorage.setItem(READ_KEY, JSON.stringify(Array.from(set)));
    } catch (e) {
      // ignore
    }
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function statusClass(status) {
    switch (status) {
      case "pending":
        return "bg-slate-100 text-slate-600";
      case "running":
        return "bg-lime-100 text-lime-700";
      case "done":
        return "bg-emerald-100 text-emerald-700";
      case "error":
        return "bg-red-100 text-red-700";
      default:
        return "bg-slate-100 text-slate-600";
    }
  }

  function statusLabel(status) {
    switch (status) {
      case "pending":
        return "В очереди";
      case "running":
        return "Выполняется";
      case "done":
        return "Готово";
      case "error":
        return "Ошибка";
      default:
        return status;
    }
  }

  function isUnread(job) {
    const read = loadRead();
    const acked = loadAcked();
    if (read.has(job.id)) return false;
    if (job.status === "pending" || job.status === "running") return true;
    if (job.status === "done" || job.status === "error") {
      return !acked.has(job.id);
    }
    return false;
  }

  function markAllAsRead(jobs) {
    if (!jobs?.length) return;
    const read = loadRead();
    for (const job of jobs) {
      read.add(job.id);
    }
    saveRead(read);
  }

  function activeJobs(jobs) {
    return (jobs || []).filter((j) => j.status === "pending" || j.status === "running");
  }

  function updateCancelAllButton(jobs) {
    if (!cancelAllBtn) return;
    const hasActive = activeJobs(jobs).length > 0;
    cancelAllBtn.classList.toggle("hidden", !hasActive);
    cancelAllBtn.disabled = cancellingAll || !hasActive;
    cancelAllBtn.textContent = cancellingAll ? "Останавливаем..." : "Остановить все";
  }

  function unreadCount(jobs) {
    return jobs.filter(isUnread).length;
  }

  function updateBadge(jobs) {
    const n = unreadCount(jobs);
    if (!badgeEl) return;
    if (n > 0) {
      badgeEl.textContent = n > 9 ? "9+" : String(n);
      badgeEl.classList.remove("hidden");
      bellBtn.classList.add("notifications-bell--active");
    } else {
      badgeEl.classList.add("hidden");
      bellBtn.classList.remove("notifications-bell--active");
    }
  }

  function renderJobActions(job) {
    if (job.type === "report.generate" && job.status === "done") {
      return `<button type="button" class="btn-accent text-xs !py-1.5 !px-3 shrink-0" data-open-report="${job.id}">Открыть</button>`;
    }
    const meta = TYPE_META[job.type];
    if (meta?.href && (job.status === "done" || job.status === "error")) {
      return `<a href="${meta.href}" class="text-xs font-semibold text-ink hover:underline shrink-0">Перейти</a>`;
    }
    return "";
  }

  function renderJobRow(job) {
    const meta = TYPE_META[job.type] || { icon: "•", label: job.type };
    const title = job.title || meta.label;
    const unread = isUnread(job);
    const errorBlock = job.error
      ? `<p class="text-xs text-red-600 mt-1 line-clamp-2">${escapeHtml(job.error)}</p>`
      : "";
    const isTerminal = job.status === "done" || job.status === "error";

    return `
      <li class="notifications-item ${unread ? "notifications-item--unread" : ""}" data-job-id="${job.id}">
        <div class="flex items-start gap-3">
          <span class="text-lg leading-none mt-0.5 shrink-0">${meta.icon}</span>
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-ink truncate">${escapeHtml(title)}</p>
            <p class="text-xs text-slate-500 mt-0.5 truncate">${escapeHtml(job.progress || "")}</p>
            ${errorBlock}
            <div class="flex items-center gap-2 mt-2 flex-wrap">
              <span class="chip ${statusClass(job.status)} !text-[10px] !px-2 !py-0.5">${statusLabel(job.status)}</span>
              ${renderJobActions(job)}
            </div>
          </div>
          ${isTerminal ? `<button type="button" class="notifications-dismiss" data-dismiss="${job.id}" title="Скрыть">×</button>` : ""}
        </div>
      </li>
    `;
  }

  function bindListEvents() {
    listEl.querySelectorAll("[data-open-report]").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const jobId = btn.getAttribute("data-open-report");
        const job = lastJobs.find((j) => j.id === jobId);
        const read = loadRead();
        read.add(jobId);
        saveRead(read);
        closeDropdown();
        if (window.ReportPanel) {
          try {
            await window.ReportPanel.openFromJob(jobId, { title: job?.title });
          } catch (err) {
            alert(err.message || "Не удалось открыть отчёт");
          }
        } else {
          window.location.href = `/?job=${jobId}`;
        }
        poll();
      });
    });

    listEl.querySelectorAll("[data-dismiss]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.getAttribute("data-dismiss");
        const acked = loadAcked();
        acked.add(id);
        saveAcked(acked);
        const read = loadRead();
        read.add(id);
        saveRead(read);
        btn.closest("li")?.remove();
        if (!listEl.children.length && emptyEl) emptyEl.classList.remove("hidden");
        updateBadge(lastJobs);
        window.dispatchEvent(new CustomEvent("job-dismissed", { detail: { id } }));
      });
    });
  }

  function render(jobs) {
    lastJobs = jobs;
    const acked = loadAcked();
    const visible = jobs.filter((j) => {
      if (j.status === "done" || j.status === "error") return !acked.has(j.id);
      return true;
    });

    updateBadge(jobs);
    updateCancelAllButton(jobs);

    if (visible.length === 0) {
      listEl.innerHTML = "";
      emptyEl?.classList.remove("hidden");
      return;
    }

    emptyEl?.classList.add("hidden");
    listEl.innerHTML = visible.map(renderJobRow).join("");
    bindListEvents();
  }

  function openDropdown() {
    markAllAsRead(lastJobs);
    dropdownOpen = true;
    dropdown.classList.remove("hidden");
    bellBtn.setAttribute("aria-expanded", "true");
    render(lastJobs);
  }

  function closeDropdown() {
    dropdownOpen = false;
    dropdown.classList.add("hidden");
    bellBtn.setAttribute("aria-expanded", "false");
  }

  function toggleDropdown() {
    if (dropdownOpen) closeDropdown();
    else openDropdown();
  }

  bellBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleDropdown();
  });

  document.addEventListener("click", (e) => {
    if (!dropdownOpen) return;
    if (dropdown.contains(e.target) || bellBtn.contains(e.target)) return;
    closeDropdown();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && dropdownOpen) closeDropdown();
  });

  async function cancelAllJobs() {
    if (cancellingAll || activeJobs(lastJobs).length === 0) return;
    if (!window.confirm("Остановить все активные задачи?")) return;

    cancellingAll = true;
    updateCancelAllButton(lastJobs);

    try {
      const res = await fetch("/api/jobs/cancel-all", { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      await poll();
      window.dispatchEvent(new CustomEvent("jobs-cancelled"));
    } catch (e) {
      alert(e.message || "Не удалось остановить задачи");
    } finally {
      cancellingAll = false;
      updateCancelAllButton(lastJobs);
    }
  }

  cancelAllBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    cancelAllJobs();
  });

  async function poll() {
    try {
      const res = await fetch("/api/jobs/active");
      if (!res.ok) return;
      const data = await res.json();
      const jobs = data.jobs || [];
      render(jobs);
      window.dispatchEvent(new CustomEvent("jobs-updated", { detail: { jobs } }));
    } catch (e) {
      // ignore transient network errors
    }
  }

  function start() {
    poll();
    if (timer) clearInterval(timer);
    timer = setInterval(poll, POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", start);

  window.JobsWidget = {
    refresh: poll,
    cancelAll: cancelAllJobs,
    markRead: (jobId) => {
      const read = loadRead();
      read.add(jobId);
      saveRead(read);
      poll();
    },
  };
})();
