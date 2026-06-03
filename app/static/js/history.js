(() => {
  async function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function startGammaForReport(id) {
    const res = await fetch(`/api/reports/${id}/gamma`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ num_cards: 10, export_as: "pptx" }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Не удалось запустить генерацию Gamma");
    return data.generation_id;
  }

  async function waitGammaResult(generationId) {
    for (let i = 0; i < 90; i += 1) {
      const res = await fetch(`/api/gamma/generations/${generationId}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || "Ошибка проверки статуса Gamma");
      if (data.status === "completed") return data;
      if (data.status === "failed") {
        throw new Error(data.raw?.error?.message || "Gamma не смог сгенерировать презентацию");
      }
      await sleep(5000);
    }
    throw new Error("Gamma генерирует слишком долго, попробуйте позже");
  }

  async function saveGammaLinks(reportId, generationId, result) {
    const res = await fetch(`/api/reports/${reportId}/gamma`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        generation_id: generationId,
        gamma_url: result.gamma_url || null,
        export_url: result.export_url || null,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "Не удалось сохранить ссылку Gamma");
    }
  }

  function upsertGammaCell(reportId, result) {
    const btn = document.querySelector(`.btn-gamma-report[data-report-id="${reportId}"]`);
    const row = btn?.closest("tr");
    if (!row) return;
    const cells = row.querySelectorAll("td");
    const gammaCell = cells[5];
    const gammaUrl = result.gamma_url || "";
    const exportUrl = result.export_url || "";
    if (!gammaCell) return;
    if (gammaUrl) {
      gammaCell.innerHTML = `<a href="${gammaUrl}" target="_blank" rel="noopener noreferrer" class="text-xs font-semibold text-indigo-700 hover:underline">Открыть Gamma</a>`;
      return;
    }
    if (exportUrl) {
      gammaCell.innerHTML = `<a href="${exportUrl}" target="_blank" rel="noopener noreferrer" class="text-xs font-semibold text-indigo-700 hover:underline">Скачать .pptx</a>`;
      return;
    }
  }

  document.querySelectorAll(".btn-gamma-report").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.reportId;
      btn.disabled = true;
      const oldText = btn.textContent;
      btn.textContent = "Старт...";
      try {
        const generationId = await startGammaForReport(id);
        btn.textContent = "Gamma генерирует...";
        const result = await waitGammaResult(generationId);
        await saveGammaLinks(id, generationId, result);
        upsertGammaCell(id, result);
        const url = result.gamma_url || result.export_url;
        if (url) {
          window.open(url, "_blank", "noopener,noreferrer");
        }
        btn.textContent = "Готово";
        setTimeout(() => {
          btn.textContent = oldText;
          btn.disabled = false;
        }, 1500);
      } catch (err) {
        alert(err.message || "Не удалось сгенерировать презентацию в Gamma");
        btn.textContent = oldText;
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll(".btn-delete-report").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const id = btn.dataset.reportId;
      const name = btn.dataset.reportName || "отчёт";
      if (!confirm(`Удалить «${name}» и все связанные задачи синка?`)) return;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/reports/${id}`, { method: "DELETE" });
        if (!res.ok) throw new Error(await res.text());
        btn.closest("tr")?.remove();
      } catch (err) {
        alert(err.message || "Не удалось удалить");
        btn.disabled = false;
      }
    });
  });
})();
