/**
 * Глобальный просмотр/редактор отчёта из фоновой задачи (как на дашборде).
 */
(() => {
  const modal = document.getElementById("report-job-modal");
  if (!modal) return;

  const backdrop = document.getElementById("report-job-backdrop");
  const btnClose = document.getElementById("report-job-close");
  const titleEl = document.getElementById("report-job-title");
  const subtitleEl = document.getElementById("report-job-subtitle");
  const previewPane = document.getElementById("report-job-preview");
  const editPane = document.getElementById("report-job-edit");
  const ragBadge = document.getElementById("report-job-rag");
  const projectsBadge = document.getElementById("report-job-projects");
  const btnCopy = document.getElementById("report-job-copy");
  const btnSave = document.getElementById("report-job-save");
  const errorEl = document.getElementById("report-job-error");

  const ragStyles = {
    GREEN: { cls: "bg-lime-100 text-lime-700", label: "🟢 Портфель GREEN" },
    AMBER: { cls: "bg-amber-100 text-amber-700", label: "🟡 Портфель AMBER" },
    RED: { cls: "bg-red-100 text-red-700", label: "🔴 Портфель RED" },
  };

  let currentJobId = null;
  let currentSnapshot = null;

  function showError(msg) {
    if (!errorEl) return;
    errorEl.textContent = msg;
    errorEl.classList.remove("hidden");
  }

  function hideError() {
    errorEl?.classList.add("hidden");
  }

  function setRag(status) {
    if (!ragBadge) return;
    const meta = ragStyles[status];
    if (!meta) {
      ragBadge.classList.add("hidden");
      return;
    }
    ragBadge.className = "chip " + meta.cls;
    ragBadge.textContent = meta.label;
    ragBadge.classList.remove("hidden");
  }

  function setProjectsBadge(count) {
    if (!projectsBadge) return;
    if (!count) {
      projectsBadge.classList.add("hidden");
      return;
    }
    projectsBadge.textContent = `${count} проект(ов)`;
    projectsBadge.classList.remove("hidden");
  }

  function renderMarkdown(md) {
    const text = md || "";
    if (typeof marked !== "undefined") {
      previewPane.innerHTML = marked.parse(text);
    } else {
      previewPane.textContent = text;
    }
    editPane.value = text;
    btnCopy.disabled = !text;
    btnSave.disabled = !text || !currentJobId;
  }

  function bindTabs() {
    modal.querySelectorAll(".report-job-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.dataset.tab;
        modal.querySelectorAll(".report-job-tab").forEach((b) => {
          b.classList.toggle("active", b === btn);
        });
        if (tab === "edit") {
          previewPane.classList.add("hidden");
          editPane.classList.remove("hidden");
          editPane.classList.add("flex");
        } else {
          editPane.classList.add("hidden");
          editPane.classList.remove("flex");
          previewPane.classList.remove("hidden");
          renderMarkdown(editPane.value);
        }
      });
    });
  }

  bindTabs();

  editPane?.addEventListener("input", () => {
    if (typeof marked !== "undefined") {
      previewPane.innerHTML = marked.parse(editPane.value);
    }
  });

  function openModal() {
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function closeModal() {
    modal.classList.add("hidden");
    document.body.style.overflow = "";
    hideError();
  }

  function applyJobPayload(data) {
    currentJobId = data.job_id;
    currentSnapshot = data.snapshot;
    setRag(data.rag_status || "AMBER");
    setProjectsBadge((data.project_reports || []).length);
    renderMarkdown(data.report_text || "");
    if (titleEl) titleEl.textContent = data.title || "Готовый отчёт";
    if (subtitleEl) {
      const fmt = data.report_format || "";
      subtitleEl.textContent = fmt ? `Формат: ${fmt}` : "";
    }
    modal.querySelectorAll(".report-job-tab").forEach((b, i) => {
      b.classList.toggle("active", i === 0);
    });
    previewPane.classList.remove("hidden");
    editPane.classList.add("hidden");
    editPane.classList.remove("flex");
  }

  async function fetchReportJob(jobId) {
    const res = await fetch(`/api/reports/jobs/${jobId}`);
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async function openFromJob(jobId, { title } = {}) {
    hideError();
    const data = await fetchReportJob(jobId);
    if (data.status === "error") {
      throw new Error(data.error || "Ошибка генерации отчёта");
    }
    if (data.status !== "done") {
      throw new Error(data.progress || "Отчёт ещё готовится…");
    }
    if (title) data.title = title;
    applyJobPayload(data);
    openModal();
    return data;
  }

  btnClose?.addEventListener("click", closeModal);
  backdrop?.addEventListener("click", closeModal);

  btnCopy?.addEventListener("click", async () => {
    const text = editPane.value || previewPane.innerText;
    await navigator.clipboard.writeText(text);
    btnCopy.textContent = "Скопировано!";
    setTimeout(() => {
      btnCopy.textContent = "Копировать";
    }, 2000);
  });

  btnSave?.addEventListener("click", async () => {
    if (!currentJobId) {
      showError("Нет готового отчёта для сохранения");
      return;
    }
    hideError();
    try {
      const res = await fetch(`/api/reports/save-batch/${currentJobId}`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      const saved = await res.json();
      if (saved.id) {
        window.location.href = `/reports/${saved.id}`;
        return;
      }
      btnSave.textContent = saved.saved === 1 ? "Сохранено в историю" : `Сохранено (${saved.saved})`;
      setTimeout(() => {
        btnSave.textContent = "В историю";
      }, 2500);
    } catch (e) {
      showError(e.message || "Не удалось сохранить");
    }
  });

  window.addEventListener("open-report-job", (e) => {
    const jobId = e.detail?.jobId;
    if (jobId) openFromJob(jobId, { title: e.detail?.title }).catch((err) => showError(err.message));
  });

  window.ReportPanel = {
    openFromJob,
    close: closeModal,
    applyJobPayload,
    openModal,
  };
})();
