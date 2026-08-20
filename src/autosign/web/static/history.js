window.createHistoryUi = function createHistoryUi({api, element, showNotice, state}) {
  const historyContainer = document.querySelector("#execution-history");
  const detailDialog = document.querySelector("#execution-detail-dialog");
  const detailSummary = document.querySelector("#execution-detail-summary");
  const detailMessage = document.querySelector("#execution-detail-message");
  const detailJson = document.querySelector("#execution-detail-json");

  function statusText(status) {
    return {
      success: "成功",
      already_signed: "今日已签到",
      failed: "失败",
      interaction_required: "需要登录"
    }[status] || status;
  }

  function openDetail(record) {
    detailSummary.textContent =
      `${record.account_label} · ${record.plugin_id} · ${statusText(record.status)}`;
    detailMessage.textContent = record.message;
    detailJson.textContent = JSON.stringify({
      执行时间: new Date(record.started_at).toLocaleString("zh-CN", {hour12: false}),
      验证通过: record.verified,
      耗时毫秒: record.duration_ms,
      详细结果: record.details
    }, null, 2);
    detailDialog.showModal();
  }

  function render() {
    if (!state.executions.length) {
      historyContainer.replaceChildren(
        element("p", "muted", "还没有签到记录。点击账户卡片中的“执行账户签到”开始测试。")
      );
      return;
    }
    historyContainer.replaceChildren(...state.executions.map((record) => {
      const item = element("article", "history-item");
      const identity = element("div");
      identity.append(
        element("strong", "", record.account_label),
        element("div", "meta",
          new Date(record.started_at).toLocaleString("zh-CN", {hour12: false}))
      );
      const status = element(
        "span",
        `badge ${record.status}`,
        statusText(record.status)
      );
      const message = element("div", "history-message", record.message);
      const detail = element("button", "secondary", "详情");
      detail.addEventListener("click", () => openDetail(record));
      item.append(identity, status, message, detail);
      return item;
    }));
  }

  async function load() {
    state.executions = await api("/api/v1/executions?limit=6");
    render();
  }

  document.querySelector("#history-refresh").addEventListener("click", async () => {
    try {
      await load();
      showNotice("已刷新最近签到记录");
    } catch (error) {
      showNotice(error.message, true);
    }
  });
  document.querySelector("#execution-detail-close").addEventListener(
    "click", () => detailDialog.close()
  );
  document.querySelector("#execution-detail-done").addEventListener(
    "click", () => detailDialog.close()
  );
  detailDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    detailDialog.close();
  });

  return {load, render};
};
