(() => {
  "use strict";

  const LANGUAGE_STORAGE_KEY = "oam-inspector-language";
  const bindings = new WeakMap();
  const msg = (key, params = {}) => ({ uiMessage: true, key, params });
  let language = "en";

  function translate(value) {
    if (typeof value === "function") return translate(value());
    if (!value?.uiMessage) return String(value ?? "");
    const template = INSPECTOR_LOCALES[language]?.[value.key] ?? value.key;
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
  const viewLabel = (view, title = false) => msg(title ? { resources: "Resources", profiles: "User Profile", memories: "Memories", insights: "Insights", retrieve: "Retrieved evidence", entities: "Entities", "request-logs": "Request Logs" }[view] : view);
  const enumValues = new Set(["fact", "insight", "memory", "basic", "preferences", "patterns", "projects", "relationships", "experiences", "general_insights", "chat", "interaction", "evidence", "resource", "pending", "processing", "succeeded", "partial", "failed", "cancelled", "ready", "missing", "blocked", "unavailable", "complete", "completed", "not_required", "fast", "mid", "deep", "normal", "user", "assistant", "system", "developer", "tool"]);
  const enumLabel = (value) => enumValues.has(value) ? msg(value) : value;

  function errorMessage(error) {
    const code = error.message || "Unknown";
    if (error instanceof TypeError) return msg("Unable to reach the service. Check the connection and try again.");
    if (code === "RecallUnavailable") return msg("Retrieval is currently unavailable ({code}).", { code });
    return msg("Request failed ({code}).", { code });
  }

  function supportedLanguage(value) {
    const locale = String(value || "").toLowerCase();
    const [base, ...subtags] = locale.split("-");
    if (base === "zh") {
      if (subtags.includes("hans")) return "zh-CN";
      if (subtags.some(tag => ["hant", "tw", "hk", "mo"].includes(tag))) return "zh-TW";
      return "zh-CN";
    }
    return base === "en" || Object.hasOwn(INSPECTOR_LOCALES, base) ? base : null;
  }

  function setLanguage(value, persist = false) {
    language = supportedLanguage(value) || "en";
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
    const browserLanguage = (navigator.languages?.length ? navigator.languages : [navigator.language]).map(supportedLanguage).find(Boolean);
    setLanguage(supportedLanguage(saved) || browserLanguage || "en");
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
    selectedItemKey: null,
    nextAfter: null,
    generation: 0,
    draftKey: null,
    profileRequest: 0,
    detailRequest: 0,
    listRequest: 0,
    logErrorsOnly: false,
    logOffset: 0,
    entityLayout: "list",
    graphDispose: null,
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
    disposeEntityGraph();
    closeEntity();
    state.generation += 1;
    state.items = [];
    state.selectedItemKey = null;
    state.nextAfter = null;
    state.draftKey = null;
    state.profileRequest += 1;
    state.detailRequest += 1;
    state.listRequest += 1;
    byId("recall-level").value = "fast";
    byId("query").value = "";
    byId("entity-search-form").reset();
    byId("log-filter-form").reset();
    setLogScope(false);
    state.logOffset = 0;
    byId("log-page-size").value = "10";
    setText(byId("log-total-calls"), "—");
    setText(byId("log-success-rate"), "—");
    setText(byId("request-scope"), "");
    setText(byId("log-page-summary"), "");
    byId("log-page-buttons").replaceChildren();
    state.requestScope = null;
    state.savedProfileInstruction = "";
    byId("profile-instruction").value = "";
    setText(byId("profile-instruction-status"), "");
    setText(byId("profile-default-prompt"), "");
    byId("profile-prompt-details").open = false;
    byId("profile-customization").open = false;
    byId("profile-refresh-mode").value = "incremental";
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
    disposeEntityGraph();
    closeEntity();
    state.logOffset = 0;
    state.view = view;
    state.detailRequest += 1;
    state.listRequest += 1;
    state.items = [];
    state.selectedItemKey = null;
    state.nextAfter = null;
    byId("item-list").replaceChildren();
    byId("detail").replaceChildren(element("p", msg("Loading details…")));
    setText(byId("list-summary"), "");
    byId("load-more").hidden = true;
    setText(byId("list-title"), viewLabel(view, true));
    byId("new-resource").hidden = view !== "resources";
    byId("entity-search-form").hidden = view !== "entities";
    byId("log-filter-form").hidden = view !== "request-logs";
    byId("log-note").hidden = true;
    byId("request-overview").hidden = view !== "request-logs";
    byId("log-pagination").hidden = view !== "request-logs";
    byId("entity-layout-controls").hidden = view !== "entities";
    byId("entity-description").hidden = view !== "entities";
    byId("entity-graph").hidden = view !== "entities" || state.entityLayout !== "relationships";
    byId("workspace-grid").classList.toggle("entities-view", view === "entities");
    document.querySelector(".detail-panel").hidden = ["entities", "request-logs"].includes(view);
    if (view === "request-logs") setText(byId("list-title"), msg("Captured requests"));
    byId("workspace-grid").classList.toggle("logs-view", view === "request-logs");
    byId("search-form").hidden = !["memories", "retrieve"].includes(view);
    setText(byId("query-label"), view === "retrieve" ? msg("What would you like to retrieve?") : msg("Search current memory"));
    byId("retrieval-mode-field").hidden = view !== "retrieve";
    setText(byId("search-form").querySelector("button"), view === "retrieve" ? msg("Retrieve evidence") : msg("Search"));
    byId("profile-form").hidden = view !== "profiles";
    byId("refresh-profile").hidden = view !== "profiles";
    byId("profile-refresh-controls").hidden = view !== "profiles";
    setText(byId("detail-title"), view === "profiles" ? msg("User Profile") : msg("Details"));
    document.querySelector(".list-panel").hidden = view === "profiles";
    byId("workspace-grid").classList.toggle("profile-view", view === "profiles");
    byId("item-list").hidden = view === "profiles" || (view === "entities" && state.entityLayout === "relationships");
    byId("refresh-list").hidden = ["profiles", "retrieve", "request-logs"].includes(view);
    for (const tab of document.querySelectorAll("[data-view]")) {
      tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
    }
    if (view === "retrieve") {
      byId("detail").replaceChildren(element("p", msg("Enter a query to retrieve cited evidence. Open a result to inspect it.")));
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
    if (blockedInputs > 0) detail.append(element("p", msg("{count} memory input{plural} could not be prepared. Check the input operation before refreshing the User Profile.", { count: blockedInputs, plural: blockedInputs === 1 ? "" : "s" })));
    if (failedInputs > 0 || uncoveredInputs > 0) {
      const generationDetails = element("details");
      generationDetails.append(element("summary", msg("Generation details")));
      if (failedInputs > 0) {
        generationDetails.append(labelValue(msg("Failed Insight inputs"), failedInputs));
        generationDetails.append(element("p", msg("{count} Insight input{plural} failed. {lead} only ready information.", { count: failedInputs, plural: failedInputs === 1 ? "" : "s", lead: profile.content ? msg("This User Profile uses") : msg("A refreshed User Profile can use") })));
      }
      if (uncoveredInputs > 0) generationDetails.append(labelValue(msg("Insights outside this summary"), uncoveredInputs));
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
    body = { ...body, mode: byId("profile-refresh-mode").value };
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

  async function loadList(append, updateDetail = true) {
    const view = state.view;
    if (!["memories", "insights", "resources", "entities", "request-logs"].includes(view)) return;
    const generation = state.generation;
    const requestId = ++state.listRequest;
    const detailRequest = state.detailRequest;
    const params = new URLSearchParams();
    if (append && state.nextAfter && view !== "request-logs") params.set("after", state.nextAfter);
    if (view === "insights") params.set("memory_type", "insight");
    if (view === "entities") params.set("query", byId("entity-query").value.trim());
    if (view === "request-logs") {
      if (byId("log-operation").value.trim()) params.set("operation", byId("log-operation").value.trim());
      if (byId("log-request-id").value.trim()) params.set("request_id", byId("log-request-id").value.trim());
      if (state.logErrorsOnly) params.set("errors_only", "true");
      if (byId("log-status").value) params.set("status_code", byId("log-status").value);
      params.set("limit", byId("log-page-size").value);
      params.set("offset", String(state.logOffset));
    }
    const isGraph = view === "entities" && state.entityLayout === "relationships";
    const endpoint = isGraph ? "entities/graph" : view === "insights" ? "memories" : view;
    const suffix = params.size ? `?${params}` : "";
    status(msg("Loading {view}…", { view: viewLabel(view) }));
    try {
      const data = await json(route(`${endpoint}${suffix}`));
      if (current(generation) && view === state.view && requestId === state.listRequest) {
        if (isGraph) {
          disposeEntityGraph();
          state.items = data.nodes;
          state.graphDispose = drawEntityGraph(byId("entity-graph"), data, { element, button, msg, setAttribute, setText, onSelect: openEntity, onError: showError });
          setText(byId("list-summary"), msg("{count} entities", { count: data.total }));
          if (data.truncated || data.edges_truncated) byId("entity-graph").append(element("p", msg("Showing up to 500 entities and 5,000 co-occurrences. Search to narrow the graph."), "graph-bound-note"));
          if (!data.nodes.length) byId("entity-graph").append(element("p", msg("No entities found."), "graph-empty"));
        } else applyList(data, append, updateDetail && detailRequest === state.detailRequest);
        status(msg("{count} current {view} shown.", { count: state.items.length, view: viewLabel(view) }));
      }
    } catch (error) {
      if (current(generation) && view === state.view && requestId === state.listRequest) {
        if (updateDetail && detailRequest === state.detailRequest && !state.items.length) byId("detail").replaceChildren(element("p", errorMessage(error)));
        throw error;
      }
    }
  }

  function itemKey(item) {
    return item.resource_id ? `resource:${item.resource_id}`
      : item.kind === "insight" ? `insight:${item.insight_id}` : `memory:${item.memory_id || item.unit_id}`;
  }

  function applyList(data, append, updateDetail = true) {
    state.items = append ? state.items.concat(data.items) : data.items;
    state.nextAfter = data.next_after;
    const list = byId("item-list");
    list.replaceChildren();
    if (["entities", "request-logs"].includes(state.view)) {
      if (state.view === "request-logs") state.requestScope = data.scope;
      renderInspectionTable(list);
      byId("load-more").hidden = state.view === "request-logs" || !state.nextAfter;
      if (state.view === "request-logs") renderRequestOverview(data);
      setText(byId("list-summary"), state.view === "entities" ? msg("{count} entities", { count: data.total }) : "");
      if (!state.items.length && updateDetail) byId("detail").replaceChildren(element("p", msg(state.view === "entities" ? "No entities found." : "No requests match these filters.")));
      return;
    }
    for (const item of state.items) {
      const id = state.view === "resources" ? item.resource_id
        : item.kind === "insight" ? item.insight_id : item.memory_id || item.unit_id;
      const kind = state.view === "resources" ? msg("Resource")
        : item.kind === "insight" && item.subtype ? msg("{kind} · {subtype}", { kind: enumLabel(item.kind), subtype: enumLabel(item.subtype) }) : enumLabel(item.kind);
      const row = element("li");
      const control = button(state.view === "resources" ? id : String(item.text || id), () => openItem(item), "list-item");
      if (state.view !== "resources") control.replaceChildren(element("span", item.text || id, "item-title"));
      control.dataset.unitId = item.unit_id || id;
      control.setAttribute("aria-current", String(itemKey(item) === state.selectedItemKey));
      control.append(element("small", state.view === "resources" ? msg("{kind} · {media} · version {version}", { kind, media: item.media_type, version: item.active_version }) : msg("{kind} · {id}", { kind, id })));
      row.append(control);
      list.append(row);
    }
    byId("load-more").hidden = !state.nextAfter;
    setText(byId("list-summary"), msg(data.total == null ? "{count} shown{support}" : "{count} of {total} shown{support}", { count: state.items.length, total: data.total, support: state.view === "insights" ? msg(" · supported by memories") : "" }));
    if (!append && updateDetail && ["memories", "insights", "resources"].includes(state.view)) {
      if (!state.items.length) {
        state.detailRequest += 1;
        state.selectedItemKey = null;
        clearSourceMessages();
        byId("detail").replaceChildren(element("p", msg("No current {view} are available.", { view: viewLabel(state.view) })));
      } else if (!state.items.some(item => itemKey(item) === state.selectedItemKey)) {
        openItem(state.items[0]);
      }
    }
  }

  function displayTime(value) {
    return value ? new Date(value).toLocaleString(language) : translate(msg("Unavailable"));
  }

  function setLogScope(errorsOnly) {
    state.logErrorsOnly = errorsOnly;
    byId("log-all").setAttribute("aria-pressed", String(!errorsOnly));
    byId("log-errors").setAttribute("aria-pressed", String(errorsOnly));
  }

  function disposeEntityGraph() {
    state.graphDispose?.();
    state.graphDispose = null;
    byId("entity-graph").replaceChildren();
  }

  function closeEntity() {
    const drawer = byId("entity-drawer");
    state.detailRequest += 1;
    if (drawer.open) drawer.close();
    byId("entity-drawer-content").replaceChildren();
    setText(byId("entity-drawer-title"), "");
  }

  function requestParts(operation) {
    const separator = operation.indexOf(" ");
    const method = separator < 0 ? "" : operation.slice(0, separator);
    const path = separator < 0 ? operation : operation.slice(separator + 1);
    const parts = path.split("/");
    return { method, path, type: parts[2] === "users" ? parts[4] || "users" : parts[2] || "" };
  }

  function renderRequestOverview(data) {
    const summary = data.summary || {};
    setText(byId("log-total-calls"), summary.total_calls == null ? "—" : String(summary.total_calls));
    setText(byId("log-success-rate"), summary.success_rate == null ? "—" : `${Number(summary.success_rate).toFixed(0)}%`);
    const scope = data.scope || {};
    setText(byId("request-scope"), msg("Current scope: {tenant} / {user}", { tenant: scope.tenant_id || state.tenant, user: scope.user_id || state.user }));
    const limit = data.limit || Number(byId("log-page-size").value), offset = data.offset || 0;
    const page = Math.floor(offset / limit) + 1, pages = Math.max(1, Math.ceil(data.total / limit));
    setText(byId("log-page-summary"), msg("{total} requests · Page {page} / {pages}", { total: data.total, page, pages }));
    const controls = byId("log-page-buttons");
    const go = next => { state.logOffset = (next - 1) * limit; loadList(false).catch(showError); };
    const previous = button(msg("Previous page"), () => go(page - 1));
    previous.disabled = page <= 1;
    controls.replaceChildren(previous);
    let previousNumber = 0;
    for (const number of [...new Set([1, page - 1, page, page + 1, pages])].filter(n => n >= 1 && n <= pages).sort((a, b) => a - b)) {
      if (previousNumber && number > previousNumber + 1) controls.append(element("span", "…", "pagination-gap"));
      previousNumber = number;
      const control = button(String(number), () => go(number));
      if (number === page) control.setAttribute("aria-current", "page");
      controls.append(control);
    }
    const next = button(msg("Next page"), () => go(page + 1));
    next.disabled = page >= pages;
    controls.append(next);
  }

  function renderInspectionTable(list) {
    const entities = state.view === "entities";
    const row = element("li", undefined, "table-scroll");
    const table = element("table", undefined, "inspection-table");
    const header = element("tr");
    const labels = entities ? ["Name", "Linked memories", "First seen", "Last seen"] : ["Time", "API type", "Method", "Path", "HTTP status", "Duration", "Request ID", "Account ID", "User ID"];
    for (const label of labels) {
      const cell = element("th", msg(label)); cell.scope = "col";
      if (entities && label === "Linked memories") cell.setAttribute("aria-sort", "descending");
      header.append(cell);
    }
    const head = element("thead"); head.append(header);
    const body = element("tbody");
    for (const item of state.items) {
      const tr = element("tr");
      if (entities) {
        const name = element("td");
        name.append(button(item.name, () => openEntity(item), "table-link secondary"));
        tr.append(name, element("td", String(item.memory_count)), element("td", () => displayTime(item.first_mentioned_at)), element("td", () => displayTime(item.last_mentioned_at)));
      } else {
        const code = element("span", String(item.status), "http-status");
        code.dataset.state = item.status >= 400 ? "error" : "success";
        if (item.error_code) code.title = item.error_code;
        const statusCell = element("td"); statusCell.append(code);
        const id = element("td");
        const copy = button(item.request_id, async () => {
          try { await navigator.clipboard.writeText(item.request_id); status(msg("Request ID copied.")); }
          catch (_) { status(msg("Could not copy. Select the Request ID to copy it manually."), true); }
        }, "table-link secondary request-id");
        setAttribute(copy, "title", msg("Copy Request ID"));
        id.append(copy);
        const parts = requestParts(item.operation);
        tr.append(element("td", () => displayTime(item.time)), element("td", parts.type, "request-api"), element("td", parts.method, "request-method"), element("td", parts.path, "request-operation"), statusCell, element("td", `${Number(item.duration_ms).toFixed(1)} ms`), id, element("td", state.requestScope?.tenant_id || state.tenant), element("td", state.requestScope?.user_id || state.user));
      }
      body.append(tr);
    }
    table.append(head, body); row.append(table);
    list.append(row);
    if (!state.items.length) {
      const empty = element("p", msg(entities ? "No entities found." : "No requests match these filters."), "table-empty");
      row.append(empty);
    }
  }

  function highlightedChunk(text, mentions, quotes, spans) {
    const paragraph = element("pre", undefined, "entity-source-chunk");
    // Python offsets count Unicode code points, so convert before slicing in JS.
    const chars = Array.from(text);
    let located = spans;
    if (located == null) {
      located = [];
      for (const name of new Set(mentions.filter(Boolean))) {
        const start = text.indexOf(name);
        // Ambiguous legacy occurrences stay unhighlighted.
        if (start >= 0 && text.indexOf(name, start + name.length) < 0 &&
            quotes.some(quote => quote && quote.includes(name) && text.includes(quote))) {
          const left = Array.from(text.slice(0, start)).length;
          located.push([left, left + Array.from(name).length]);
        }
      }
    }
    let start = 0;
    for (const [left, right] of located.filter(span => Array.isArray(span) && span.length === 2)
      .sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
      if (!Number.isInteger(left) || !Number.isInteger(right) || left < start || right <= left || right > chars.length) continue;
      paragraph.append(document.createTextNode(chars.slice(start, left).join("")), element("mark", chars.slice(left, right).join("")));
      start = right;
    }
    paragraph.append(document.createTextNode(chars.slice(start).join("")));
    return paragraph;
  }

  async function openEntity(item) {
    const generation = state.generation, requestId = ++state.detailRequest, view = state.view;
    const drawer = byId("entity-drawer"), content = byId("entity-drawer-content");
    setText(byId("entity-drawer-title"), item.name);
    content.replaceChildren(element("p", msg("Loading details…")));
    if (!drawer.open) drawer.showModal();
    const memories = element("div", undefined, "entity-timeline");
    const displayedChunks = new Map();
    const originalNames = element("p"), aliases = element("p");
    const nameSets = { original_names: new Set(), aliases: new Set() };
    const moreNames = button(msg("Load more names"), () => loadNames(moreNames.dataset.after));
    function appendNames(data) {
      for (const key of ["original_names", "aliases"]) for (const name of data[key] || []) nameSets[key].add(name);
      originalNames.textContent = [...nameSets.original_names].join(", ") || "—";
      aliases.textContent = [...nameSets.aliases].join(", ") || "—";
      moreNames.hidden = !data.names_next_after; moreNames.dataset.after = data.names_next_after || "";
    }
    async function loadNames(namesAfter) {
      moreNames.disabled = true;
      try {
        const data = await json(route(`entities/${encodeURIComponent(item.entity_id)}/memories?${new URLSearchParams({ names_after: namesAfter })}`));
        if (currentDetail(generation, view, requestId) && drawer.open) appendNames(data);
      } catch (error) { if (currentDetail(generation, view, requestId) && drawer.open) showError(error); }
      finally { moreNames.disabled = false; }
    }
    const more = button(msg("Load more"), () => loadPage(more.dataset.after));
    async function loadPage(after) {
      more.disabled = true;
      try {
        const suffix = after ? `?${new URLSearchParams({ after })}` : "";
        const data = await json(route(`entities/${encodeURIComponent(item.entity_id)}/memories${suffix}`));
        if (!currentDetail(generation, view, requestId) || !drawer.open) return;
        if (!after) {
          const info = element("div", undefined, "entity-info-grid");
          info.append(labelValue(msg("Linked memories"), String(data.memory_count ?? item.memory_count)), labelValue(msg("First seen"), () => displayTime(data.first_mentioned_at || item.first_mentioned_at)));
          setText(byId("entity-drawer-title"), data.name);
          appendNames(data);
          content.replaceChildren(info, labelValue(msg("Entity ID"), item.entity_id), element("h4", msg("Original names")), originalNames, element("h4", msg("Aliases")), aliases, moreNames, element("h4", msg("Original chunks")), element("p", msg("Recorded entity mentions are highlighted in their original source chunks."), "form-note"), memories, more);
        }
        for (const memory of data.items) {
          for (const chunk of memory.source_chunks || []) {
            const key = JSON.stringify([chunk.source_id, chunk.source_version, chunk.passage_id]);
            const previous = displayedChunks.get(key);
            if (previous) {
              previous.mentions.push(...chunk.mentions);
              previous.quotes.push(chunk.quote);
              if (chunk.mention_spans != null) previous.spans = [...(previous.spans || []), ...chunk.mention_spans];
              const paragraph = highlightedChunk(chunk.text, previous.mentions, previous.quotes, previous.spans);
              previous.paragraph.replaceWith(paragraph);
              previous.paragraph = paragraph;
              continue;
            }
            const entry = element("div", undefined, "entity-timeline-entry");
            const paragraph = highlightedChunk(chunk.text, chunk.mentions || [], [chunk.quote], chunk.mention_spans);
            entry.append(element("small", () => displayTime(memory.mention_time || memory.created_at)), paragraph,
              element("small", msg("Source {source} · chunk {passage}", { source: chunk.source_id, passage: chunk.passage_id }), "form-note"),
              button(msg("Open learned memory"), () => { closeEntity(); openMemory(memory.memory_id); }, "secondary"));
            memories.append(entry);
            displayedChunks.set(key, { paragraph, mentions: [...(chunk.mentions || [])], quotes: [chunk.quote], spans: chunk.mention_spans });
          }
        }
        if (!displayedChunks.size && !data.next_after) memories.replaceChildren(element("p", msg("No original chunks are currently available."), "form-note"));
        more.hidden = !data.next_after; more.dataset.after = data.next_after || "";
      } catch (error) {
        if (currentDetail(generation, view, requestId) && drawer.open) { content.replaceChildren(element("p", errorMessage(error))); showError(error); }
      } finally { more.disabled = false; }
    }
    await loadPage(null);
  }

  async function openItem(item) {
    clearSourceMessages();
    state.selectedItemKey = itemKey(item);
    for (const [index, control] of [...byId("item-list").querySelectorAll(".list-item")].entries()) {
      control.setAttribute("aria-current", String(itemKey(state.items[index]) === state.selectedItemKey));
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
      } else if (state.view === "retrieve" && !["fact", "insight"].includes(item.kind)) {
        byId("detail").replaceChildren(element("h3", msg("Retrieved passage")), element("p", item.text, "source-text"));
        if (item.excerpt) byId("detail").append(element("p", msg("Excerpt: characters {start}–{end} of {total}", {
          start: item.excerpt.start + 1, end: item.excerpt.end, total: item.excerpt.original_length,
        }), "form-note"));
        for (const citation of item.citations || []) {
          byId("detail").append(labelValue(msg("Source"), citation.source_id), labelValue(msg("Passage"), citation.passage_id));
        }
      } else {
        const isInsight = item.kind === "insight";
        const id = isInsight ? item.insight_id : item.memory_id;
        const data = await json(route(`memories/${encodeURIComponent(id)}?memory_type=${isInsight ? "insight" : "basic"}`));
        if (!current(generation) || state.view !== view || requestId !== state.detailRequest) return;
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
      return msg("{basis} · {duration} apart", { basis, duration: () => new Intl.NumberFormat(language, { style: "unit", unit, unitDisplay: "long" }).format(amount) });
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
    if (!current(generation) || !["memories", "insights", "retrieve"].includes(state.view) || requestId !== state.detailRequest) return;
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
        await loadList(false, false);
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
      const view = state.view;
      const requestId = state.detailRequest;
      const listRequest = state.listRequest;
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
        if (currentDetail(generation, view, requestId)) showReceipt(receipt.operation_id, receipt);
        if (listRequest === state.listRequest) await loadList(false, false);
      } catch (error) { if (current(generation)) showError(error); }
    });
    detail.replaceChildren(form);
    id.focus();
  }

  function showReceipt(operationId, receipt) {
    clearSourceMessages();
    state.detailRequest += 1;
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
        await loadList(false, false);
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
      labelValue(msg("Estimated context tokens"), result.context_tokens),
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
  for (const id of ["entity-search-form", "log-filter-form"]) {
    byId(id).addEventListener("submit", event => {
      event.preventDefault();
      state.detailRequest += 1;
      state.logOffset = 0;
      byId("detail").replaceChildren();
      if (state.view === "entities") disposeEntityGraph();
      loadList(false).catch(showError);
    });
  }
  for (const [id, errorsOnly] of [["log-all", false], ["log-errors", true]]) {
    byId(id).addEventListener("click", () => {
      setLogScope(errorsOnly);
      state.detailRequest += 1;
      state.logOffset = 0;
      byId("detail").replaceChildren();
      if (state.view === "entities") disposeEntityGraph();
      loadList(false).catch(showError);
    });
  }
  byId("close-entity").addEventListener("click", closeEntity);
  byId("entity-drawer").addEventListener("cancel", () => { state.detailRequest += 1; });
  for (const [id, layout] of [["entity-relations", "relationships"], ["entity-list", "list"]]) {
    byId(id).addEventListener("click", () => {
      state.entityLayout = layout;
      for (const [key, value] of [["entity-relations", "relationships"], ["entity-list", "list"]]) byId(key).setAttribute("aria-pressed", String(value === layout));
      setView("entities");
    });
  }
  byId("log-page-size").addEventListener("change", () => { state.logOffset = 0; loadList(false).catch(showError); });
  byId("reset-logs").addEventListener("click", () => {
    byId("log-filter-form").reset(); setLogScope(false); state.logOffset = 0; loadList(false).catch(showError);
  });
  byId("refresh-logs").addEventListener("click", () => loadList(false).catch(showError));
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
