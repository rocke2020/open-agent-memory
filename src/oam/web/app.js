(() => {
  "use strict";

  const LANGUAGE_STORAGE_KEY = "oam-inspector-language";
  const translations = {
    "Open Agent Memory Inspector": "Open Agent Memory 记忆查看器",
    "Memory Inspector": "记忆查看器",
    "Explore memories, insights, and the evidence connecting them.": "探索记忆、洞察及连接它们的证据。",
    "Language": "语言",
    "Connect to a personal memory": "连接个人记忆",
    "Use a user credential and the user ID it belongs to. Trusted and local development hosts may provide identity without a credential.": "使用用户凭证及其所属的用户 ID。受信任的主机和本地开发主机可无需凭证提供身份。",
    "Tenant": "租户",
    "User ID": "用户 ID",
    "API credential": "API 凭证",
    "The credential stays in page memory and clears on sign-out or reload.": "凭证仅保存在当前页面内存中，退出或重新加载时清除。",
    "Connect": "连接",
    "Memory workspace": "记忆工作区",
    "Current user": "当前用户",
    "Sign out": "退出",
    "Inspector views": "查看器视图",
    "Memories": "记忆",
    "Insights": "洞察",
    "Retrieve": "检索",
    "Resources": "资料",
    "User Profile": "用户画像",
    "Refresh": "刷新",
    "Search current memory": "搜索当前记忆",
    "Ask for supporting evidence": "输入要查找的支持证据",
    "Search": "搜索",
    "Retrieval mode": "检索模式",
    "Fast": "快速",
    "Mid": "标准",
    "Deep": "深入",
    "Add Resource": "添加资料",
    "Current items": "当前条目",
    "Load more": "加载更多",
    "Details": "详情",
    "Refresh profile": "刷新画像",
    "Select an item to inspect its source and readiness.": "选择条目以查看其来源和就绪状态。",
    "Customize profile": "自定义画像",
    "Saved profile instruction": "已保存的画像指令",
    "Custom instructions": "自定义指令",
    "Describe the content or format you want": "描述所需的内容或格式",
    "Apply and refresh": "应用并刷新",
    "Use default instructions": "使用默认指令",
    "Default profile prompt": "默认画像提示词",
    "Source messages": "来源消息",
    "Original messages supporting the selected memory.": "支持所选记忆的原始消息。",
    "Checking access…": "正在检查访问权限…",
    "Connected. Current source policy is applied to every view.": "已连接。所有视图均应用当前来源策略。",
    "Signed out. Page-held content and credential were cleared.": "已退出。页面中的内容和凭证已清除。",
    "Retrieved evidence": "检索到的证据",
    "What would you like to retrieve?": "您想检索什么？",
    "Retrieve evidence": "检索证据",
    "Enter a query to retrieve cited evidence. Open a result to inspect it.": "输入查询以检索带引用的证据，打开结果可查看详情。",
    "Loading User Profile…": "正在加载用户画像…",
    "User Profile loaded.": "用户画像已加载。",
    "Up to date": "最新",
    "Out of date": "待更新",
    "Some inputs unavailable": "部分输入不可用",
    "Not created": "尚未创建",
    "Needs rebuild": "需要重建",
    "User Profile status": "用户画像状态",
    "Status unknown": "状态未知",
    "Updated {relative}": "更新于{relative}",
    "Updated time not recorded": "未记录更新时间",
    "Saved custom instruction active; it supplements the default prompt.": "已启用保存的自定义指令，作为默认提示词的补充。",
    "Default prompt active; no saved custom instruction.": "正在使用默认提示词，未保存自定义指令。",
    "Create profile": "创建画像",
    "Failed Insight inputs": "失败的洞察输入",
    "Failed initial processing": "初始处理失败",
    "This User Profile exceeds the server's display limit.": "此用户画像超出服务器的显示限制。",
    "No generated User Profile yet. Use Create profile to create one from eligible inputs.": "尚未生成用户画像。点击“创建画像”，使用符合条件的输入创建画像。",
    "This stored revision has no User Profile document. Use Refresh profile to build one from current sources.": "此保存版本没有用户画像文档。点击“刷新画像”，根据当前来源构建画像。",
    "The stored User Profile is unavailable for these inputs. Refresh it to rebuild from current sources.": "已保存的用户画像对这些输入不可用。请刷新以根据当前来源重建。",
    "{count} Insight input{plural} failed. {lead} only ready information.": "{count} 个洞察输入失败。{lead}已就绪的信息。",
    "This User Profile uses": "此用户画像仅使用",
    "A refreshed User Profile can use": "刷新后的用户画像可仅使用",
    "{count} memory input{plural} could not be prepared. Check the input operation before refreshing the User Profile.": "{count} 个记忆输入无法准备就绪。刷新用户画像前，请检查输入操作。",
    "Generation details": "生成详情",
    "Insights outside this summary": "未纳入此摘要的洞察",
    "Reference details ({count})": "引用详情（{count}）",
    "[{index}] Provided profile details": "[{index}] 提供的画像详情",
    "Source": "来源",
    "[{index}] Conversation on {dates}": "[{index}] 对话日期：{dates}",
    "[{index}] Conversation": "[{index}] 对话",
    "Conversation": "对话",
    "Passage": "段落",
    "Checking User Profile inputs…": "正在检查用户画像输入…",
    "Nothing new to create or refresh. Stored User Profile reused with zero generation calls.": "没有需要创建或刷新的内容。已复用保存的用户画像，未调用生成模型。",
    "View User Profile": "查看用户画像",
    "User Profile creation/refresh pending; operation {id}.": "用户画像正在等待创建或刷新；操作 {id}。",
    "User Profile inputs are not ready. Check the input operation, then try again.": "用户画像输入尚未就绪。请检查输入操作后重试。",
    "Loading {view}…": "正在加载{view}…",
    "{count} current {view} shown.": "已显示 {count} 条当前{view}。",
    "Resource": "资料",
    "{kind} · {media} · version {version}": "{kind} · {media} · 版本 {version}",
    "{count} shown{support}": "已显示 {count} 条{support}",
    "{count} of {total} shown{support}": "已显示 {count} / {total} 条{support}",
    " · supported by memories": " · 由记忆支持",
    "No current {view} are available.": "当前没有可用的{view}。",
    "Loading details…": "正在加载详情…",
    "Retrieved passage": "检索到的段落",
    "This item is no longer available under current policy.": "根据当前策略，此条目已不可用。",
    "Led to": "导致",
    "Caused by": "起因",
    "Updated by": "后续更新",
    "Updates": "更新",
    "Shared entity · {name}": "共同实体 · {name}",
    "Event time": "事件时间",
    "Referenced time": "引用时间",
    "Mentioned time": "提及时间",
    "Source created": "来源创建时间",
    "Source updated": "来源更新时间",
    "Time-related": "时间相关",
    "{basis} · overlapping or same time": "{basis} · 时间重叠或相同",
    "{basis} · {amount} {unit}{plural} apart": "{basis} · 相隔 {amount} {unit}",
    "day": "天",
    "hour": "小时",
    "minute": "分钟",
    "Similar meaning": "含义相似",
    "Cause & effect": "因果",
    "Time": "时间",
    "Shared entity": "共同实体",
    "Connection map": "关系图",
    "This memory": "此记忆",
    "Open connected memory {id}": "打开关联记忆 {id}",
    "{start}–{end} of {total} connected memories": "关联记忆 {start}–{end} / {total}",
    "Previous": "上一页",
    "Next": "下一页",
    "Entities in supporting memories": "支持记忆中的实体",
    "Entities in this memory": "此记忆中的实体",
    "Memory connections": "记忆关系",
    "Filter connections": "筛选关系",
    "Not recorded": "未记录",
    "This memory: {time}": "此记忆：{time}",
    "Related memory: {time}": "关联记忆：{time}",
    "Open memory →": "打开记忆 →",
    "Open memory {id}": "打开记忆 {id}",
    "No connections match this filter.": "没有符合此筛选条件的关系。",
    "No stored connections.": "没有已存储的关系。",
    "Mentioned as {mention}. Show connections through this entity.": "提及名称为 {mention}。显示通过此实体建立的关系。",
    "No supported entities recorded.": "未记录有证据支持的实体。",
    "Mentioned together: {names}": "共同提及：{names}",
    "The memory above describes their relationship; co-mentions alone do not establish a typed relationship.": "上方记忆描述了它们的关系；仅共同提及并不代表存在特定类型的关系。",
    "Map": "关系图",
    "List": "列表",
    "All": "全部",
    "Causal": "因果",
    "Shared entities": "共同实体",
    "Updates & similar": "更新与相似",
    "Showing the first 50 connections. Causal and update links appear first.": "显示前 50 条关系，因果和更新关系优先。",
    "Loading source messages…": "正在加载来源消息…",
    "No source messages available.": "没有可用的来源消息。",
    "Insight": "洞察",
    "Memory": "记忆",
    "Extracted fact": "提取的事实",
    "Origin": "来源类型",
    "Canonical ID": "规范 ID",
    "Revision": "修订版本",
    "Subtype": "子类型",
    "Supporting memories": "支持记忆",
    "Open supporting memory →": "打开支持记忆 →",
    "Open supporting memory {id}": "打开支持记忆 {id}",
    "Support revision": "支持版本",
    "No supporting memories recorded.": "未记录支持记忆。",
    "Affected dependent Insights": "受影响的依赖洞察",
    "No dependent Insights are affected.": "没有依赖洞察受到影响。",
    "Remove Insight": "移除洞察",
    "Remove Memory Unit": "移除记忆单元",
    "Source provenance": "来源溯源",
    "Unknown": "未知",
    "Version": "版本",
    "Inspect source": "查看来源",
    "Delete this source": "删除此来源",
    "Source messages unavailable.": "来源消息不可用。",
    "Role": "角色",
    "Memory Unit": "记忆单元",
    "This item has no canonical removal ID.": "此条目没有可用于移除的规范 ID。",
    "Remove {target}? The selected {type} will be excluded without deleting its source.": "移除 {target}？所选{type}将被排除，其来源不会被删除。",
    "{target} excluded; list refresh failed: {error}": "已排除 {target}；列表刷新失败：{error}",
    "{target} excluded. Cleanup is {status}.": "已排除 {target}。清理状态：{status}。",
    "Source Evidence": "来源证据",
    "Active version": "当前版本",
    "Retained passages": "保留的段落",
    "Locator": "定位信息",
    "Current source {id} opened.": "已打开当前来源 {id}。",
    "Media type": "媒体类型",
    "Original bytes": "原始字节数",
    "Index operation": "索引操作",
    "Pending": "等待中",
    "Abstract": "摘要",
    "Inspect original": "查看原文",
    "Download original": "下载原文",
    "Revise": "修订",
    "Delete Resource": "删除资料",
    "Operation status": "操作状态",
    "Extracted passages": "提取的段落",
    "More passages exist than this detail view displays.": "还有更多段落未在此详情视图中显示。",
    "This original is binary. Use Download original to inspect it in a local viewer.": "此原始文件是二进制格式。请点击“下载原文”，使用本地查看器打开。",
    "Original source text": "原始来源文本",
    "Original rendered as text. Source markup was not executed.": "原文已显示为纯文本，未执行其中的标记代码。",
    "Original download started.": "已开始下载原文。",
    "HTTPS URL": "HTTPS 地址",
    "Upload file": "上传文件",
    "Text": "文本",
    "Original text": "原始文本",
    "Declared media type": "声明的媒体类型",
    "Original file": "原始文件",
    "Input": "输入方式",
    "Revise {id} (expected version {version})": "修订 {id}（预期版本 {version}）",
    "Resource ID": "资料 ID",
    "Create immutable revision": "创建不可变修订版本",
    "Create Resource": "创建资料",
    "Select an original file.": "请选择原始文件。",
    "Accepted version {version}; operation {id}.": "已接受版本 {version}；操作 {id}。",
    "Durable operation": "持久化操作",
    "Operation ID": "操作 ID",
    "Refresh operation status": "刷新操作状态",
    "Operation {id}": "操作 {id}",
    "Status": "状态",
    "Refresh status": "刷新状态",
    "Operation {status}.": "操作状态：{status}。",
    "chat source": "对话来源",
    "Delete {target}? All versions and dependent passages, memories, Insights, and User Profile fields become unavailable immediately. Cleanup is tracked separately.": "删除 {target}？所有版本及依赖的段落、记忆、洞察和用户画像字段将立即不可用。清理进度单独跟踪。",
    "{target} excluded. Cleanup operation {id} is {status}.": "已排除 {target}。清理操作 {id} 的状态为{status}。",
    "Retrieving evidence…": "正在检索证据…",
    "Searching current evidence…": "正在搜索当前证据…",
    "{count} cited items returned.": "已返回 {count} 条带引用的结果。",
    "Retrieval failed": "检索失败",
    "Try retrieving again after the service is available.": "请在服务恢复可用后重新检索。",
    "Current search context": "当前搜索上下文",
    "Mode": "模式",
    "Elapsed": "耗时",
    "{seconds}s": "{seconds} 秒",
    "Estimated tokens": "估算词元数",
    "Coverage": "覆盖情况",
    "Partial": "部分可用",
    "Current": "当前",
    "No matching current evidence.": "没有匹配的当前证据。",
    "Some indexing or retrieval stages are incomplete.": "部分索引或检索阶段尚未完成。",
    "Explain this search": "解释此搜索",
    "Trace {id}": "跟踪记录 {id}",
    "Recorded status": "记录的状态",
    "Trace {status}.": "跟踪状态：{status}。",
    "Enter custom instructions before applying them.": "应用前请输入自定义指令。",
    "Unavailable": "不可用",
    "Request failed ({code}).": "请求失败（{code}）。",
    "Retrieval is currently unavailable ({code}).": "检索服务当前不可用（{code}）。",
    "Unable to reach the service. Check the connection and try again.": "无法连接服务。请检查连接后重试。",
    "memories": "记忆", "insights": "洞察", "resources": "资料",
    "fact": "事实", "insight": "洞察", "memory": "记忆", "basic": "基础记忆",
    "preferences": "偏好", "patterns": "行为模式", "projects": "项目", "relationships": "人际关系", "experiences": "经验", "general_insights": "综合洞察",
    "chat": "对话", "interaction": "交互", "evidence": "证据", "resource": "资料",
    "pending": "等待中", "processing": "处理中", "succeeded": "已成功", "partial": "部分完成", "failed": "已失败", "cancelled": "已取消",
    "ready": "已就绪", "missing": "缺失", "blocked": "已阻塞", "unavailable": "不可用", "complete": "已完成", "completed": "已完成", "not_required": "无需处理",
    "fast": "快速", "mid": "标准", "deep": "深入", "normal": "标准",
    "user": "用户", "assistant": "助手", "system": "系统", "developer": "开发者", "tool": "工具",
  };
  const bindings = new WeakMap();
  const msg = (key, params = {}) => ({ uiMessage: true, key, params });
  let language = "en";

  function translate(value) {
    if (typeof value === "function") return translate(value());
    if (!value?.uiMessage) return String(value ?? "");
    const template = language === "zh-CN" ? translations[value.key] ?? value.key : value.key;
    return template.replace(/\{(\w+)\}/g, (_, key) => translate(value.params[key]));
  }

  function bind(node, attribute, value) {
    const marker = attribute === "textContent" ? "data-i18n" : `data-i18n-${attribute}`;
    if (value?.uiMessage || typeof value === "function") {
      if (!bindings.has(node)) bindings.set(node, new Map());
      bindings.get(node).set(attribute, value);
      node.setAttribute(marker, value.key || "");
    } else {
      bindings.get(node)?.delete(attribute);
      node.removeAttribute(marker);
    }
    if (attribute === "textContent") node.textContent = translate(value);
    else node.setAttribute(attribute, translate(value));
  }

  const setText = (node, value) => bind(node, "textContent", value);
  const setAttribute = (node, attribute, value) => bind(node, attribute, value);
  const viewLabel = (view, title = false) => msg(title ? { resources: "Resources", profiles: "User Profile", memories: "Memories", insights: "Insights", retrieve: "Retrieved evidence" }[view] : view);
  const enumValues = new Set(["fact", "insight", "memory", "basic", "preferences", "patterns", "projects", "relationships", "experiences", "general_insights", "chat", "interaction", "evidence", "resource", "pending", "processing", "succeeded", "partial", "failed", "cancelled", "ready", "missing", "blocked", "unavailable", "complete", "completed", "not_required", "fast", "mid", "deep", "normal", "user", "assistant", "system", "developer", "tool"]);
  const enumLabel = (value) => enumValues.has(value) ? msg(value) : value;

  function errorMessage(error) {
    const code = error.message || "Unknown";
    if (error instanceof TypeError) return msg("Unable to reach the service. Check the connection and try again.");
    if (code === "RecallUnavailable") return msg("Retrieval is currently unavailable ({code}).", { code });
    return msg("Request failed ({code}).", { code });
  }

  function setLanguage(value, persist = false) {
    language = value === "zh-CN" ? "zh-CN" : "en";
    document.documentElement.lang = language;
    document.getElementById("language").value = language;
    for (const node of document.querySelectorAll("[data-i18n], [data-i18n-aria-label], [data-i18n-placeholder], [data-i18n-title]")) {
      for (const [attribute, message] of bindings.get(node) || []) bind(node, attribute, message);
    }
    if (persist) {
      try { localStorage.setItem(LANGUAGE_STORAGE_KEY, language); } catch (_) { /* Page-only preference when storage is disabled. */ }
    }
  }

  function initializeLanguage() {
    for (const node of document.querySelectorAll("[data-i18n]")) setText(node, msg(node.dataset.i18n));
    for (const attribute of ["aria-label", "placeholder"]) {
      for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) setAttribute(node, attribute, msg(node.getAttribute(`data-i18n-${attribute}`)));
    }
    let saved;
    try { saved = localStorage.getItem(LANGUAGE_STORAGE_KEY); } catch (_) { /* Browser language remains available without storage. */ }
    const browserLanguage = (navigator.languages?.length ? navigator.languages : [navigator.language]).find(value => /^(en|zh)(-|$)/i.test(value)) || "en";
    setLanguage(saved === "en" || saved === "zh-CN" ? saved : /^zh(-|$)/i.test(browserLanguage) ? "zh-CN" : "en");
    document.getElementById("language").addEventListener("change", event => setLanguage(event.target.value, true));
  }

  const byId = (id) => document.getElementById(id);
  const current = (generation) => generation === state.generation;
  const currentDetail = (generation, view, requestId) => current(generation) && view === state.view && requestId === state.detailRequest;
  const state = {
    credential: "",
    tenant: "",
    user: "",
    view: "memories",
    items: [],
    nextAfter: null,
    generation: 0,
    draftKey: null,
    profileRequest: 0,
    detailRequest: 0,
    listRequest: 0,
    savedProfileInstruction: "",
  };
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) setText(node, text);
    if (className) node.className = className;
    return node;
  }

  function status(message, isError = false) {
    const node = byId("status");
    setText(node, message);
    node.dataset.state = isError ? "error" : "info";
  }

  function resetContent() {
    clearSourceMessages();
    state.generation += 1;
    state.items = [];
    state.nextAfter = null;
    state.draftKey = null;
    state.profileRequest += 1;
    state.detailRequest += 1;
    state.listRequest += 1;
    byId("recall-level").value = "fast";
    byId("query").value = "";
    state.savedProfileInstruction = "";
    byId("profile-instruction").value = "";
    setText(byId("profile-instruction-status"), "");
    setText(byId("profile-default-prompt"), "");
    byId("profile-prompt-details").open = false;
    byId("profile-customization").open = false;
    byId("profile-use-default").hidden = true;
    byId("item-list").replaceChildren();
    setText(byId("list-summary"), "");
    byId("detail").replaceChildren(element("p", msg("Select an item to inspect its source and readiness.")));
    byId("load-more").hidden = true;
    byId("workspace").hidden = true;
    byId("account").hidden = true;
    setText(byId("scope-label"), "");
    byId("scope-label").removeAttribute("title");
  }

  function clearSourceMessages() {
    byId("source-panel").hidden = true;
    byId("source-messages").replaceChildren();
    byId("workspace-grid").classList.remove("with-memory");
  }

  function route(suffix) {
    return `/v1/users/${encodeURIComponent(state.user)}/${suffix}`;
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.credential) headers.set("Authorization", `Bearer ${state.credential}`);
    headers.set("X-OAM-Tenant", state.tenant);
    headers.set("X-OAM-User", state.user);
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    if (!response.ok) {
      let code = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        code = payload.detail?.code || code;
      } catch (_) {
        // The status code remains the safe error when a proxy returns non-JSON.
      }
      throw new Error(code);
    }
    return response;
  }

  async function json(path, options) {
    return (await request(path, options)).json();
  }

  function button(text, action, className = "secondary") {
    const node = element("button", text, className);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }

  function labelValue(label, value) {
    const row = element("div", undefined, "detail-row");
    row.append(element("strong", label), element("span", value ?? msg("Unavailable")));
    return row;
  }

  function showError(error) {
    status(errorMessage(error), true);
  }

  async function connect(event) {
    event.preventDefault();
    resetContent();
    state.tenant = byId("tenant").value.trim();
    state.user = byId("user").value.trim();
    state.credential = byId("credential").value;
    byId("credential").value = "";
    const generation = state.generation;
    status(msg("Checking access…"));
    try {
      const [memories] = await Promise.all([
        json(route("memories")),
        json(route("resources")),
      ]);
      if (generation !== state.generation) return;
      setText(byId("scope-label"), `${state.tenant} / ${state.user}`);
      setAttribute(byId("scope-label"), "title", `${state.tenant} / ${state.user}`);
      byId("connection").hidden = true;
      byId("workspace").hidden = false;
      byId("account").hidden = false;
      state.view = "memories";
      setView("memories", memories);
      status(msg("Connected. Current source policy is applied to every view."));
    } catch (error) {
      if (generation !== state.generation) return;
      state.credential = "";
      resetContent();
      showError(error);
    }
  }

  function signOut() {
    resetContent();
    state.credential = "";
    state.tenant = "";
    state.user = "";
    byId("connect-form").reset();
    byId("credential").value = "";
    byId("connection").hidden = false;
    status(msg("Signed out. Page-held content and credential were cleared."));
    byId("user").focus();
  }

  function setView(view, initial) {
    clearSourceMessages();
    state.view = view;
    state.detailRequest += 1;
    state.listRequest += 1;
    state.items = [];
    state.nextAfter = null;
    byId("item-list").replaceChildren();
    byId("detail").replaceChildren();
    setText(byId("list-summary"), "");
    byId("load-more").hidden = true;
    setText(byId("list-title"), viewLabel(view, true));
    byId("new-resource").hidden = view !== "resources";
    byId("search-form").hidden = !["memories", "retrieve"].includes(view);
    setText(byId("query-label"), view === "retrieve" ? msg("What would you like to retrieve?") : msg("Search current memory"));
    byId("retrieval-mode-field").hidden = view !== "retrieve";
    setText(byId("search-form").querySelector("button"), view === "retrieve" ? msg("Retrieve evidence") : msg("Search"));
    byId("profile-form").hidden = view !== "profiles";
    byId("refresh-profile").hidden = view !== "profiles";
    setText(byId("detail-title"), view === "profiles" ? msg("User Profile") : msg("Details"));
    document.querySelector(".list-panel").hidden = view === "profiles";
    byId("workspace-grid").classList.toggle("profile-view", view === "profiles");
    byId("item-list").hidden = view === "profiles";
    byId("refresh-list").hidden = ["profiles", "retrieve"].includes(view);
    for (const tab of document.querySelectorAll("[data-view]")) {
      tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
    }
    if (view === "retrieve") {
      byId("detail").append(element("p", msg("Enter a query to retrieve cited evidence. Open a result to inspect it.")));
      return;
    }
    if (view === "profiles") {
      byId("load-more").hidden = true;
      loadProfile();
      return;
    }
    if (initial) {
      applyList(initial, false);
    } else {
      loadList(false).catch(showError);
    }
  }

  async function loadProfile() {
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      status(msg("Loading User Profile…"));
      const profile = await json(route("profile"));
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return false;
      const message = showProfile(profile);
      status(message || msg("User Profile loaded."));
      return true;
    } catch (error) {
      if (current(generation) && state.view === "profiles" && requestId === state.profileRequest) showError(error);
      return false;
    }
  }

  function profileParagraph(text) {
    const paragraph = element("p");
    let start = 0;
    for (const match of text.matchAll(/\*\*([^*\n]+)\*\*/g)) {
      paragraph.append(document.createTextNode(text.slice(start, match.index)), element("strong", match[1]));
      start = match.index + match[0].length;
    }
    paragraph.append(document.createTextNode(text.slice(start)));
    return paragraph;
  }

  function renderProfileContent(content) {
    try {
      const parsed = JSON.parse(content);
      if (parsed !== null && typeof parsed === "object") {
        return element("pre", JSON.stringify(parsed, null, 2), "source-text");
      }
    } catch (_) {
      // The content is Markdown or another text format.
    }
    if (!content.startsWith("## Main content\n")) return element("pre", content, "source-text");
    const rendered = element("div", undefined, "profile-content");
    const mainContent = content.split("\n\n## References\n", 1)[0];
    for (const line of mainContent.split("\n")) {
      if (!line.trim()) continue;
      if (line.startsWith("## ")) rendered.append(element("h4", line.slice(3)));
      else rendered.append(profileParagraph(line.startsWith("- ") ? line.slice(2) : line));
    }
    return rendered;
  }

  function profileStatusLine(profile) {
    const labels = {
      current: msg("Up to date"),
      dirty: msg("Out of date"),
      stale: msg("Out of date"),
      partial: msg("Some inputs unavailable"),
      uncovered: msg("Up to date"),
      missing: profile.profile_revision == null ? msg("Not created") : msg("Needs rebuild"),
    };
    const line = element("div", undefined, "profile-freshness");
    setAttribute(line, "aria-label", msg("User Profile status"));
    const badge = element("span", labels[profile.freshness] || msg("Status unknown"), "profile-state");
    badge.dataset.freshness = profile.freshness || "unknown";
    line.append(badge);
    const updated = profile.updated_at ? new Date(profile.updated_at) : null;
    if (updated && !Number.isNaN(updated.getTime())) {
      const elapsed = (Date.now() - updated.getTime()) / 1000;
      const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
      const [unit, seconds] = units.find(([, seconds]) => Math.abs(elapsed) >= seconds) || units.at(-1);
      const relative = () => new Intl.RelativeTimeFormat(language, { numeric: "auto" }).format(-Math.round(elapsed / seconds), unit);
      const time = element("time", msg("Updated {relative}", { relative }));
      time.dateTime = profile.updated_at;
      setAttribute(time, "title", () => updated.toLocaleString(language));
      line.append(element("span", "·"), time);
    } else if (profile.profile_revision != null) {
      line.append(element("span", "·"), element("span", msg("Updated time not recorded")));
    }
    return line;
  }

  function showProfile(profile) {
    const instruction = profile.instruction || "";
    const editor = byId("profile-instruction");
    if (editor.value === state.savedProfileInstruction) editor.value = instruction;
    state.savedProfileInstruction = instruction;
    setText(byId("profile-default-prompt"), profile.default_prompt);
    setText(byId("profile-instruction-status"), profile.instruction
      ? msg("Saved custom instruction active; it supplements the default prompt.")
      : msg("Default prompt active; no saved custom instruction."));
    byId("profile-use-default").hidden = !instruction;
    setText(byId("refresh-profile"), profile.profile_revision == null ? msg("Create profile") : msg("Refresh profile"));
    const detail = byId("detail");
    const failedInputs = profile.failed_inputs || 0;
    const blockedInputs = profile.blocked_inputs || 0;
    const uncoveredInputs = profile.uncovered_inputs || 0;
    detail.replaceChildren();
    detail.append(profileStatusLine(profile));
    if (profile.content) detail.append(renderProfileContent(profile.content));
    if (failedInputs > 0) detail.append(labelValue(msg("Failed Insight inputs"), failedInputs));
    if (blockedInputs > 0) detail.append(labelValue(msg("Failed initial processing"), blockedInputs));
    let message = "";
    if (!profile.content) {
      if ((profile.omitted || []).some(item => item.reason === "budget")) {
        message = msg("This User Profile exceeds the server's display limit.");
      } else if (profile.profile_revision == null) {
        message = msg("No generated User Profile yet. Use Create profile to create one from eligible inputs.");
      } else if (profile.freshness === "missing") {
        message = msg("This stored revision has no User Profile document. Use Refresh profile to build one from current sources.");
      } else {
        message = msg("The stored User Profile is unavailable for these inputs. Refresh it to rebuild from current sources.");
      }
      detail.append(element("p", message));
    }
    if (failedInputs > 0) detail.append(element("p", msg("{count} Insight input{plural} failed. {lead} only ready information.", { count: failedInputs, plural: failedInputs === 1 ? "" : "s", lead: profile.content ? msg("This User Profile uses") : msg("A refreshed User Profile can use") })));
    if (blockedInputs > 0) detail.append(element("p", msg("{count} memory input{plural} could not be prepared. Check the input operation before refreshing the User Profile.", { count: blockedInputs, plural: blockedInputs === 1 ? "" : "s" })));
    if (uncoveredInputs > 0) {
      const generationDetails = element("details");
      generationDetails.append(element("summary", msg("Generation details")));
      generationDetails.append(labelValue(msg("Insights outside this summary"), uncoveredInputs));
      detail.append(generationDetails);
    }
    const references = profile.references || [];
    if (references.length) {
      const referenceDetails = element("details", undefined, "profile-references");
      referenceDetails.append(element("summary", msg("Reference details ({count})", { count: references.length })));
      for (const [index, reference] of references.entries()) {
        const row = element("div", undefined, "citation");
        if (reference.kind === "profile_data") {
          row.append(element("p", msg("[{index}] Provided profile details", { index: index + 1 })));
          row.append(labelValue(msg("Source"), reference.profile_data_id));
        } else {
          const passages = reference.passages || [reference];
          const dates = [...new Set(passages.filter(passage => passage.event_time).map(passage => passage.event_time.slice(0, 10)))];
          row.append(element("p", dates.length ? msg("[{index}] Conversation on {dates}", { index: index + 1, dates: dates.join("; ") }) : msg("[{index}] Conversation", { index: index + 1 })));
          for (const passage of passages) {
            row.append(labelValue(msg("Conversation"), passage.source_id));
            row.append(labelValue(msg("Passage"), passage.passage_id));
          }
        }
        referenceDetails.append(row);
      }
      detail.append(referenceDetails);
    }
    return message;
  }

  async function refreshProfile(body = {}) {
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      status(msg("Checking User Profile inputs…"));
      const outcome = await json(route("profile/refresh"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return;
      if (body.reset_instruction) byId("profile-instruction").value = "";
      if (outcome.status === "unchanged") {
        if (await loadProfile()) status(msg("Nothing new to create or refresh. Stored User Profile reused with zero generation calls."));
      } else {
        showReceipt(outcome.operation_id, outcome);
        byId("detail").append(button(msg("View User Profile"), loadProfile));
        status(msg("User Profile creation/refresh pending; operation {id}.", { id: outcome.operation_id }));
      }
    } catch (error) {
      if (current(generation) && state.view === "profiles" && requestId === state.profileRequest) {
        if (error.message === "ProfileInputsNotReady") {
          status(msg("User Profile inputs are not ready. Check the input operation, then try again."), true);
        } else {
          showError(error);
        }
      }
    }
  }

  async function loadList(append) {
    const view = state.view;
    if (!["memories", "insights", "resources"].includes(view)) return;
    const generation = state.generation;
    const requestId = ++state.listRequest;
    const detailRequest = state.detailRequest;
    const params = new URLSearchParams();
    if (append && state.nextAfter) params.set("after", state.nextAfter);
    if (view === "insights") params.set("memory_type", "insight");
    const endpoint = view === "insights" ? "memories" : view;
    const suffix = params.size ? `?${params}` : "";
    status(msg("Loading {view}…", { view: viewLabel(view) }));
    try {
      const data = await json(route(`${endpoint}${suffix}`));
      if (current(generation) && view === state.view && requestId === state.listRequest) {
        applyList(data, append, detailRequest === state.detailRequest);
        status(msg("{count} current {view} shown.", { count: state.items.length, view: viewLabel(view) }));
      }
    } catch (error) {
      if (current(generation) && view === state.view && requestId === state.listRequest) throw error;
    }
  }

  function applyList(data, append, showEmptyState = true) {
    state.items = append ? state.items.concat(data.items) : data.items;
    state.nextAfter = data.next_after;
    const list = byId("item-list");
    list.replaceChildren();
    for (const item of state.items) {
      const id = state.view === "resources" ? item.resource_id
        : item.kind === "insight" ? item.insight_id : item.memory_id || item.unit_id;
      const kind = state.view === "resources" ? msg("Resource")
        : item.kind === "insight" && item.subtype ? msg("{kind} · {subtype}", { kind: enumLabel(item.kind), subtype: enumLabel(item.subtype) }) : enumLabel(item.kind);
      const row = element("li");
      const control = button(state.view === "resources" ? id : String(item.text || id), () => openItem(item), "list-item");
      if (state.view !== "resources") control.replaceChildren(element("span", item.text || id, "item-title"));
      control.dataset.unitId = item.unit_id || id;
      control.append(element("small", state.view === "resources" ? msg("{kind} · {media} · version {version}", { kind, media: item.media_type, version: item.active_version }) : msg("{kind} · {id}", { kind, id })));
      row.append(control);
      list.append(row);
    }
    byId("load-more").hidden = !state.nextAfter;
    setText(byId("list-summary"), msg(data.total == null ? "{count} shown{support}" : "{count} of {total} shown{support}", { count: state.items.length, total: data.total, support: state.view === "insights" ? msg(" · supported by memories") : "" }));
    if (!state.items.length && showEmptyState) byId("detail").replaceChildren(element("p", msg("No current {view} are available.", { view: viewLabel(state.view) })));
  }

  async function openItem(item) {
    clearSourceMessages();
    if (state.view === "retrieve" && ["fact", "insight"].includes(item.kind)) {
      openMemory(item.kind === "insight" ? item.insight_id : item.memory_id, item.kind);
      return;
    }
    const generation = state.generation;
    const requestId = ++state.detailRequest;
    const view = state.view;
    byId("detail").replaceChildren(element("p", msg("Loading details…")));
    try {
      if (state.view === "resources") {
        const view = state.view;
        const detail = await json(route(resourcePath(item)));
        if (current(generation) && state.view === view && requestId === state.detailRequest) showResource(detail);
      } else if (state.view === "retrieve") {
        byId("detail").replaceChildren(element("h3", msg("Retrieved passage")), element("p", item.text, "source-text"));
        for (const citation of item.citations || []) {
          byId("detail").append(labelValue(msg("Source"), citation.source_id), labelValue(msg("Passage"), citation.passage_id));
        }
      } else {
        const isInsight = item.kind === "insight";
        const id = isInsight ? item.insight_id : item.memory_id;
        const data = await json(route(`memories/${encodeURIComponent(id)}?memory_type=${isInsight ? "insight" : "basic"}`));
        if (!current(generation) || state.view !== view || requestId !== state.detailRequest) return;
        for (const control of byId("item-list").querySelectorAll(".list-item")) {
          control.setAttribute("aria-current", String(control.dataset.unitId === data.unit_id));
        }
        await showMemory(data, generation, requestId);
      }
    } catch (error) {
      if (!current(generation) || state.view !== view || requestId !== state.detailRequest) return;
      showError(error);
      byId("detail").replaceChildren(element("p", msg("This item is no longer available under current policy.")));
    }
  }

  function openMemory(id, kind = "fact") {
    if (state.view !== "memories") setView("memories");
    openItem(kind === "insight" ? { kind, insight_id: id } : { kind, memory_id: id });
  }

  function relationshipLabel(link) {
    const incoming = link.direction === "incoming";
    if (link.kind === "caused_by") return incoming ? msg("Led to") : msg("Caused by");
    if (link.kind === "updates") return incoming ? msg("Updated by") : msg("Updates");
    if (link.kind === "shared_entity") return msg("Shared entity · {name}", { name: link.entity_name });
    if (link.kind === "temporal") {
      const basis = { event: msg("Event time"), referenced: msg("Referenced time"), mentioned: msg("Mentioned time"), created: msg("Source created"), updated: msg("Source updated") }[link.time_basis] || msg("Time-related");
      const seconds = link.gap_seconds;
      if (seconds === 0) return msg("{basis} · overlapping or same time", { basis });
      const [size, unit] = seconds >= 86400 ? [86400, "day"] : seconds >= 3600 ? [3600, "hour"] : [60, "minute"];
      const amount = Math.max(1, Math.round(seconds / size));
      return msg("{basis} · {amount} {unit}{plural} apart", { basis, amount, unit: msg(unit), plural: amount === 1 ? "" : "s" });
    }
    return msg("Similar meaning");
  }

  const MAP_NEIGHBORS_PER_PAGE = 6;
  const MAP_ROW_HEIGHT_PX = 145;
  const connectionKinds = {
    caused_by: [msg("Cause & effect"), "causal"], updates: [msg("Updates"), "updates"],
    temporal: [msg("Time"), "temporal"], shared_entity: [msg("Shared entity"), "entity"],
    semantic: [msg("Similar meaning"), "semantic"],
  };

  function connectionMap(item, links, page, changePage) {
    const region = element("section", undefined, "connection-map");
    setAttribute(region, "aria-label", msg("Connection map"));
    const peers = new Map();
    for (const link of links) {
      const id = link.memory_kind === "insight" ? link.insight_id : link.memory_id;
      const key = `${link.memory_kind}:${id}`;
      if (!peers.has(key)) peers.set(key, { id, kind: link.memory_kind, text: link.text, links: [] });
      peers.get(key).links.push(link);
    }
    const neighbors = [...peers.values()];
    const start = page * MAP_NEIGHBORS_PER_PAGE;
    const shown = neighbors.slice(start, start + MAP_NEIGHBORS_PER_PAGE);
    const sides = [[], []];
    for (const peer of shown) {
      const directed = peer.links.find(link => ["caused_by", "updates"].includes(link.kind));
      const side = directed ? Number(directed.direction === "incoming") : Number(sides[0].length > sides[1].length);
      sides[side].push(peer);
    }
    const height = Math.max(380, (Math.max(...sides.map(side => side.length)) + 1) * MAP_ROW_HEIGHT_PX);
    const canvas = element("div", undefined, "map-canvas");
    canvas.style.height = `${height}px`;
    const svgElement = (tag, attributes) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
      return node;
    };
    const svg = svgElement("svg", { viewBox: `0 0 1000 ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    const defs = svgElement("defs", {});
    for (const kind of ["caused_by", "updates"]) {
      const marker = svgElement("marker", { id: `map-arrow-${kind}`, viewBox: "0 0 8 8", refX: "7", refY: "4", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      marker.append(svgElement("path", { d: "M0,0 L8,4 L0,8 Z", class: `map-arrow ${connectionKinds[kind][1]}` }));
      defs.append(marker);
    }
    svg.append(defs);
    canvas.append(svg);
    const currentNode = element("div", undefined, "map-node map-current");
    currentNode.style.left = "50%";
    currentNode.style.top = "50%";
    currentNode.append(element("strong", msg("This memory")), element("span", item.text, "map-node-text"));
    currentNode.title = item.text;
    canvas.append(currentNode);
    for (const [sideIndex, side] of sides.entries()) {
      for (const [index, peer] of side.entries()) {
        const y = height * (index + 1) / (side.length + 1);
        const edgeGroups = new Map();
        for (const link of peer.links) edgeGroups.set(`${link.kind}:${link.direction}`, link);
        const edgePaths = [];
        for (const [edgeIndex, link] of [...edgeGroups.values()].entries()) {
          const offset = (edgeIndex - (edgeGroups.size - 1) / 2) * 7;
          const x1 = sideIndex ? 640 : 300;
          const x2 = sideIndex ? 700 : 360;
          const y1 = (sideIndex ? height / 2 : y) + offset;
          const y2 = (sideIndex ? y : height / 2) + offset;
          const middle = (x1 + x2) / 2;
          const path = svgElement("path", { d: `M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`, class: `map-edge ${connectionKinds[link.kind][1]}`, "data-kind": link.kind });
          if (["caused_by", "updates"].includes(link.kind)) {
            const pointsToPeer = link.kind === "caused_by" ? link.direction === "incoming" : link.direction === "outgoing";
            path.setAttribute(pointsToPeer === Boolean(sideIndex) ? "marker-end" : "marker-start", `url(#map-arrow-${link.kind})`);
          }
          svg.append(path);
          edgePaths.push(path);
        }
        const node = button("", () => openMemory(peer.id, peer.kind), "map-node secondary");
        setAttribute(node, "aria-label", msg("Open connected memory {id}", { id: peer.id }));
        node.style.left = sideIndex ? "84%" : "16%";
        node.style.top = `${y / height * 100}%`;
        setAttribute(node, "title", () => `${[...new Set(peer.links.map(link => translate(relationshipLabel(link))))].join("; ")}\n${peer.text}`);
        node.append(element("span", peer.text, "map-node-text"), element("small", () => [...new Set(peer.links.map(link => translate(connectionKinds[link.kind][0])))].join(" · ")));
        const highlight = (active) => edgePaths.forEach(path => path.classList.toggle("map-edge-active", active));
        node.addEventListener("pointerenter", () => highlight(true));
        node.addEventListener("pointerleave", () => highlight(false));
        node.addEventListener("focus", () => highlight(true));
        node.addEventListener("blur", () => highlight(false));
        canvas.append(node);
      }
    }
    const legend = element("div", undefined, "map-legend");
    for (const kind of [...new Set(links.map(link => link.kind))]) {
      const [label, className] = connectionKinds[kind];
      legend.append(element("span", label, className));
    }
    region.append(canvas, legend);
    const footer = element("div", undefined, "map-footer");
    footer.append(element("small", msg("{start}–{end} of {total} connected memories", { start: start + 1, end: start + shown.length, total: neighbors.length })));
    if (neighbors.length > MAP_NEIGHBORS_PER_PAGE) {
      const previous = button(msg("Previous"), () => changePage(page - 1));
      previous.disabled = page === 0;
      const next = button(msg("Next"), () => changePage(page + 1));
      next.disabled = start + shown.length >= neighbors.length;
      footer.append(previous, next);
    }
    region.append(footer);
    return region;
  }

  function memoryConnections(item) {
    const wrapper = element("div");
    const entities = element("section", undefined, "memory-section");
    const entityTitle = item.kind === "insight" ? msg("Entities in supporting memories") : msg("Entities in this memory");
    setAttribute(entities, "aria-label", entityTitle);
    entities.append(element("h4", entityTitle));
    const chips = element("div", undefined, "entity-chips");
    const connections = element("section", undefined, "memory-section");
    setAttribute(connections, "aria-label", msg("Memory connections"));
    const links = item.relationships || [];
    let filter = "all";
    let selectedEntity = null;
    let layout = links.length ? "map" : "list";
    let mapPage = 0;
    const layouts = element("div", undefined, "connection-layouts");
    const map = element("div");
    const filters = element("div", undefined, "connection-filters");
    setAttribute(filters, "aria-label", msg("Filter connections"));
    const list = element("ul", undefined, "connection-list");
    const render = () => {
      for (const control of filters.children) control.setAttribute("aria-pressed", String(!selectedEntity && control.dataset.filter === filter));
      for (const chip of chips.children) chip.setAttribute("aria-pressed", String(chip.dataset.entityId === selectedEntity));
      list.replaceChildren();
      map.replaceChildren();
      for (const control of layouts.children) control.setAttribute("aria-pressed", String(control.dataset.layout === layout));
      list.hidden = layout !== "list";
      map.hidden = layout !== "map";
      const visible = links.filter(link => selectedEntity ? link.entity_id === selectedEntity
        : filter === "all" || (filter === "causal" ? link.kind === "caused_by" : filter === "time" ? link.kind === "temporal" : filter === "entities" ? link.kind === "shared_entity" : ["updates", "semantic"].includes(link.kind)));
      if (layout === "map" && visible.length) map.append(connectionMap(item, visible, mapPage, page => { mapPage = page; render(); }));
      for (const link of layout === "list" ? visible : []) {
        const row = element("li", undefined, "connection-item");
        row.append(element("span", relationshipLabel(link), "connection-label"));
        if (link.kind === "temporal" && link.time) {
          const formatTime = (start, end) => start ? () => `${new Date(start).toLocaleString(language)}${end && end !== start ? ` – ${new Date(end).toLocaleString(language)}` : ""}` : msg("Not recorded");
          row.append(element("p", msg("This memory: {time}", { time: formatTime(link.time.memory_start, link.time.memory_end) }), "form-note"));
          row.append(element("p", msg("Related memory: {time}", { time: formatTime(link.time.related_start, link.time.related_end) }), "form-note"));
        }
        row.append(element("p", link.text));
        const id = link.memory_kind === "insight" ? link.insight_id : link.memory_id;
        const control = button(msg("Open memory →"), () => openMemory(id, link.memory_kind));
        setAttribute(control, "aria-label", msg("Open memory {id}", { id }));
        row.append(control);
        list.append(row);
      }
      if (!visible.length) (layout === "map" ? map : list).append(element("p", links.length ? msg("No connections match this filter.") : msg("No stored connections."), "form-note"));
    };
    for (const entity of item.entities || []) {
      const chip = button(entity.name, () => { selectedEntity = selectedEntity === entity.entity_id ? null : entity.entity_id; mapPage = 0; render(); }, "entity-chip secondary");
      chip.dataset.entityId = entity.entity_id;
      setAttribute(chip, "title", msg("Mentioned as {mention}. Show connections through this entity.", { mention: entity.mention }));
      chips.append(chip);
    }
    entities.append(chips);
    if (!chips.children.length) entities.append(element("p", msg("No supported entities recorded."), "form-note"));
    if (chips.children.length > 1 && item.kind !== "insight") {
      entities.append(element("p", msg("Mentioned together: {names}", { names: item.entities.map(entity => entity.name).join(" ↔ ") }), "form-note"));
      entities.append(element("p", msg("The memory above describes their relationship; co-mentions alone do not establish a typed relationship."), "form-note"));
    }
    connections.append(element("h4", msg("Memory connections")));
    if (links.length) {
      for (const [key, label] of [["map", msg("Map")], ["list", msg("List")]]) {
        const control = button(label, () => { layout = key; render(); });
        control.dataset.layout = key;
        layouts.append(control);
      }
      connections.append(layouts);
      for (const [key, label] of [["all", msg("All")], ["causal", msg("Causal")], ["time", msg("Time")], ["entities", msg("Shared entities")], ["other", msg("Updates & similar")]]) {
        const control = button(label, () => { filter = key; selectedEntity = null; mapPage = 0; render(); });
        control.dataset.filter = key;
        filters.append(control);
      }
      connections.append(filters);
    }
    connections.append(map, list);
    if (item.relationships_truncated) connections.append(element("p", msg("Showing the first 50 connections. Causal and update links appear first."), "form-note"));
    render();
    wrapper.append(entities, connections);
    return wrapper;
  }

  async function showMemory(item, generation, requestId) {
    const detail = byId("detail");
    const citations = item.citations || [];
    const isInsight = item.kind === "insight";
    const targetId = isInsight ? item.insight_id : item.memory_id;
    const revision = item.insight_revision ?? item.memory_revision ?? item.revision;
    const sourceMessages = byId("source-messages");
    sourceMessages.replaceChildren();
    byId("source-panel").hidden = false;
    byId("workspace-grid").classList.add("with-memory");
    if (citations.length) sourceMessages.append(element("p", msg("Loading source messages…")));
    else sourceMessages.append(element("p", msg("No source messages available.")));
    detail.replaceChildren(
      element("span", item.kind === "insight" ? msg("Insight") : msg("Memory"), "badge"),
      element("h3", targetId),
      element("h4", item.kind === "insight" ? msg("Insight") : msg("Extracted fact")),
      element("p", item.text, "source-text"),
      memoryConnections(item),
      labelValue(msg("Origin"), enumLabel(item.origin)),
      labelValue(msg("Canonical ID"), targetId),
      labelValue(msg("Revision"), revision),
    );
    if (isInsight && item.subtype) detail.append(labelValue(msg("Subtype"), enumLabel(item.subtype)));
    detail.append(element("h4", msg("Supporting memories")));
    if (item.supports?.length) {
      for (const support of item.supports) {
        detail.append(labelValue(msg("Memory"), support.memory_id));
        const control = button(msg("Open supporting memory →"), () => openMemory(support.memory_id));
        setAttribute(control, "aria-label", msg("Open supporting memory {id}", { id: support.memory_id }));
        detail.append(control);
        if (support.support_revision !== undefined) detail.append(labelValue(msg("Support revision"), support.support_revision));
      }
    } else {
      detail.append(element("p", msg("No supporting memories recorded.")));
    }
    const affected = item.affected_insight_ids || [];
    detail.append(element("h4", msg("Affected dependent Insights")));
    if (affected.length) for (const insightId of affected) detail.append(button(insightId, () => openMemory(insightId, "insight")));
    else detail.append(element("p", msg("No dependent Insights are affected.")));
    detail.append(element("div", undefined, "actions"));
    detail.lastElementChild.append(
      button(isInsight ? msg("Remove Insight") : msg("Remove Memory Unit"), () => removeMemoryUnit(item)),
    );
    detail.append(element("h4", msg("Source provenance")));
    for (const citation of citations) {
      const row = element("div", undefined, "citation");
      row.append(labelValue(msg("Source"), citation.source_id || msg("Unknown")));
      row.append(labelValue(msg("Version"), citation.source_version));
      row.append(labelValue(msg("Passage"), citation.passage_id));
      if (citation.source_id) {
        row.append(button(msg("Inspect source"), () => inspectSource(citation.source_id)));
        row.append(button(msg("Delete this source"), () => deleteSource(citation.source_id, false)));
      }
      detail.append(row);
    }
    if (!citations.length) return;
    const sourceIds = [...new Set(citations.map((citation) => citation.source_id).filter(Boolean))];
    const sources = await Promise.all(sourceIds.map(async (sourceId) => {
      try { return await json(route(`evidence/${encodeURIComponent(sourceId)}`)); }
      catch (_) { return null; }
    }));
    if (!current(generation) || !["memories", "insights"].includes(state.view) || requestId !== state.detailRequest) return;
    sourceMessages.replaceChildren();
    for (const [index, source] of sources.entries()) {
      const group = element("div", undefined, "citation");
      group.append(element("h4", sourceIds[index]));
      if (!source) group.append(element("p", msg("Source messages unavailable.")));
      else if (!source.original?.messages?.length) group.append(element("p", msg("No source messages available.")));
      else for (const message of source.original.messages) {
        group.append(labelValue(msg("Role"), enumLabel(message.role)));
        group.append(element("pre", typeof message.content === "string" ? message.content : JSON.stringify(message.content), "source-text"));
      }
      sourceMessages.append(group);
    }
    if (!sources.length) sourceMessages.append(element("p", msg("No source messages available.")));
  }

  async function removeMemoryUnit(item) {
    const isInsight = item.kind === "insight";
    const targetId = isInsight ? item.insight_id : item.memory_id;
    const target = msg("{type} {id}", { type: isInsight ? msg("Insight") : msg("Memory Unit"), id: targetId });
    if (!targetId) {
      status(msg("This item has no canonical removal ID."), true);
      return;
    }
    if (!window.confirm(translate(msg("Remove {target}? The selected {type} will be excluded without deleting its source.", { target, type: isInsight ? msg("Insight") : msg("Memory Unit") })))) return;
    const generation = state.generation;
    try {
      const endpoint = isInsight ? `insights/${encodeURIComponent(targetId)}` : `memories/${encodeURIComponent(targetId)}`;
      const receipt = await json(route(endpoint), { method: "DELETE" });
      if (!current(generation)) return;
      const removalGeneration = ++state.generation;
      state.detailRequest += 1;
      state.items = [];
      state.nextAfter = null;
      byId("item-list").replaceChildren();
      byId("load-more").hidden = true;
      try {
        await loadList(false);
      } catch (error) {
        if (current(removalGeneration)) {
          showReceipt(receipt.operation_id, receipt);
          status(msg("{target} excluded; list refresh failed: {error}", { target, error: errorMessage(error) }), true);
        }
        return;
      }
      if (current(removalGeneration)) {
        showReceipt(receipt.operation_id, receipt);
        status(msg("{target} excluded. Cleanup is {status}.", { target, status: enumLabel(receipt.cleanup_status) }));
      }
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function inspectSource(sourceId) {
    clearSourceMessages();
    const generation = state.generation;
    const view = state.view;
    const requestId = ++state.detailRequest;
    try {
      const source = await json(route(`evidence/${encodeURIComponent(sourceId)}`));
      if (!currentDetail(generation, view, requestId)) return;
      clearSourceMessages();
      const detail = byId("detail");
      detail.replaceChildren(
        element("span", msg("Source Evidence"), "badge"),
        element("h3", sourceId),
        labelValue(msg("Active version"), source.active_version),
        labelValue("SHA-256", source.sha256),
        element("pre", JSON.stringify(source.original, null, 2), "source-text"),
        button(msg("Delete this source"), () => deleteSource(sourceId, false)),
      );
      if (source.passages?.length) {
        detail.append(element("h4", msg("Retained passages")));
        for (const passage of source.passages) {
          detail.append(labelValue(msg("Locator"), JSON.stringify(passage.locator)));
          detail.append(element("p", passage.text, "source-text"));
        }
      }
      status(msg("Current source {id} opened.", { id: sourceId }));
    } catch (error) { if (currentDetail(generation, view, requestId)) showError(error); }
  }

  const resourcePath = (item) => `resources/${encodeURIComponent(item.resource_id)}`;

  function showResource(item) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("span", msg("Resource"), "badge"),
      element("h3", item.resource_id),
      labelValue(msg("Media type"), item.media_type),
      labelValue(msg("Active version"), item.active_version),
      labelValue(msg("Original bytes"), item.byte_length),
      labelValue("SHA-256", item.sha256),
      labelValue(msg("Index operation"), item.operation_status ? enumLabel(item.operation_status) : msg("Pending")),
    );
    detail.append(labelValue(msg("Abstract"), enumLabel(item.abstract_status)));
    if (item.abstract) detail.append(element("p", item.abstract, "source-text"));
    const actions = element("div", undefined, "actions");
    actions.append(button(msg("Inspect original"), () => inspectOriginal(item)));
    actions.append(button(msg("Download original"), () => downloadOriginal(item)));
    actions.append(button(msg("Revise"), () => showResourceForm(item)));
    actions.append(button(msg("Delete Resource"), () => deleteSource(item.resource_id, "resources")));
    if (item.operation_id) actions.append(button(msg("Operation status"), () => showOperation(item.operation_id)));
    detail.append(actions);
    if (item.passages?.length) {
      detail.append(element("h4", msg("Extracted passages")));
      for (const passage of item.passages) {
        const row = element("div", undefined, "citation");
        row.append(labelValue(msg("Locator"), JSON.stringify(passage.locator)));
        row.append(element("p", passage.text, "source-text"));
        detail.append(row);
      }
    }
    if (item.passages_truncated) detail.append(element("p", msg("More passages exist than this detail view displays.")));
  }

  async function inspectOriginal(item) {
    const generation = state.generation;
    try {
      if (!item.media_type.startsWith("text/") && item.media_type !== "application/json") {
        status(msg("This original is binary. Use Download original to inspect it in a local viewer."));
        return;
      }
      const response = await request(route(`${resourcePath(item)}/original`));
      const text = await response.text();
      if (!current(generation)) return;
      const preview = element("pre", text, "original-preview");
      byId("detail").append(element("h4", msg("Original source text")), preview);
      status(msg("Original rendered as text. Source markup was not executed."));
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function downloadOriginal(item) {
    const generation = state.generation;
    try {
      const response = await request(route(`${resourcePath(item)}/original`));
      const blob = await response.blob();
      if (!current(generation)) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "original";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status(msg("Original download started."));
    } catch (error) { if (current(generation)) showError(error); }
  }

  function field(title, control) {
    const label = element("label");
    label.append(element("span", title));
    label.append(control);
    return label;
  }

  function showResourceForm(existing = null) {
    state.detailRequest += 1;
    const detail = byId("detail");
    const form = element("form", undefined, "resource-form");
    const id = element("input"); id.required = true; id.value = existing ? existing.resource_id : ""; id.readOnly = !!existing;
    const evidenceInputs = element("div", undefined, "resource-inputs");
    const method = element("select");
    for (const value of ["text", "url", "file"]) {
      const option = element("option", value === "url" ? msg("HTTPS URL") : value === "file" ? msg("Upload file") : msg("Text"));
      option.value = value; method.append(option);
    }
    const text = element("textarea"); text.rows = 7;
    const url = element("input"); url.type = "url"; url.placeholder = "https://example.org/report.pdf";
    const media = element("select");
    for (const value of ["text/html", "application/pdf", "image/png", "image/jpeg"]) {
      const option = element("option", value); option.value = value; media.append(option);
    }
    const file = element("input"); file.type = "file"; file.accept = ".html,.pdf,.png,.jpg,.jpeg";
    const textField = field(msg("Original text"), text);
    const urlField = field(msg("HTTPS URL"), url);
    const mediaField = field(msg("Declared media type"), media);
    const fileField = field(msg("Original file"), file);
    evidenceInputs.append(field(msg("Input"), method), textField, urlField, mediaField, fileField);

    const update = () => {
      textField.hidden = method.value !== "text";
      urlField.hidden = method.value !== "url";
      mediaField.hidden = method.value !== "url";
      fileField.hidden = method.value !== "file";
    };
    method.addEventListener("change", update);
    form.addEventListener("input", () => { state.draftKey = null; });
    form.append(
      element("h3", existing ? msg("Revise {id} (expected version {version})", { id: existing.resource_id, version: existing.active_version }) : msg("Add Resource")),
      field(msg("Resource ID"), id), evidenceInputs,
    );
    update();
    const submit = element("button", existing ? msg("Create immutable revision") : msg("Create Resource"));
    submit.type = "submit";
    form.append(submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const generation = state.generation;
      if (!state.draftKey) state.draftKey = crypto.randomUUID();
      const headers = { "Idempotency-Key": state.draftKey };
      if (existing) headers["If-Match"] = String(existing.active_version);
      let body;
      const metadata = { input_type: "document", document_id: id.value.trim(), update_mode: "replace" };
      if (existing) metadata.user_id = state.user;
      if (method.value === "file") {
        if (!file.files.length) { status(msg("Select an original file."), true); return; }
        body = new FormData();
        body.append("metadata", JSON.stringify(metadata));
        body.append("original", file.files[0]);
      } else if (method.value === "url") {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify({ ...metadata, input_type: "document_url", media_type: media.value, url: url.value });
      } else {
        headers["Content-Type"] = "application/json";
        body = JSON.stringify({ ...metadata, text: text.value });
      }
      const path = existing ? route(resourcePath(existing)) : route("resources");
      const verb = existing ? "PUT" : "POST";
      try {
        const receipt = await json(path, { method: verb, headers, body });
        if (!current(generation)) return;
        state.draftKey = null;
        status(msg("Accepted version {version}; operation {id}.", { version: receipt.revision, id: receipt.operation_id }));
        showReceipt(receipt.operation_id, receipt);
        await loadList(false);
      } catch (error) { if (current(generation)) showError(error); }
    });
    detail.replaceChildren(form);
    id.focus();
  }

  function showReceipt(operationId, receipt) {
    clearSourceMessages();
    const detail = byId("detail");
    detail.replaceChildren(
      element("h3", msg("Durable operation")),
      labelValue(msg("Operation ID"), operationId),
      element("pre", JSON.stringify(receipt, null, 2), "source-text"),
      button(msg("Refresh operation status"), () => showOperation(operationId)),
    );
  }

  async function showOperation(operationId) {
    const generation = state.generation;
    const view = state.view;
    const requestId = view === "profiles" ? ++state.profileRequest : null;
    try {
      const operation = await json(route(`operations/${encodeURIComponent(operationId)}`));
      if (!current(generation) || state.view !== view || (requestId !== null && requestId !== state.profileRequest)) return;
      byId("detail").replaceChildren(
        element("h3", msg("Operation {id}", { id: operationId })),
        labelValue(msg("Status"), enumLabel(operation.status)),
        element("pre", JSON.stringify(operation.readiness || operation, null, 2), "source-text"),
        button(msg("Refresh status"), () => showOperation(operationId)),
      );
      if (state.view === "profiles") {
        byId("detail").append(button(msg("View User Profile"), loadProfile));
      }
      status(msg("Operation {status}.", { status: enumLabel(operation.status) }));
    } catch (error) {
      if (current(generation) && state.view === view && (requestId === null || requestId === state.profileRequest)) showError(error);
    }
  }

  async function deleteSource(sourceId, collection) {
    const type = collection ? msg("Resource") : msg("chat source");
    const target = msg("{type} {id}", { type, id: sourceId });
    if (!window.confirm(translate(msg("Delete {target}? All versions and dependent passages, memories, Insights, and User Profile fields become unavailable immediately. Cleanup is tracked separately.", { target })))) return;
    const generation = state.generation;
    try {
      const endpoint = `${collection || "evidence"}/${encodeURIComponent(sourceId)}`;
      const receipt = await json(route(endpoint), { method: "DELETE" });
      if (!current(generation)) return;
      const deletionGeneration = ++state.generation;
      state.items = [];
      state.nextAfter = null;
      byId("item-list").replaceChildren();
      byId("load-more").hidden = true;
      showReceipt(receipt.cleanup_operation_id, receipt);
      status(msg("{target} excluded. Cleanup operation {id} is {status}.", { target, id: receipt.cleanup_operation_id, status: enumLabel(receipt.cleanup_status) }));
      try {
        await loadList(false);
      } catch (error) {
        if (current(deletionGeneration)) status(msg("{target} excluded; list refresh failed: {error}", { target, error: errorMessage(error) }), true);
        return;
      }
      if (current(deletionGeneration)) {
        showReceipt(receipt.cleanup_operation_id, receipt);
        status(msg("{target} excluded. Cleanup operation {id} is {status}.", { target, id: receipt.cleanup_operation_id, status: enumLabel(receipt.cleanup_status) }));
      }
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function search(event) {
    event.preventDefault();
    const generation = state.generation;
    const view = state.view;
    const requestId = ++state.detailRequest;
    const listRequest = ++state.listRequest;
    const query = byId("query").value.trim();
    if (!query) return;
    clearSourceMessages();
    if (view === "retrieve") {
      state.items = [];
      state.nextAfter = null;
      byId("load-more").hidden = true;
      byId("item-list").replaceChildren();
      setText(byId("list-summary"), "");
      byId("detail").replaceChildren(element("h3", msg("Retrieving evidence…")), element("p", msg("Searching current evidence…")));
    }
    const mode = view === "retrieve" ? byId("recall-level").value : "fast";
    const started = performance.now();
    try {
      status(msg("Searching current evidence…"));
      const result = await json("/v1/recall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.user, query, recall_level: mode, allow_partial: true }),
      });
      if (!current(generation) || state.view !== view || requestId !== state.detailRequest || listRequest !== state.listRequest) return;
      const retrieval = { result, query, mode, duration: ((performance.now() - started) / 1000).toFixed(2) };
      if (view === "retrieve") {
        applyList({ items: result.items, next_after: null }, false);
      }
      showRetrieval(retrieval);
      status(msg("{count} cited items returned.", { count: result.items.length }));
    } catch (error) {
      if (current(generation) && state.view === view && requestId === state.detailRequest && listRequest === state.listRequest) {
        showError(error);
        byId("detail").replaceChildren(element("h3", msg("Retrieval failed")), element("p", errorMessage(error)), element("p", msg("Try retrieving again after the service is available.")));
      }
    }
  }

  function showRetrieval({ result, query, mode, duration }) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("h3", msg("Current search context")),
      element("p", query),
      labelValue(msg("Mode"), enumLabel(mode)),
      labelValue(msg("Elapsed"), msg("{seconds}s", { seconds: duration })),
      labelValue(msg("Estimated tokens"), result.context_tokens),
      labelValue(msg("Coverage"), result.diagnostics?.partial ? msg("Partial") : msg("Current")),
      element("pre", result.context || msg("No matching current evidence."), "source-text"),
    );
    if (result.diagnostics?.partial) detail.append(element("p", msg("Some indexing or retrieval stages are incomplete."), "form-note"));
    if (result.trace_id) detail.append(button(msg("Explain this search"), () => explain(result.trace_id)));
  }

  async function explain(traceId) {
    const generation = state.generation;
    const view = state.view;
    const requestId = ++state.detailRequest;
    try {
      const trace = await json(route(`debug/traces/${encodeURIComponent(traceId)}`));
      if (!currentDetail(generation, view, requestId)) return;
      clearSourceMessages();
      byId("detail").replaceChildren(
        element("h3", msg("Trace {id}", { id: traceId })),
        labelValue(msg("Recorded status"), enumLabel(trace.status)),
        element("pre", JSON.stringify(trace.decisions || {}, null, 2), "source-text"),
      );
      status(msg("Trace {status}.", { status: enumLabel(trace.status) }));
    } catch (error) { if (currentDetail(generation, view, requestId)) showError(error); }
  }

  initializeLanguage();

  byId("connect-form").addEventListener("submit", connect);
  byId("sign-out").addEventListener("click", signOut);
  byId("refresh-list").addEventListener("click", () => loadList(false).catch(showError));
  byId("load-more").addEventListener("click", () => loadList(true).catch(showError));
  byId("new-resource").addEventListener("click", () => showResourceForm());
  byId("search-form").addEventListener("submit", search);
  byId("profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const instruction = byId("profile-instruction").value.trim();
    if (!instruction) {
      status(msg("Enter custom instructions before applying them."), true);
      return;
    }
    refreshProfile({ instruction });
  });
  byId("refresh-profile").addEventListener("click", () => refreshProfile());
  byId("profile-use-default").addEventListener("click", () => refreshProfile({ reset_instruction: true }));
  for (const tab of document.querySelectorAll("[data-view]")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
})();
