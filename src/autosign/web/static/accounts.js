window.createAccountsUi = function createAccountsUi({
  api,
  element,
  icons,
  loadAccounts,
  loadExecutions,
  openBrowserLogin,
  openChannelAssignment,
  openScheduleDialog,
  openVikacgRecovery,
  scheduleForAccount,
  showNotice,
  statusBadge,
  state
}) {
  const accountContainer = document.querySelector("#accounts");
  const deleteDialog = document.querySelector("#delete-account-dialog");
  const deleteForm = document.querySelector("#delete-account-form");
  const deleteAccountName = document.querySelector("#delete-account-name");
  const deleteConfirmation = document.querySelector("#delete-confirmation");
  const deleteSubmitButton = document.querySelector("#delete-dialog-submit");
  const pluginSelect = document.querySelector("#plugin-select");
  let deleteAccountId = null;
  let deleteExpectedLabel = null;

  function loginBadge(loggedIn) {
    return element(
      "span",
      `badge ${loggedIn ? "ok" : "missing"}`,
      loggedIn ? "已登录" : "未登录"
    );
  }

  function supportsInteractiveLogin(account) {
    const plugin = state.plugins.find((candidate) => candidate.id === account.plugin_id);
    return Boolean(plugin && plugin.capabilities.includes("interactive_login"));
  }

  function openDeleteDialog(account) {
    deleteAccountId = account.id;
    deleteExpectedLabel = account.label;
    deleteForm.reset();
    deleteAccountName.textContent = account.label;
    deleteDialog.showModal();
    window.setTimeout(() => deleteConfirmation.focus(), 0);
  }

  function render() {
    if (!state.accounts.length) {
      accountContainer.replaceChildren(
        element("p", "muted", "还没有账户，请先创建一个 Demo 账户。")
      );
      return;
    }
    accountContainer.replaceChildren(...state.accounts.map((account) => {
      const card = element(
        "article",
        `card account-card${account.enabled ? "" : " disabled"}`
      );
      const title = element("h3", "", account.label);
      const status = element(
        "span",
        `badge ${account.enabled ? "ok" : "missing"}`,
        account.enabled ? "已启用" : "已禁用"
      );
      const meta = element("div", "meta", `${account.plugin_id} · ${account.id}`);
      const badges = element("div", "status-badges");
      badges.append(status);
      if (supportsInteractiveLogin(account)) {
        badges.append(loginBadge(account.secret_names.includes("browser_storage_state")));
      }
      badges.append(
        statusBadge("Uptime Kuma", account.monitor_configured),
        statusBadge("QQ通知", account.napcat_configured)
      );
      const schedule = scheduleForAccount(account.id);
      const scheduleMeta = element("div", "schedule-meta");
      if (schedule) {
        const next = schedule.next_run_at
          ? new Date(schedule.next_run_at).toLocaleString("zh-CN", {hour12: false})
          : "暂停";
        scheduleMeta.append(
          element(
            "span",
            "",
            `自动签到：${schedule.enabled ? `${schedule.daily_time} 后随机延迟` : "已暂停"}`
          ),
          element("span", "", `下次签到：${next}`)
        );
      } else {
        scheduleMeta.append(element("span", "", "自动签到：未设置"));
      }
      const actions = element("div", "actions account-actions");

      if (supportsInteractiveLogin(account)) {
        const isVikacg = account.plugin_id === "vikacg";
        const login = element(
          "button",
          "login-action",
          isVikacg ? "登录与恢复" : "交互登录"
        );
        login.addEventListener(
          "click",
          () => isVikacg ? openVikacgRecovery(account) : openBrowserLogin(account)
        );
        actions.append(login);
      }

      const execute = element("button", "execute-action", "执行账户签到");
      execute.addEventListener("click", async () => {
        execute.disabled = true;
        try {
          const result = await api(`/api/v1/accounts/${account.id}/execute`, {
            method: "POST",
            body: "{}"
          });
          showNotice(`${result.message}；验证：${result.verified ? "通过" : "未通过"}`);
          await loadExecutions();
        } catch (error) {
          showNotice(error.message, true);
        } finally {
          execute.disabled = false;
        }
      });

      const toggle = element(
        "button",
        `corner-button power-button${account.enabled ? " enabled" : ""}`
      );
      toggle.type = "button";
      toggle.innerHTML = icons.power;
      toggle.title = account.enabled ? "禁用账户" : "启用账户";
      toggle.setAttribute("aria-label", toggle.title);
      toggle.addEventListener("click", async () => {
        try {
          await api(`/api/v1/accounts/${account.id}`, {
            method: "PATCH",
            body: JSON.stringify({enabled: !account.enabled})
          });
          await loadAccounts();
          showNotice(`账户已${account.enabled ? "禁用" : "启用"}`);
        } catch (error) {
          showNotice(error.message, true);
        }
      });

      const editSchedule = element("button", "schedule-action", "自动签到计划");
      editSchedule.addEventListener("click", () => openScheduleDialog(account));

      const pushSettings = element("button", "push-action", "消息推送设置");
      pushSettings.addEventListener("click", () => openChannelAssignment(account));

      const remove = element("button", "inline-delete");
      remove.type = "button";
      remove.innerHTML = icons.trash;
      remove.title = "删除账户";
      remove.setAttribute("aria-label", "删除账户");
      remove.addEventListener("click", () => openDeleteDialog(account));

      actions.append(execute, editSchedule, pushSettings, remove);
      card.append(title, meta, badges, scheduleMeta, actions, toggle);
      return card;
    }));
  }

  document.querySelector("#account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const labelInput = document.querySelector("#account-label");
    try {
      await api("/api/v1/accounts", {
        method: "POST",
        body: JSON.stringify({
          plugin_id: pluginSelect.value,
          label: labelInput.value,
          enabled: true,
          settings: {}
        })
      });
      labelInput.value = "";
      await loadAccounts();
      showNotice("账户已创建并写入数据库");
    } catch (error) {
      showNotice(error.message, true);
    }
  });

  document.querySelector("#delete-dialog-close").addEventListener(
    "click", () => deleteDialog.close()
  );
  document.querySelector("#delete-dialog-cancel").addEventListener(
    "click", () => deleteDialog.close()
  );

  deleteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!deleteAccountId || !deleteExpectedLabel) return;
    deleteSubmitButton.disabled = true;
    try {
      await api(`/api/v1/accounts/${deleteAccountId}/delete`, {
        method: "POST",
        body: JSON.stringify({confirm_label: deleteConfirmation.value})
      });
      deleteDialog.close();
      await loadAccounts();
      showNotice("账户及其关联数据已删除");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      deleteSubmitButton.disabled = false;
    }
  });

  deleteDialog.addEventListener("close", () => {
    deleteForm.reset();
    deleteAccountName.textContent = "";
    deleteAccountId = null;
    deleteExpectedLabel = null;
  });

  return {render};
};
