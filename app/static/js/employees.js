(() => {
  const editModal = document.getElementById("edit-modal");

  const buildEmployeeFormData = (form) => {
    const fd = new FormData();
    for (const [key, value] of new FormData(form).entries()) {
      if (key === "employee_id") {
        fd.append(key, value);
        continue;
      }
      const trimmed = String(value).trim();
      if (trimmed) fd.append(key, trimmed);
    }
    return fd;
  };

  document.getElementById("form-new-employee")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const res = await fetch("/api/employees", { method: "POST", body: buildEmployeeFormData(e.target) });
    if (res.ok) location.reload();
    else alert(await res.text());
  });

  document.getElementById("form-edit-employee")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = buildEmployeeFormData(e.target);
    const id = fd.get("employee_id");
    const res = await fetch(`/api/employees/${id}`, { method: "PUT", body: fd });
    if (res.ok) location.reload();
    else alert(await res.text());
  });

  document.querySelectorAll(".btn-edit-employee").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById("edit-employee-id").value = btn.dataset.employeeId;
      document.getElementById("edit-full-name").value = btn.dataset.name;
      document.getElementById("edit-yandex-email").value = btn.dataset.email;
      editModal.classList.remove("hidden");
    });
  });

  document.getElementById("edit-cancel")?.addEventListener("click", () => editModal.classList.add("hidden"));
  document.getElementById("edit-backdrop")?.addEventListener("click", () => editModal.classList.add("hidden"));

  document.querySelectorAll(".btn-test-employee").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const res = await fetch(`/api/employees/${btn.dataset.employeeId}/test-mail`, { method: "POST" });
        const data = await res.json();
        alert(data.message);
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll(".btn-delete-employee").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Удалить сотрудника?")) return;
      const res = await fetch(`/api/employees/${btn.dataset.employeeId}`, { method: "DELETE" });
      if (res.ok) location.reload();
    });
  });
})();
