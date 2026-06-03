(() => {
  const reportId = document.getElementById("report-id")?.value;
  if (!reportId) return;

  const previewPane = document.getElementById("preview-pane");
  const editPane = document.getElementById("edit-pane");
  const btnSave = document.getElementById("btn-save");
  const btnCopy = document.getElementById("btn-copy");
  const btnDelete = document.getElementById("btn-delete-report");
  const saveError = document.getElementById("save-error");
  const syncMeetingsList = document.getElementById("sync-meetings-list");
  const syncForm = document.getElementById("report-sync-form");
  const syncTranscript = document.getElementById("sync-transcript");
  const syncTitle = document.getElementById("sync-title");
  const syncDate = document.getElementById("sync-date");
  const syncModel = document.getElementById("sync-model");
  const syncError = document.getElementById("sync-error");
  const syncLoader = document.getElementById("sync-loader");
  const syncSubmit = document.getElementById("sync-submit");

  let syncMeetings = [];

  function showSaveError(msg) {
    if (!saveError) return;
    saveError.textContent = msg;
    saveError.classList.remove("hidden");
  }

  function hideSaveError() {
    saveError?.classList.add("hidden");
  }

  function renderMarkdown(text) {
    if (typeof marked !== "undefined") {
      previewPane.innerHTML = marked.parse(text || "");
    } else {
      previewPane.textContent = text || "";
    }
  }

  function getEditorText() {
    return editPane?.value || "";
  }

  function applyEditorContent(text) {
    editPane.value = text || "";
    renderMarkdown(text);
  }

  document.querySelectorAll(".report-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll(".report-tab").forEach((b) => {
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
        renderMarkdown(getEditorText());
      }
    });
  });

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatMeetingDate(meeting) {
    if (meeting.meeting_at) return meeting.meeting_at.slice(0, 10);
    if (meeting.created_at) return meeting.created_at.slice(0, 10);
    return "";
  }

  function renderMeetingTasks(tasks) {
    if (!tasks?.length) {
      return '<p class="text-xs text-slate-500">Задачи не извлечены</p>';
    }
    return `<ul class="space-y-2 mt-2">${tasks.map((t) => `
      <li class="flex items-start gap-2 text-sm">
        <input type="checkbox" class="sync-task-check mt-0.5 rounded border-slate-300" data-task-id="${t.id}" ${t.status === "done" ? "checked" : ""} />
        <span class="sync-task-label ${t.status === "done" ? "line-through text-slate-400" : "text-ink"}">
          <span class="font-medium">${escapeHtml(t.title)}</span>
          ${t.description ? `<span class="block text-slate-500 text-xs">${escapeHtml(t.description)}</span>` : ""}
        </span>
      </li>`).join("")}</ul>`;
  }

  function bindTaskCheckboxes(root) {
    root.querySelectorAll(".sync-task-check").forEach((cb) => {
      cb.addEventListener("change", async () => {
        const id = cb.dataset.taskId;
        const status = cb.checked ? "done" : "open";
        const res = await fetch(`/api/sync-tasks/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status }),
        });
        if (!res.ok) {
          cb.checked = !cb.checked;
          return;
        }
        for (const meeting of syncMeetings) {
          const task = meeting.tasks?.find((t) => String(t.id) === String(id));
          if (task) task.status = status;
        }
      });
    });
  }

  function bindMeetingActions(root) {
    root.querySelectorAll("[data-delete-meeting]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const meetingId = btn.dataset.deleteMeeting;
        if (!confirm("Удалить эту встречу и все её задачи?")) return;
        const res = await fetch(`/api/sync-meetings/${meetingId}`, { method: "DELETE" });
        if (!res.ok) {
          alert("Не удалось удалить");
          return;
        }
        await loadReport();
      });
    });

    root.querySelectorAll("[data-toggle-transcript]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const block = btn.closest("[data-meeting-card]")?.querySelector("[data-transcript]");
        if (!block) return;
        block.classList.toggle("hidden");
        btn.textContent = block.classList.contains("hidden") ? "Показать текст" : "Скрыть текст";
      });
    });
  }

  function renderMeetingsList(meetings) {
    syncMeetings = meetings || [];
    if (!syncMeetingsList) return;

    if (!syncMeetings.length) {
      syncMeetingsList.innerHTML = '<p class="text-sm text-slate-500">Встреч пока нет — добавьте расшифровку ниже</p>';
      return;
    }

    syncMeetingsList.innerHTML = syncMeetings.map((m) => {
      const date = formatMeetingDate(m);
      const preview = (m.transcript || "").slice(0, 120);
      return `
        <article data-meeting-card class="rounded-xl border border-slate-200 bg-slate-50/50 p-4" data-meeting-id="${m.id}">
          <div class="flex items-start justify-between gap-2">
            <div>
              <h3 class="text-sm font-bold text-ink">${escapeHtml(m.title || "Созвон / синк")}</h3>
              <p class="text-xs text-slate-500 mt-0.5">${date ? `${date} · ` : ""}${(m.tasks || []).length} задач</p>
            </div>
            <div class="flex gap-1 shrink-0">
              <button type="button" class="btn-ghost text-xs !py-1" data-toggle-transcript>Показать текст</button>
              <button type="button" class="btn-ghost text-xs !py-1 text-red-600" data-delete-meeting="${m.id}">Удалить</button>
            </div>
          </div>
          <pre data-transcript class="hidden mt-2 text-xs text-slate-600 whitespace-pre-wrap max-h-40 overflow-auto bg-white rounded-lg p-2 border border-slate-100">${escapeHtml(m.transcript || preview)}</pre>
          ${renderMeetingTasks(m.tasks)}
        </article>`;
    }).join("");

    syncMeetingsList.insertAdjacentHTML(
      "beforeend",
      `<a href="/sync-tasks#report-${reportId}" class="text-xs font-semibold text-ink underline inline-block">Все задачи отчёта →</a>`,
    );

    bindTaskCheckboxes(syncMeetingsList);
    bindMeetingActions(syncMeetingsList);
  }

  async function loadReport() {
    const res = await fetch(`/api/reports/${reportId}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Не удалось загрузить отчёт");
    applyEditorContent(data.generated_text || "");
    renderMeetingsList(data.sync_meetings || []);
  }

  btnSave?.addEventListener("click", async () => {
    const text = getEditorText().trim();
    if (!text) {
      showSaveError("Текст не может быть пустым");
      return;
    }
    hideSaveError();
    btnSave.disabled = true;
    try {
      const res = await fetch(`/api/reports/${reportId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Ошибка сохранения");
      applyEditorContent(data.generated_text || text);
      btnSave.textContent = "Сохранено";
      setTimeout(() => { btnSave.textContent = "Сохранить изменения"; }, 2000);
    } catch (e) {
      showSaveError(e.message);
    } finally {
      btnSave.disabled = false;
    }
  });

  btnCopy?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(getEditorText() || previewPane.innerText);
    btnCopy.textContent = "Скопировано!";
    setTimeout(() => { btnCopy.textContent = "Копировать"; }, 2000);
  });

  async function deleteReport() {
    const res = await fetch(`/api/reports/${reportId}`, { method: "DELETE" });
    if (!res.ok) throw new Error(await res.text());
    window.location.href = "/history";
  }

  btnDelete?.addEventListener("click", () => {
    if (!confirm("Удалить отчёт и все связанные встречи и задачи синка?")) return;
    deleteReport().catch((e) => alert(e.message));
  });

  async function pollJob(jobId) {
    for (let i = 0; i < 300; i += 1) {
      const res = await fetch(`/api/jobs/${jobId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.status === "done") return data;
      if (data.status === "error") throw new Error(data.error || "Ошибка");
      await new Promise((r) => setTimeout(r, 2000));
    }
    throw new Error("Таймаут");
  }

  function clearSyncForm() {
    syncTitle.value = "";
    syncDate.value = "";
    syncTranscript.value = "";
  }

  syncForm?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const transcript = syncTranscript?.value?.trim();
    if (!transcript) {
      syncError.textContent = "Вставьте расшифровку";
      syncError.classList.remove("hidden");
      return;
    }
    const body = {
      title: syncTitle?.value || "",
      transcript,
      model_key: syncModel?.value,
    };
    if (syncDate?.value) body.meeting_at = `${syncDate.value}T12:00:00`;

    syncError?.classList.add("hidden");
    syncLoader?.classList.remove("hidden");
    syncSubmit.disabled = true;
    try {
      const res = await fetch(`/api/reports/${reportId}/sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const start = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(start.detail || "Ошибка");
      if (window.JobsWidget) window.JobsWidget.refresh();
      await pollJob(start.job_id);
      clearSyncForm();
      await loadReport();
      if (window.JobsWidget) window.JobsWidget.refresh();
    } catch (err) {
      syncError.textContent = err.message;
      syncError.classList.remove("hidden");
    } finally {
      syncLoader?.classList.add("hidden");
      syncSubmit.disabled = false;
    }
  });

  loadReport().catch((e) => {
    previewPane.textContent = e.message;
  });
})();
