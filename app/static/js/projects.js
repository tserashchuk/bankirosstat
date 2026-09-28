(() => {
  const projectEditModal = document.getElementById("project-edit-modal");
  const editProjectId = document.getElementById("edit-project-id");
  const editProjectName = document.getElementById("edit-project-name");
  const editProjectDescription = document.getElementById("edit-project-description");

  function closeProjectEditModal() {
    projectEditModal?.classList.add("hidden");
  }

  function openProjectEditModal() {
    projectEditModal?.classList.remove("hidden");
  }

  document.getElementById("form-new-project")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const res = await fetch("/api/projects", { method: "POST", body: fd });
    if (res.ok) location.reload();
    else alert(await res.text());
  });

  document.querySelectorAll(".source-type-select").forEach((sel) => {
    const form = sel.closest("form");
    if (!form) return;
    const updateFields = () => {
      const type = sel.value;
      form.querySelectorAll(".source-fields").forEach((block) => {
        block.classList.toggle("hidden", block.dataset.type !== type);
      });
    };
    sel.addEventListener("change", updateFields);
    updateFields();
  });

  document.querySelectorAll(".form-add-source").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const projectId = form.dataset.projectId;
      const type = form.querySelector('[name="source_type"]').value;
      const label = form.querySelector('[name="label"]')?.value || "";
      const creds = {};

      const active = form.querySelector(`.source-fields[data-type="${type}"]`);
      active?.querySelectorAll("input, select, textarea").forEach((el) => {
        if (!el.name) return;
        const raw = (el.value || "").trim();
        if (!raw) return;
        creds[el.name] = raw;
      });

      if (type === "email") {
        if (creds.port) creds.port = parseInt(creds.port, 10);
        if (creds.email && (creds.email.includes("@yandex.") || creds.email.endsWith("@ya.ru"))) {
          creds.provider = "yandex";
        }
      }
      if (type === "google_sheet" && creds.spreadsheet_id && !creds.gid) creds.gid = "0";

      const body = new FormData();
      body.append("source_type", type);
      body.append("label", label);
      body.append("credentials_json", JSON.stringify(creds));

      const res = await fetch(`/api/projects/${projectId}/sources`, {
        method: "POST",
        body,
      });
      if (res.ok) location.reload();
      else alert(await res.text());
    });
  });

  document.querySelectorAll(".btn-test-source").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.sourceId;
      btn.disabled = true;
      btn.textContent = "...";
      try {
        const res = await fetch(`/api/sources/${id}/test`, { method: "POST" });
        const data = await res.json();
        alert(data.message);
      } finally {
        btn.disabled = false;
        btn.textContent = "Проверить";
      }
    });
  });

  document.querySelectorAll(".btn-delete-source").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Удалить источник?")) return;
      const res = await fetch(`/api/sources/${btn.dataset.sourceId}`, { method: "DELETE" });
      if (res.ok) location.reload();
    });
  });

  document.querySelectorAll(".form-assign-employee").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const projectId = form.dataset.projectId;
      const employeeId = form.querySelector('[name="employee_id"]').value;
      const res = await fetch(`/api/projects/${projectId}/employees/${employeeId}`, { method: "POST" });
      if (res.ok) location.reload();
      else alert(await res.text());
    });
  });

  document.querySelectorAll(".btn-unassign-employee").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await fetch(
        `/api/projects/${btn.dataset.projectId}/employees/${btn.dataset.employeeId}`,
        { method: "DELETE" }
      );
      if (res.ok) location.reload();
    });
  });

  document.querySelectorAll(".btn-edit-project").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const projectId = btn.dataset.projectId;
      if (!projectId) return;

      btn.disabled = true;
      try {
        const res = await fetch(`/api/projects/${projectId}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(typeof data.detail === "string" ? data.detail : "Не удалось загрузить проект");
          return;
        }
        if (editProjectId) editProjectId.value = data.id || projectId;
        if (editProjectName) editProjectName.value = data.name || "";
        if (editProjectDescription) editProjectDescription.value = data.description || "";
        openProjectEditModal();
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.getElementById("project-edit-cancel")?.addEventListener("click", closeProjectEditModal);
  document.getElementById("project-edit-backdrop")?.addEventListener("click", closeProjectEditModal);

  document.getElementById("form-edit-project")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const projectId = editProjectId?.value;
    const name = editProjectName?.value?.trim();
    const description = editProjectDescription?.value?.trim() || "";
    if (!projectId || !name) {
      alert("Укажите название проекта");
      return;
    }
    const res = await fetch(`/api/projects/${projectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      alert(typeof data.detail === "string" ? data.detail : "Не удалось сохранить");
      return;
    }
    location.reload();
  });

  const roadmapModal = document.getElementById("roadmap-preview-modal");
  const roadmapLoading = document.getElementById("roadmap-preview-loading");
  const roadmapEmpty = document.getElementById("roadmap-preview-empty");
  const roadmapTableWrap = document.getElementById("roadmap-preview-table-wrap");
  const roadmapThead = document.getElementById("roadmap-preview-thead");
  const roadmapTbody = document.getElementById("roadmap-preview-tbody");
  const roadmapMeta = document.getElementById("roadmap-preview-meta");
  const roadmapWarnings = document.getElementById("roadmap-preview-warnings");
  const roadmapTitle = document.getElementById("roadmap-preview-title");
  const roadmapRefreshBtn = document.getElementById("roadmap-preview-refresh");
  const roadmapSaveBtn = document.getElementById("roadmap-preview-save");

  let roadmapProjectId = null;
  let roadmapPreviewData = null;

  function closeRoadmapModal() {
    roadmapModal?.classList.add("hidden");
    roadmapProjectId = null;
    roadmapPreviewData = null;
  }

  function openRoadmapModal() {
    roadmapModal?.classList.remove("hidden");
  }

  function escHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatRoadmapMeta(data) {
    const meta = data.meta || {};
    const parts = [];
    if (data.saved && data.saved_at) {
      parts.push(`В базе: ${new Date(data.saved_at).toLocaleString("ru-RU")}`);
    } else if (roadmapPreviewData) {
      parts.push("Превью из Google (не сохранено)");
    }
    if (meta.rows_returned != null) parts.push(`Строк: ${meta.rows_returned}`);
    if ((meta.sources_used || []).length) {
      parts.push(`Источники: ${meta.sources_used.join(", ")}`);
    }
    return parts.join(" · ");
  }

  function renderRoadmapTable(data) {
    const columns = data.columns || [];
    const rows = data.rows || [];
    const meta = data.meta || {};

    if (roadmapMeta) roadmapMeta.textContent = formatRoadmapMeta(data);

    const warnings = meta.warnings || [];
    if (warnings.length && roadmapWarnings) {
      roadmapWarnings.classList.remove("hidden");
      roadmapWarnings.innerHTML = warnings.map((w) => `<div>• ${escHtml(w)}</div>`).join("");
    } else {
      roadmapWarnings?.classList.add("hidden");
    }

    roadmapEmpty?.classList.add("hidden");
    roadmapTableWrap?.classList.add("hidden");

    if (!rows.length) {
      roadmapEmpty?.classList.remove("hidden");
      if (roadmapTbody) roadmapTbody.innerHTML = "";
      return;
    }

    if (roadmapThead) {
      roadmapThead.innerHTML = `<tr>${columns
        .map((c) => `<th class="text-left p-3 font-semibold whitespace-nowrap">${escHtml(c)}</th>`)
        .join("")}</tr>`;
    }
    if (roadmapTbody) {
      roadmapTbody.innerHTML = rows
        .map((row) => {
          const cells = columns
            .map((col) => `<td class="p-3 align-top max-w-xs break-words">${escHtml(row[col] ?? "")}</td>`)
            .join("");
          return `<tr class="border-t border-slate-100">${cells}</tr>`;
        })
        .join("");
    }
    roadmapTableWrap?.classList.remove("hidden");
  }

  function setRoadmapLoading(on) {
    if (on) roadmapLoading?.classList.remove("hidden");
    else roadmapLoading?.classList.add("hidden");
  }

  async function openRoadmapForProject(projectId) {
    roadmapProjectId = projectId;
    openRoadmapModal();
    setRoadmapLoading(true);
    roadmapEmpty?.classList.add("hidden");
    roadmapTableWrap?.classList.add("hidden");
    roadmapWarnings?.classList.add("hidden");
    if (roadmapTbody) roadmapTbody.innerHTML = "";
    if (roadmapMeta) roadmapMeta.textContent = "";

    try {
      const res = await fetch(`/api/projects/${projectId}/roadmap`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(typeof data.detail === "string" ? data.detail : "Не удалось загрузить");
        closeRoadmapModal();
        return;
      }
      if (roadmapTitle) {
        roadmapTitle.textContent = `Таблица roadmap — ${data.project_name || ""}`;
      }
      renderRoadmapTable(data);
    } finally {
      setRoadmapLoading(false);
    }
  }

  document.querySelectorAll(".btn-preview-roadmap").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const projectId = btn.dataset.projectId;
      if (!projectId) return;
      btn.disabled = true;
      try {
        await openRoadmapForProject(projectId);
      } finally {
        btn.disabled = false;
      }
    });
  });

  roadmapRefreshBtn?.addEventListener("click", async () => {
    if (!roadmapProjectId) return;
    roadmapRefreshBtn.disabled = true;
    setRoadmapLoading(true);
    try {
      const res = await fetch(`/api/projects/${roadmapProjectId}/roadmap-table`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(typeof data.detail === "string" ? data.detail : "Не удалось загрузить из Google");
        return;
      }
      roadmapPreviewData = data;
      renderRoadmapTable(data);
    } finally {
      setRoadmapLoading(false);
      roadmapRefreshBtn.disabled = false;
    }
  });

  async function pollJob(jobId, { intervalMs = 2000, timeoutMs = 5 * 60 * 1000 } = {}) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = await res.json();
      if (data.status === "done") return data;
      if (data.status === "error") throw new Error(data.error || "Ошибка задачи");
      await new Promise((r) => setTimeout(r, intervalMs));
    }
    throw new Error("Таймаут ожидания задачи");
  }

  roadmapSaveBtn?.addEventListener("click", async () => {
    if (!roadmapProjectId) return;
    roadmapSaveBtn.disabled = true;
    setRoadmapLoading(true);
    if (roadmapMeta) roadmapMeta.textContent = "Сохранение запущено в фоне...";
    try {
      const res = await fetch(`/api/projects/${roadmapProjectId}/roadmap/save`, { method: "POST" });
      const startData = await res.json().catch(() => ({}));
      if (!res.ok) {
        alert(typeof startData.detail === "string" ? startData.detail : "Не удалось поставить задачу");
        return;
      }
      if (window.JobsWidget) window.JobsWidget.refresh();
      const done = await pollJob(startData.job_id);
      const data = done.result || {};
      roadmapPreviewData = null;
      renderRoadmapTable(data);
      alert("Roadmap сохранён в базу");
      if (window.JobsWidget) window.JobsWidget.refresh();
    } catch (e) {
      alert(e.message || "Не удалось сохранить");
    } finally {
      setRoadmapLoading(false);
      roadmapSaveBtn.disabled = false;
    }
  });

  document.getElementById("roadmap-preview-close")?.addEventListener("click", closeRoadmapModal);
  document.getElementById("roadmap-preview-backdrop")?.addEventListener("click", closeRoadmapModal);

  const milestoneRowTemplate = document.getElementById("milestone-row-template");

  function bindMilestoneRow(row) {
    row.querySelector(".btn-del-milestone")?.addEventListener("click", () => {
      row.remove();
    });
  }

  function collectMilestoneItems(form) {
    const items = [];
    form.querySelectorAll(".milestones-tbody tr").forEach((tr, idx) => {
      const code = (tr.querySelector('[name="code"]')?.value || "").trim();
      const title = (tr.querySelector('[name="title"]')?.value || "").trim();
      const description = (tr.querySelector('[name="description"]')?.value || "").trim();
      const deadline = (tr.querySelector('[name="deadline"]')?.value || "").trim();
      if (!code && !title && !description && !deadline) return;
      const item = { code, title, description, deadline, sort_order: idx };
      if (tr.dataset.id) item.id = tr.dataset.id;
      items.push(item);
    });
    return items;
  }

  function setMilestoneStatus(form, text, isError) {
    const el = form.querySelector(".milestones-status");
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("hidden", !text);
    el.classList.toggle("text-red-600", Boolean(isError));
    el.classList.toggle("text-slate-500", !isError);
  }

  document.querySelectorAll(".form-milestones").forEach((form) => {
    form.querySelectorAll(".milestones-tbody tr").forEach(bindMilestoneRow);
    form.querySelector(".btn-add-milestone")?.addEventListener("click", () => {
      const tbody = form.querySelector(".milestones-tbody");
      if (!tbody || !milestoneRowTemplate) return;
      const node = milestoneRowTemplate.content.firstElementChild.cloneNode(true);
      const n = tbody.querySelectorAll("tr").length + 1;
      const codeInput = node.querySelector('[name="code"]');
      if (codeInput && !codeInput.value) codeInput.value = `M${n}`;
      bindMilestoneRow(node);
      tbody.appendChild(node);
      codeInput?.focus();
    });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const projectId = form.dataset.projectId;
      if (!projectId) return;
      const btn = form.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      setMilestoneStatus(form, "Сохранение…", false);
      try {
        const res = await fetch(`/api/projects/${projectId}/milestones`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: collectMilestoneItems(form) }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const detail = typeof data.detail === "string" ? data.detail : "Не удалось сохранить milestones";
          setMilestoneStatus(form, detail, true);
          return;
        }
        location.reload();
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  document.querySelectorAll(".btn-delete-project").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Удалить проект и все источники?")) return;
      const res = await fetch(`/api/projects/${btn.dataset.projectId}`, { method: "DELETE" });
      if (res.ok) location.reload();
    });
  });
})();
