(() => {
  const formatSelect = document.getElementById("format-select");
  const modelSelect = document.getElementById("model-select");
  const btnGenerate = document.getElementById("btn-generate");
  const btnCopy = document.getElementById("btn-copy");
  const btnSave = document.getElementById("btn-save");
  const loader = document.getElementById("loader");
  const loaderText = document.getElementById("loader-text");
  const errorMsg = document.getElementById("error-msg");
  const previewPane = document.getElementById("preview-pane");
  const editPane = document.getElementById("edit-pane");
  const ragBadge = document.getElementById("rag-badge");
  const projectsBadge = document.getElementById("projects-badge");

  const ragStyles = {
    GREEN: { cls: "bg-lime-100 text-lime-700", label: "🟢 Портфель GREEN" },
    AMBER: { cls: "bg-amber-100 text-amber-700", label: "🟡 Портфель AMBER" },
    RED: { cls: "bg-red-100 text-red-700", label: "🔴 Портфель RED" },
  };

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

  let currentJobId = null;
  let currentSnapshot = null;
  let currentRag = "AMBER";
  let pollTimer = null;

  const LS_KEY = "dashboard:lastJobId";

  function rememberJob(jobId) {
    try { localStorage.setItem(LS_KEY, jobId); } catch (e) { /* ignore */ }
  }

  function forgetJob() {
    try { localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ }
  }

  function loadLastJob() {
    try { return localStorage.getItem(LS_KEY); } catch (e) { return null; }
  }

  function showError(msg) {
    errorMsg.textContent = msg;
    errorMsg.classList.remove("hidden");
  }

  function hideError() {
    errorMsg.classList.add("hidden");
  }

  function setLoading(on, text = "Сбор данных...") {
    loader.classList.toggle("hidden", !on);
    loaderText.textContent = text;
    btnGenerate.disabled = on;
  }

  function renderMarkdown(md) {
    if (typeof marked !== "undefined") {
      previewPane.innerHTML = marked.parse(md || "");
    } else {
      previewPane.textContent = md;
    }
    editPane.value = md || "";
    btnCopy.disabled = !md;
    btnSave.disabled = !md || !currentJobId;
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll(".tab-btn").forEach((b) => {
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

  editPane.addEventListener("input", () => {
    if (typeof marked !== "undefined") {
      previewPane.innerHTML = marked.parse(editPane.value);
    }
  });

  btnGenerate.addEventListener("click", async () => {
    const modelKey = modelSelect.value;
    const reportFormat = formatSelect?.value || "brief_progress";

    if (!modelKey || modelSelect.selectedOptions[0]?.disabled) {
      showError("Выберите доступную модель (настройте API-ключ в .env)");
      return;
    }

    hideError();
    setLoading(true, "Запуск сбора по всем проектам...");
    currentJobId = null;
    currentSnapshot = null;

    try {
      const res = await fetch("/api/reports/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_key: modelKey,
          report_format: reportFormat,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      currentJobId = data.job_id;
      rememberJob(data.job_id);
      pollJob(data.job_id);
      if (window.JobsWidget) window.JobsWidget.refresh();
    } catch (e) {
      setLoading(false);
      showError(e.message);
    }
  });

  function applyReportData(data, jobId) {
    currentJobId = jobId;
    currentSnapshot = data.snapshot;
    currentRag = data.rag_status || "AMBER";
    setRag(currentRag);
    setProjectsBadge((data.project_reports || []).length);
    renderMarkdown(data.report_text);
  }

  async function fetchJobStatus(jobId, retries = 3) {
    let lastErr = null;
    for (let i = 0; i < retries; i += 1) {
      try {
        const res = await fetch(`/api/reports/jobs/${jobId}`);
        if (!res.ok) {
          const text = await res.text();
          throw new Error(text || `HTTP ${res.status}`);
        }
        return await res.json();
      } catch (e) {
        lastErr = e;
        if (i < retries - 1) await new Promise((r) => setTimeout(r, 2000));
      }
    }
    throw lastErr;
  }

  function pollJob(jobId) {
    if (pollTimer) clearInterval(pollTimer);
    let pollFailures = 0;
    pollTimer = setInterval(async () => {
      try {
        const data = await fetchJobStatus(jobId);
        pollFailures = 0;
        setLoading(true, data.progress || "Обработка...");

        if (data.status === "done") {
          clearInterval(pollTimer);
          setLoading(false);
          applyReportData(data, jobId);
          if (window.JobsWidget) {
            window.JobsWidget.markRead(jobId);
            window.JobsWidget.refresh();
          }
        } else if (data.status === "error") {
          clearInterval(pollTimer);
          setLoading(false);
          showError(data.error || "Ошибка генерации");
          forgetJob();
          if (window.JobsWidget) window.JobsWidget.refresh();
        }
      } catch (e) {
        pollFailures += 1;
        const msg = e?.message || String(e);
        if (pollFailures >= 5) {
          clearInterval(pollTimer);
          setLoading(false);
          showError(
            msg.includes("fetch") || msg.includes("disconnect")
              ? "Сервер не отвечает во время генерации (долгий запрос к ИИ). Подождите и обновите страницу — задача может ещё выполняться."
              : msg
          );
          return;
        }
        setLoading(true, `Ожидание сервера… (${pollFailures}/5)`);
      }
    }, 2000);
  }

  btnCopy.addEventListener("click", async () => {
    const text = editPane.value || previewPane.innerText;
    await navigator.clipboard.writeText(text);
    btnCopy.textContent = "Скопировано!";
    setTimeout(() => {
      btnCopy.textContent = "Копировать";
    }, 2000);
  });

  btnSave.addEventListener("click", async () => {
    if (!currentJobId) {
      showError("Сначала сгенерируйте отчёт");
      return;
    }
    try {
      const res = await fetch(`/api/reports/save-batch/${currentJobId}`, {
        method: "POST",
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      if (data.id) {
        window.location.href = `/reports/${data.id}`;
        return;
      }
      btnSave.textContent = data.saved === 1 ? "Сохранено в историю" : `Сохранено (${data.saved})`;
      setTimeout(() => {
        btnSave.textContent = "В историю";
      }, 2500);
    } catch (e) {
      showError(e.message);
    }
  });

  async function loadJob(jobId, { openModal = false } = {}) {
    try {
      const data = await fetchJobStatus(jobId, 1);
      if (data.status === "done") {
        rememberJob(jobId);
        applyReportData(data, jobId);
        if (window.JobsWidget) window.JobsWidget.markRead(jobId);
        if (openModal && window.ReportPanel) {
          await window.ReportPanel.openFromJob(jobId);
        }
      } else if (data.status === "error") {
        showError(data.error || "Задача завершилась ошибкой");
        forgetJob();
      } else {
        rememberJob(jobId);
        currentJobId = jobId;
        setLoading(true, data.progress || "Возобновляем отслеживание задачи...");
        pollJob(jobId);
      }
    } catch (e) {
      forgetJob();
    }
  }

  async function resumeLastJob() {
    const jobId = loadLastJob();
    if (!jobId) return;
    await loadJob(jobId);
  }

  const urlJobId = new URLSearchParams(window.location.search).get("job");
  if (urlJobId) {
    loadJob(urlJobId, { openModal: true });
  } else {
    resumeLastJob();
  }

  window.addEventListener("jobs-updated", (e) => {
    const jobs = e.detail?.jobs || [];
    const running = jobs.find(
      (j) => j.type === "report.generate" && (j.status === "pending" || j.status === "running")
    );
    if (running && !pollTimer && currentJobId !== running.id) {
      rememberJob(running.id);
      currentJobId = running.id;
      setLoading(true, running.progress || "Генерация в фоне...");
      pollJob(running.id);
    }
  });
})();
