(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const current = (generation) => generation === state.generation;
  const state = {
    credential: "",
    tenant: "",
    user: "",
    view: "memories",
    items: [],
    nextAfter: null,
    generation: 0,
    draftKey: null,
  };

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function status(message, isError = false) {
    const node = byId("status");
    node.textContent = message;
    node.dataset.state = isError ? "error" : "info";
  }

  function resetContent() {
    state.generation += 1;
    state.items = [];
    state.nextAfter = null;
    state.draftKey = null;
    byId("query").value = "";
    byId("item-list").replaceChildren();
    byId("detail").replaceChildren(element("p", "Select an item to inspect its source and readiness."));
    byId("load-more").hidden = true;
    byId("workspace").hidden = true;
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
    row.append(element("strong", label), element("span", value ?? "Unavailable"));
    return row;
  }

  function showError(error) {
    status(error.message || "Request failed", true);
  }

  async function connect(event) {
    event.preventDefault();
    resetContent();
    state.tenant = byId("tenant").value.trim();
    state.user = byId("user").value.trim();
    state.credential = byId("credential").value;
    byId("credential").value = "";
    const generation = state.generation;
    status("Checking access…");
    try {
      const [memories] = await Promise.all([
        json(route("memories")),
        json(route("resources")),
      ]);
      if (generation !== state.generation) return;
      byId("scope-label").textContent = `${state.tenant} / ${state.user}`;
      byId("connection").hidden = true;
      byId("workspace").hidden = false;
      state.view = "memories";
      setView("memories", memories);
      status("Connected. Current source policy is applied to every view.");
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
    status("Signed out. Page-held content and credential were cleared.");
    byId("user").focus();
  }

  function setView(view, initial) {
    state.view = view;
    state.items = [];
    state.nextAfter = null;
    byId("item-list").replaceChildren();
    byId("detail").replaceChildren();
    byId("list-title").textContent = view === "resources" ? "Resources" : view === "profiles" ? "Profiles" : "Memories";
    byId("new-resource").hidden = view !== "resources";
    byId("search-form").hidden = view !== "memories";
    byId("refresh-list").hidden = view === "profiles";
    for (const tab of document.querySelectorAll("[data-view]")) {
      tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
    }
    if (view === "profiles") {
      byId("detail").append(element("p", "Profile sections and freshness become available after the Profile capability is installed."));
      byId("load-more").hidden = true;
      status("Profile capability is unavailable in this build.");
      return;
    }
    if (initial) {
      applyList(initial, false);
    } else {
      loadList(false).catch(showError);
    }
  }

  async function loadList(append) {
    const view = state.view;
    const generation = state.generation;
    const after = append && state.nextAfter ? `?after=${encodeURIComponent(state.nextAfter)}` : "";
    status(`Loading ${view}…`);
    try {
      const data = await json(route(`${view}${after}`));
      if (current(generation) && view === state.view) {
        applyList(data, append);
        status(`${state.items.length} current ${view} shown.`);
      }
    } catch (error) {
      if (current(generation)) throw error;
    }
  }

  function applyList(data, append) {
    state.items = append ? state.items.concat(data.items) : data.items;
    state.nextAfter = data.next_after;
    const list = byId("item-list");
    list.replaceChildren();
    for (const item of state.items) {
      const id = state.view === "resources" ? item.resource_id : item.unit_id;
      const kind = state.view === "resources" ? item.resource_type : item.kind;
      const caption = state.view === "resources" ? `${item.media_type} · version ${item.active_version}` : item.text;
      const row = element("li");
      const control = button(id, () => openItem(item), "list-item");
      control.append(element("small", `${kind} · ${String(caption || "").slice(0, 110)}`));
      row.append(control);
      list.append(row);
    }
    byId("load-more").hidden = !state.nextAfter;
    if (!state.items.length) byId("detail").replaceChildren(element("p", `No current ${state.view} are available.`));
  }

  async function openItem(item) {
    const generation = state.generation;
    try {
      if (state.view === "resources") {
        const detail = await json(route(`resources/${encodeURIComponent(item.resource_id)}`));
        if (generation === state.generation && state.view === "resources") showResource(detail);
      } else {
        showMemory(item);
      }
    } catch (error) {
      if (!current(generation)) return;
      showError(error);
      byId("detail").replaceChildren(element("p", "This item is no longer available under current policy."));
    }
  }

  function showMemory(item) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("span", item.kind === "insight" ? "L2 Insight" : "L1 Memory", "badge"),
      element("h3", item.unit_id),
      element("p", item.text, "source-text"),
      labelValue("Origin", item.origin),
    );
    if (item.supports?.length) {
      detail.append(element("h4", "Supporting memories"));
      for (const support of item.supports) detail.append(element("p", support.memory_id));
    }
    detail.append(element("h4", "Source provenance"));
    for (const citation of item.citations || []) {
      const row = element("div", undefined, "citation");
      row.append(labelValue("Source", citation.source_id || "Unknown"));
      row.append(labelValue("Version", citation.source_version));
      row.append(labelValue("Passage", citation.passage_id));
      if (citation.source_id) {
        row.append(button("Inspect source", () => inspectSource(citation.source_id)));
        row.append(button("Delete this source", () => deleteSource(citation.source_id, false)));
      }
      detail.append(row);
    }
  }

  async function inspectSource(sourceId) {
    const generation = state.generation;
    try {
      const source = await json(route(`evidence/${encodeURIComponent(sourceId)}`));
      if (!current(generation)) return;
      const detail = byId("detail");
      detail.replaceChildren(
        element("span", "L0 Source Evidence", "badge"),
        element("h3", sourceId),
        labelValue("Active version", source.active_version),
        labelValue("SHA-256", source.sha256),
        element("pre", JSON.stringify(source.original, null, 2), "source-text"),
        button("Delete this source", () => deleteSource(sourceId, false)),
      );
      if (source.passages?.length) {
        detail.append(element("h4", "Retained passages"));
        for (const passage of source.passages) {
          detail.append(labelValue("Locator", JSON.stringify(passage.locator)));
          detail.append(element("p", passage.text, "source-text"));
        }
      }
      status(`Current source ${sourceId} opened.`);
    } catch (error) { if (current(generation)) showError(error); }
  }

  function showResource(item) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("span", "Evidence Resource", "badge"),
      element("h3", item.resource_id),
      labelValue("Media type", item.media_type),
      labelValue("Active version", item.active_version),
      labelValue("Original bytes", item.byte_length),
      labelValue("SHA-256", item.sha256),
      labelValue("Index operation", item.operation_status || "Unavailable"),
      labelValue("Abstract", item.abstract_status),
    );
    if (item.abstract) detail.append(element("p", item.abstract, "source-text"));
    const actions = element("div", undefined, "actions");
    actions.append(button("Inspect original", () => inspectOriginal(item)));
    actions.append(button("Download original", () => downloadOriginal(item)));
    actions.append(button("Revise", () => showResourceForm(item)));
    actions.append(button("Delete Resource", () => deleteSource(item.resource_id, true)));
    if (item.operation_id) actions.append(button("Operation status", () => showOperation(item.operation_id)));
    detail.append(actions);
    if (item.passages?.length) {
      detail.append(element("h4", "Extracted passages"));
      for (const passage of item.passages) {
        const row = element("div", undefined, "citation");
        row.append(labelValue("Locator", JSON.stringify(passage.locator)));
        row.append(element("p", passage.text, "source-text"));
        detail.append(row);
      }
    }
    if (item.passages_truncated) detail.append(element("p", "More passages exist than this detail view displays."));
  }

  async function inspectOriginal(item) {
    const generation = state.generation;
    try {
      if (!item.media_type.startsWith("text/")) {
        status("This original is binary. Use Download original to inspect it in a local viewer.");
        return;
      }
      const response = await request(route(`resources/${encodeURIComponent(item.resource_id)}/original`));
      const text = await response.text();
      if (!current(generation)) return;
      const preview = element("pre", text, "original-preview");
      byId("detail").append(element("h4", "Original source text"), preview);
      status("Original rendered as text. Source markup was not executed.");
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function downloadOriginal(item) {
    const generation = state.generation;
    try {
      const response = await request(route(`resources/${encodeURIComponent(item.resource_id)}/original`));
      const blob = await response.blob();
      if (!current(generation)) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "original";
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      status("Original download started.");
    } catch (error) { if (current(generation)) showError(error); }
  }

  function field(title, control) {
    const label = element("label", title);
    label.append(control);
    return label;
  }

  function showResourceForm(existing = null) {
    const detail = byId("detail");
    const form = element("form", undefined, "resource-form");
    const id = element("input"); id.required = true; id.value = existing?.resource_id || ""; id.readOnly = !!existing;
    const method = element("select");
    for (const value of ["text", "url", "file"]) {
      const option = element("option", value === "url" ? "HTTPS URL" : value === "file" ? "Upload file" : "Text");
      option.value = value; method.append(option);
    }
    const text = element("textarea"); text.rows = 7;
    const url = element("input"); url.type = "url"; url.placeholder = "https://example.org/report.pdf";
    const media = element("select");
    for (const value of ["text/html", "application/pdf", "image/png", "image/jpeg"]) {
      const option = element("option", value); option.value = value; media.append(option);
    }
    const file = element("input"); file.type = "file"; file.accept = ".html,.pdf,.png,.jpg,.jpeg";
    const textField = field("Original text", text);
    const urlField = field("HTTPS URL", url);
    const mediaField = field("Declared media type", media);
    const fileField = field("Original file", file);
    const update = () => {
      textField.hidden = method.value !== "text";
      urlField.hidden = method.value !== "url";
      mediaField.hidden = method.value !== "url";
      fileField.hidden = method.value !== "file";
    };
    method.addEventListener("change", update);
    form.addEventListener("input", () => { state.draftKey = null; });
    form.append(
      element("h3", existing ? `Revise ${existing.resource_id} (expected version ${existing.active_version})` : "Add Evidence Resource"),
      field("Resource ID", id), field("Input", method), textField, urlField, mediaField, fileField,
    );
    update();
    const submit = element("button", existing ? "Create immutable revision" : "Create Resource");
    submit.type = "submit";
    form.append(submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const generation = state.generation;
      if (!state.draftKey) state.draftKey = crypto.randomUUID();
      const metadata = { user_id: state.user, input_type: "document", document_id: id.value.trim(), update_mode: "replace" };
      const headers = { "Idempotency-Key": state.draftKey };
      if (existing) headers["If-Match"] = String(existing.active_version);
      let body;
      if (method.value === "file") {
        if (!file.files.length) { status("Select an original file.", true); return; }
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
      const path = existing ? route(`resources/${encodeURIComponent(existing.resource_id)}`) : "/v1/retain";
      try {
        const receipt = await json(path, { method: existing ? "PUT" : "POST", headers, body });
        if (!current(generation)) return;
        state.draftKey = null;
        status(`Accepted version ${receipt.revision}; operation ${receipt.operation_id}.`);
        showReceipt(receipt.operation_id, receipt);
        await loadList(false);
      } catch (error) { if (current(generation)) showError(error); }
    });
    detail.replaceChildren(form);
    id.focus();
  }

  function showReceipt(operationId, receipt) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("h3", "Durable operation"),
      labelValue("Operation ID", operationId),
      element("pre", JSON.stringify(receipt, null, 2), "source-text"),
      button("Refresh operation status", () => showOperation(operationId)),
    );
  }

  async function showOperation(operationId) {
    const generation = state.generation;
    try {
      const operation = await json(route(`operations/${encodeURIComponent(operationId)}`));
      if (!current(generation)) return;
      byId("detail").replaceChildren(
        element("h3", `Operation ${operationId}`),
        labelValue("Status", operation.status),
        element("pre", JSON.stringify(operation.readiness || operation, null, 2), "source-text"),
        button("Refresh status", () => showOperation(operationId)),
      );
      status(`Operation ${operation.status}.`);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function deleteSource(sourceId, resource) {
    const type = resource ? "Resource" : "chat source";
    const target = `${type} ${sourceId}`;
    if (!window.confirm(`Delete ${target}? All versions and dependent passages, memories, and Insights become unavailable immediately. Cleanup is tracked separately.`)) return;
    const generation = state.generation;
    try {
      const endpoint = resource ? `resources/${encodeURIComponent(sourceId)}` : `evidence/${encodeURIComponent(sourceId)}`;
      const receipt = await json(route(endpoint), { method: "DELETE" });
      if (!current(generation)) return;
      const deletionGeneration = ++state.generation;
      state.items = [];
      state.nextAfter = null;
      byId("item-list").replaceChildren();
      byId("load-more").hidden = true;
      showReceipt(receipt.cleanup_operation_id, receipt);
      status(`${target} excluded. Cleanup operation ${receipt.cleanup_operation_id} is ${receipt.cleanup_status}.`);
      try {
        await loadList(false);
      } catch (error) {
        if (current(deletionGeneration)) status(`${target} excluded; list refresh failed: ${error.message}`, true);
        return;
      }
      if (current(deletionGeneration)) {
        showReceipt(receipt.cleanup_operation_id, receipt);
        status(`${target} excluded. Cleanup operation ${receipt.cleanup_operation_id} is ${receipt.cleanup_status}.`);
      }
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function search(event) {
    event.preventDefault();
    const generation = state.generation;
    const query = byId("query").value.trim();
    if (!query) return;
    try {
      status("Searching current evidence…");
      const result = await json("/v1/recall", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: state.user, query, recall_level: "fast", profile_sections: [], allow_partial: true }),
      });
      if (!current(generation)) return;
      const detail = byId("detail");
      detail.replaceChildren(
        element("h3", "Current search context"),
        labelValue("Estimated tokens", result.context_tokens),
        labelValue("Coverage", result.diagnostics?.partial ? "Partial" : "Current"),
        element("pre", result.context || "No matching current evidence.", "source-text"),
      );
      if (result.trace_id) detail.append(button("Explain this search", () => explain(result.trace_id)));
      status(`${result.items.length} cited items returned.`);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function explain(traceId) {
    const generation = state.generation;
    try {
      const trace = await json(route(`debug/traces/${encodeURIComponent(traceId)}`));
      if (!current(generation)) return;
      byId("detail").replaceChildren(
        element("h3", `Trace ${traceId}`),
        labelValue("Recorded status", trace.status),
        element("pre", JSON.stringify(trace.decisions || {}, null, 2), "source-text"),
      );
      status(`Trace ${trace.status}.`);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function createExport() {
    const generation = state.generation;
    try {
      status("Capturing current export…");
      const receipt = await json(route("exports"), { method: "POST" });
      if (!current(generation)) return;
      const manifest = await json(route(`exports/${encodeURIComponent(receipt.export_id)}`));
      if (!current(generation)) return;
      byId("detail").replaceChildren(
        element("h3", `Export ${receipt.export_id}`),
        labelValue("State", receipt.state),
        labelValue("Records", Object.values(manifest.counts).reduce((sum, value) => sum + value, 0)),
        labelValue("Originals", manifest.originals.length),
        element("p", "Download the manifest, record parts, and originals through the public API. Place them at their manifest paths and run oam validate-export on that directory."),
      );
      status(`Export ${receipt.export_id} is ready under the current source policy.`);
    } catch (error) { if (current(generation)) showError(error); }
  }

  byId("connect-form").addEventListener("submit", connect);
  byId("sign-out").addEventListener("click", signOut);
  byId("refresh-list").addEventListener("click", () => loadList(false).catch(showError));
  byId("load-more").addEventListener("click", () => loadList(true).catch(showError));
  byId("new-resource").addEventListener("click", () => showResourceForm());
  byId("search-form").addEventListener("submit", search);
  byId("export-button").addEventListener("click", createExport);
  for (const tab of document.querySelectorAll("[data-view]")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
})();
