(() => {
  document.querySelectorAll(".sync-task-check").forEach((cb) => {
    cb.addEventListener("change", async () => {
      const id = cb.dataset.taskId;
      const status = cb.checked ? "done" : "open";
      const row = cb.closest(".sync-task-row");
      const titleEl = row?.querySelector(".sync-task-title");
      const res = await fetch(`/api/sync-tasks/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      if (!res.ok) {
        cb.checked = !cb.checked;
        return;
      }
      if (titleEl) {
        titleEl.classList.toggle("line-through", cb.checked);
        titleEl.classList.toggle("text-slate-400", cb.checked);
        titleEl.classList.toggle("text-ink", !cb.checked);
      }
    });
  });

  document.querySelectorAll(".btn-delete-task").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Удалить задачу?")) return;
      const id = btn.dataset.taskId;
      const res = await fetch(`/api/sync-tasks/${id}`, { method: "DELETE" });
      if (!res.ok) return;
      btn.closest(".sync-task-row")?.remove();
      const section = btn.closest("section");
      const list = section?.querySelector("ul");
      if (list && !list.children.length) {
        list.outerHTML = '<p class="px-6 py-8 text-sm text-slate-500 text-center">Все задачи удалены</p>';
      }
    });
  });
})();
