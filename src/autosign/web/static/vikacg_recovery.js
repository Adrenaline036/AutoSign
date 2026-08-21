(() => {
  window.createVikacgRecovery = ({api, loadAccounts, openBrowserLogin, showNotice}) => {
    const recoveryDialog = document.querySelector("#vikacg-recovery-dialog");
    const recoveryAccount = document.querySelector("#vikacg-recovery-account");
    const importDialog = document.querySelector("#vikacg-import-dialog");
    const importAccount = document.querySelector("#vikacg-import-account");
    const importForm = document.querySelector("#vikacg-import-form");
    const importValue = document.querySelector("#vikacg-import-value");
    const importError = document.querySelector("#vikacg-import-error");
    const importSubmit = document.querySelector("#vikacg-import-submit");
    const importToggle = document.querySelector("#vikacg-import-toggle");
    let recoveryTarget = null;
    let importTarget = null;
    let importConfirmed = false;

    function resetImport() {
      importForm.reset();
      importValue.disabled = false;
      importValue.type = "password";
      importToggle.textContent = "显示内容";
      importError.textContent = "";
      importConfirmed = false;
      importSubmit.textContent = "验证导入内容";
      importSubmit.disabled = false;
    }

    function open(account) {
      recoveryTarget = account;
      resetImport();
      recoveryAccount.textContent = `账户：${account.label}`;
      recoveryDialog.showModal();
    }

    document.querySelector("#vikacg-recovery-close").addEventListener(
      "click", () => recoveryDialog.close()
    );
    document.querySelector("#vikacg-open-browser").addEventListener("click", () => {
      const account = recoveryTarget;
      recoveryDialog.close();
      if (account) openBrowserLogin(account);
    });
    document.querySelector("#vikacg-show-import").addEventListener("click", () => {
      const account = recoveryTarget;
      if (!account) return;
      importTarget = account;
      resetImport();
      importAccount.textContent = `账户：${account.label}`;
      if (!account.secret_names.includes("browser_storage_state")) {
        importError.textContent = "此账户还没有基础登录状态，请先完成一次实时浏览器登录。";
        importValue.disabled = true;
        importSubmit.disabled = true;
      }
      recoveryDialog.close();
      importDialog.showModal();
      window.setTimeout(() => importValue.focus(), 0);
    });
    importToggle.addEventListener("click", (event) => {
      const showing = importValue.type === "text";
      importValue.type = showing ? "password" : "text";
      event.currentTarget.textContent = showing ? "显示内容" : "隐藏内容";
    });
    document.querySelector("#vikacg-import-close").addEventListener(
      "click", () => importDialog.close()
    );
    document.querySelector("#vikacg-import-cancel").addEventListener(
      "click", () => importDialog.close()
    );
    importForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!importTarget || !importValue.value) return;
      importError.textContent = "";
      importSubmit.disabled = true;
      try {
        const result = await api(
          `/api/v1/accounts/${importTarget.id}/vikacg-state-import`,
          {
            method: "POST",
            body: JSON.stringify({
              raw_json: importValue.value,
              confirm_overwrite: importConfirmed
            })
          }
        );
        importValue.value = "";
        importDialog.close();
        await loadAccounts();
        showNotice(
          result.token_refreshed
            ? "VikACG 登录状态已刷新、验证并加密保存"
            : "VikACG 登录状态已验证并加密保存"
        );
      } catch (error) {
        if (error.status === 409 && !importConfirmed) {
          importConfirmed = true;
          importError.textContent =
            "即将覆盖此账户当前保存的 VikACG 令牌。请再次点击确认；验证失败时旧状态不会改变。";
          importSubmit.textContent = "确认覆盖并验证";
        } else {
          importError.textContent = error.message;
        }
      } finally {
        importSubmit.disabled = false;
      }
    });
    recoveryDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      recoveryDialog.close();
    });
    recoveryDialog.addEventListener("close", () => {
      recoveryTarget = null;
    });
    importDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      importDialog.close();
    });
    importDialog.addEventListener("close", () => {
      resetImport();
      importTarget = null;
    });

    return Object.freeze({open});
  };
})();
