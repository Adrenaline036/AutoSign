window.createNotificationsUi = function createNotificationsUi({
  api,
  element,
  icons,
  loadAccounts,
  showNotice,
  state
}) {
  const channelContainer = document.querySelector("#notification-channels");
  const channelDialog = document.querySelector("#channel-dialog");
  const channelForm = document.querySelector("#channel-form");
  const channelType = document.querySelector("#channel-type");
  const channelKumaFields = document.querySelector("#channel-kuma-fields");
  const channelNapcatFields = document.querySelector("#channel-napcat-fields");
  const assignmentDialog = document.querySelector("#channel-assignment-dialog");
  const assignmentForm = document.querySelector("#channel-assignment-form");
  const assignmentList = document.querySelector("#channel-assignment-list");
  const deleteDialog = document.querySelector("#delete-channel-dialog");
  let editingChannelId = null;
  let assignmentAccountId = null;
  let deletingChannelId = null;

  function channelTypeText(type) {
    return type === "uptime_kuma" ? "Uptime Kuma Push" : "NapCat QQ";
  }

  function updateFormFields() {
    const isKuma = channelType.value === "uptime_kuma";
    channelKumaFields.hidden = !isKuma;
    channelNapcatFields.hidden = isKuma;
  }

  function openChannelDialog(channel = null) {
    editingChannelId = channel?.id ?? null;
    channelForm.reset();
    document.querySelector("#channel-dialog-title").textContent =
      channel ? "编辑推送渠道" : "创建推送渠道";
    document.querySelector("#channel-name").value = channel?.name ?? "";
    channelType.value = channel?.channel_type ?? "uptime_kuma";
    channelType.disabled = Boolean(channel);
    const placeholder = channel ? "已加密保存；留空保留原配置" : "";
    document.querySelector("#channel-push-url").placeholder =
      placeholder || "https://你的Kuma地址/api/push/监控Token";
    document.querySelector("#channel-base-url").placeholder =
      placeholder || "例如：http://napcat.example:3000";
    document.querySelector("#channel-token").placeholder =
      placeholder || "NapCat HTTP Server Token";
    document.querySelector("#channel-target-id").placeholder =
      placeholder || "只输入数字";
    updateFormFields();
    channelDialog.showModal();
    window.setTimeout(() => document.querySelector("#channel-name").focus(), 0);
  }

  function openAssignment(account) {
    assignmentAccountId = account.id;
    document.querySelector("#channel-assignment-account").textContent =
      `账户：${account.label}`;
    function assignmentChoice(channel) {
      const choice = element("label", "channel-choice");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = channel.id;
      checkbox.checked = account.notification_channel_ids.includes(channel.id);
      const copy = element("span");
      copy.append(
        element("strong", "", channel.name),
        element("div", "meta", channelTypeText(channel.channel_type))
      );
      choice.append(checkbox, copy);
      return choice;
    }
    const groups = [
      {type: "uptime_kuma", title: "Uptime Kuma"},
      {type: "napcat", title: "NapCat QQ"}
    ].map(({type, title}) => {
      const column = element("section", "channel-assignment-column");
      const list = element("div", "channel-choice-list");
      const channels = state.notificationChannels.filter(
        (channel) => channel.channel_type === type
      );
      list.replaceChildren(...(
        channels.length
          ? channels.map(assignmentChoice)
          : [element("p", "muted", `还没有 ${title} 渠道。`)]
      ));
      column.append(element("h3", "", title), list);
      return column;
    });
    assignmentList.replaceChildren(...groups);
    assignmentDialog.showModal();
  }

  function render() {
    function channelCard(channel) {
      const card = element("article", "card channel-card");
      const assignedNames = channel.assigned_account_ids
        .map((id) => state.accounts.find((account) => account.id === id)?.label)
        .filter(Boolean);
      const assigned = element(
        "div",
        "channel-accounts",
        assignedNames.length
          ? `已分配：${assignedNames.join("、")}`
          : "尚未分配给账户"
      );
      const actions = element("div", "actions channel-actions");
      const test = element("button", "success-action", "测试");
      test.addEventListener("click", async () => {
        test.disabled = true;
        try {
          const result = await api(
            `/api/v1/notification-channels/${channel.id}/test`,
            {method: "POST", body: "{}"}
          );
          showNotice(`${channel.name}：${result.message}`);
        } catch (error) {
          showNotice(error.message, true);
        } finally {
          test.disabled = false;
        }
      });
      const edit = element("button", "secondary", "编辑");
      edit.addEventListener("click", () => openChannelDialog(channel));
      const remove = element("button", "inline-delete");
      remove.type = "button";
      remove.innerHTML = icons.trash;
      remove.title = "删除推送渠道";
      remove.setAttribute("aria-label", remove.title);
      remove.addEventListener("click", () => {
        deletingChannelId = channel.id;
        document.querySelector("#delete-channel-name").textContent = channel.name;
        deleteDialog.showModal();
      });
      actions.append(test, edit, remove);
      card.append(element("h3", "", channel.name), assigned, actions);
      return card;
    }

    const groups = [
      {type: "uptime_kuma", title: "Uptime Kuma"},
      {type: "napcat", title: "NapCat QQ"}
    ].map(({type, title}) => {
      const column = element("section", "channel-column");
      const list = element("div", "channel-list");
      const channels = state.notificationChannels.filter(
        (channel) => channel.channel_type === type
      );
      list.replaceChildren(...(
        channels.length
          ? channels.map(channelCard)
          : [element("p", "muted", `还没有 ${title} 渠道。`)]
      ));
      column.append(element("h3", "", title), list);
      return column;
    });
    channelContainer.replaceChildren(...groups);
  }

  document.querySelector("#channel-create").addEventListener(
    "click", () => openChannelDialog()
  );
  document.querySelector("#channel-dialog-close").addEventListener(
    "click", () => channelDialog.close()
  );
  document.querySelector("#channel-cancel").addEventListener(
    "click", () => channelDialog.close()
  );
  channelType.addEventListener("change", updateFormFields);
  channelForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const saveButton = document.querySelector("#channel-save");
    const isKuma = channelType.value === "uptime_kuma";
    const payload = {
      name: document.querySelector("#channel-name").value,
      channel_type: channelType.value,
      push_url: isKuma
        ? document.querySelector("#channel-push-url").value || null
        : null,
      base_url: isKuma
        ? null
        : document.querySelector("#channel-base-url").value || null,
      access_token: isKuma
        ? null
        : document.querySelector("#channel-token").value || null,
      target_type: isKuma
        ? null
        : document.querySelector("#channel-target-type").value,
      target_id: isKuma
        ? null
        : document.querySelector("#channel-target-id").value || null
    };
    if (
      !editingChannelId
      && (
        (isKuma && !payload.push_url)
        || (!isKuma && (!payload.base_url || !payload.access_token || !payload.target_id))
      )
    ) {
      showNotice("请完整填写推送渠道配置", true);
      return;
    }
    saveButton.disabled = true;
    const wasEditing = Boolean(editingChannelId);
    try {
      await api(
        editingChannelId
          ? `/api/v1/notification-channels/${editingChannelId}`
          : "/api/v1/notification-channels",
        {
          method: editingChannelId ? "PUT" : "POST",
          body: JSON.stringify(payload)
        }
      );
      channelDialog.close();
      await loadAccounts();
      showNotice(wasEditing ? "推送渠道已更新" : "推送渠道已加密保存");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      saveButton.disabled = false;
    }
  });
  channelDialog.addEventListener("close", () => {
    editingChannelId = null;
    channelType.disabled = false;
    channelForm.reset();
    updateFormFields();
  });

  document.querySelector("#channel-assignment-close").addEventListener(
    "click", () => assignmentDialog.close()
  );
  document.querySelector("#channel-assignment-cancel").addEventListener(
    "click", () => assignmentDialog.close()
  );
  assignmentForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!assignmentAccountId) return;
    const channelIds = [
      ...assignmentList.querySelectorAll('input[type="checkbox"]:checked')
    ].map((input) => input.value);
    try {
      await api(
        `/api/v1/accounts/${assignmentAccountId}/notification-channels`,
        {
          method: "PUT",
          body: JSON.stringify({channel_ids: channelIds})
        }
      );
      assignmentDialog.close();
      await loadAccounts();
      showNotice("账户的消息推送渠道已更新");
    } catch (error) {
      showNotice(error.message, true);
    }
  });
  assignmentDialog.addEventListener("close", () => {
    assignmentAccountId = null;
    assignmentList.replaceChildren();
  });

  function closeDeleteDialog() {
    deleteDialog.close();
  }
  document.querySelector("#delete-channel-close").addEventListener(
    "click", closeDeleteDialog
  );
  document.querySelector("#delete-channel-cancel").addEventListener(
    "click", closeDeleteDialog
  );
  document.querySelector("#delete-channel-confirm").addEventListener("click", async () => {
    if (!deletingChannelId) return;
    const button = document.querySelector("#delete-channel-confirm");
    button.disabled = true;
    try {
      await api(`/api/v1/notification-channels/${deletingChannelId}`, {
        method: "DELETE"
      });
      deleteDialog.close();
      await loadAccounts();
      showNotice("推送渠道已删除并解除账户分配");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  deleteDialog.addEventListener("close", () => {
    deletingChannelId = null;
    document.querySelector("#delete-channel-name").textContent = "";
  });

  return {openAssignment, render};
};
