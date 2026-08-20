window.createBrowserUi = function createBrowserUi({api, loadAccounts, showNotice}) {
  const browserDialog = document.querySelector("#browser-login-dialog");
  const browserDialogAccount = document.querySelector("#browser-dialog-account");
  const browserPageTitle = document.querySelector("#browser-page-title");
  const browserWebHelp = document.querySelector("#browser-web-help");
  const browserNativeHelp = document.querySelector("#browser-native-help");
  const browserLiveOpen = document.querySelector("#browser-live-open");
  const browserSave = document.querySelector("#browser-save");
  const browserDiscard = document.querySelector("#browser-discard");
  const forceSaveDialog = document.querySelector("#force-browser-save-dialog");
  let activeSession = null;
  let pollTimer = null;
  let closing = false;
  let activeWindow = null;

  function scheduleStatusRefresh(delay = 2000) {
    window.clearTimeout(pollTimer);
    if (!activeSession) return;
    pollTimer = window.setTimeout(refreshInfo, delay);
  }

  function openLiveWindow() {
    if (!activeSession) return null;
    if (!activeSession.live_url) {
      api(`/api/v1/browser-sessions/${activeSession.id}/focus`, {
        method: "POST", body: "{}"
      }).catch((error) => showNotice(error.message, true));
      return null;
    }
    const liveUrl = new URL(activeSession.live_url, window.location.href).href;
    activeWindow = window.open(liveUrl, "_blank");
    if (!activeWindow) {
      showNotice("浏览器阻止了新标签，请允许此站点打开弹出窗口后重试", true);
    }
    return activeWindow;
  }

  function showInteractionMode() {
    const nativeWindow = Boolean(activeSession && !activeSession.live_url);
    browserPageTitle.textContent = nativeWindow
      ? "普通 Chrome 登录窗口已打开（尚未接管）"
      : "实时浏览器已在独立标签中打开";
    browserWebHelp.hidden = nativeWindow;
    browserNativeHelp.hidden = !nativeWindow;
    browserLiveOpen.textContent = nativeWindow
      ? "Chrome 窗口已打开（使用 Alt+Tab 切换）"
      : "打开或重新打开实时浏览器";
  }

  function handleSessionError(error) {
    showNotice(error.message, true);
    if (error.status !== 404) return false;
    activeSession = null;
    window.clearTimeout(pollTimer);
    if (activeWindow && !activeWindow.closed) activeWindow.close();
    activeWindow = null;
    if (browserDialog.open) browserDialog.close();
    return true;
  }

  async function refreshInfo() {
    if (!activeSession) return;
    try {
      const info = await api(`/api/v1/browser-sessions/${activeSession.id}`);
      activeSession = info;
      showInteractionMode();
      scheduleStatusRefresh();
    } catch (error) {
      if (!handleSessionError(error)) scheduleStatusRefresh(4000);
    }
  }

  async function open(account) {
    const pendingWindow = window.open("about:blank", "_blank");
    browserDialogAccount.textContent = `账户：${account.label}`;
    browserPageTitle.textContent = "正在启动实时浏览器…";
    browserWebHelp.hidden = false;
    browserNativeHelp.hidden = true;
    browserLiveOpen.disabled = true;
    browserDialog.showModal();
    try {
      activeSession = await api(
        `/api/v1/accounts/${account.id}/browser-session`,
        {method: "POST", body: "{}"}
      );
      showInteractionMode();
      browserLiveOpen.disabled = false;
      if (!activeSession.live_url) {
        if (pendingWindow) pendingWindow.close();
        activeWindow = null;
      } else if (pendingWindow) {
        activeWindow = pendingWindow;
        const liveUrl = new URL(activeSession.live_url, window.location.href).href;
        pendingWindow.location.replace(liveUrl);
      } else {
        showNotice("浏览器阻止了新标签，请点击“打开或重新打开实时浏览器”", true);
      }
      scheduleStatusRefresh();
    } catch (error) {
      if (pendingWindow) pendingWindow.close();
      showNotice(error.message, true);
      browserDialog.close();
    }
  }

  async function close(saveState, forceSave = false) {
    if (closing) return;
    closing = true;
    window.clearTimeout(pollTimer);
    try {
      let closeResult = null;
      if (activeSession) {
        closeResult = await api(`/api/v1/browser-sessions/${activeSession.id}/close`, {
          method: "POST",
          body: JSON.stringify({save_state: saveState, force_save: forceSave})
        });
      }
      activeSession = null;
      if (activeWindow && !activeWindow.closed) activeWindow.close();
      activeWindow = null;
      browserDialog.close();
      await loadAccounts();
      if (!saveState) {
        showNotice("临时浏览器会话已丢弃");
      } else if (closeResult?.verified) {
        showNotice("已检测到登录状态，浏览器会话已加密保存");
      } else {
        showNotice("已按你的确认加密保存当前浏览器状态；后续签到会再次验证");
      }
    } catch (error) {
      if (saveState && !forceSave && error.status === 409) {
        forceSaveDialog.showModal();
      } else {
        handleSessionError(error);
      }
    } finally {
      closing = false;
    }
  }

  function closeForceSaveDialog() {
    forceSaveDialog.close();
  }

  browserLiveOpen.addEventListener("click", openLiveWindow);
  browserSave.addEventListener("click", () => close(true));
  browserDiscard.addEventListener("click", () => close(false));
  document.querySelector("#browser-dialog-close").addEventListener("click", () => close(false));
  browserDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    close(false);
  });
  browserDialog.addEventListener("close", () => {
    window.clearTimeout(pollTimer);
    browserLiveOpen.disabled = false;
  });
  document.querySelector("#force-browser-save-close").addEventListener(
    "click", closeForceSaveDialog
  );
  document.querySelector("#force-browser-save-cancel").addEventListener(
    "click", closeForceSaveDialog
  );
  document.querySelector("#force-browser-save-confirm").addEventListener("click", () => {
    closeForceSaveDialog();
    close(true, true);
  });
  forceSaveDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeForceSaveDialog();
  });

  return {open};
};
