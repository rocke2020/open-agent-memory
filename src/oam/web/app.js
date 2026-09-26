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
    profileRequest: 0,
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
    state.profileRequest += 1;
    byId("query").value = "";
    byId("profile-budget").value = "";
    for (const input of document.querySelectorAll('#profile-form input[name="section"]')) {
      input.checked = input.value === "main";
    }
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
    byId("profile-form").hidden = view !== "profiles";
    byId("item-list").hidden = view === "profiles";
    byId("refresh-list").hidden = view === "profiles";
    for (const tab of document.querySelectorAll("[data-view]")) {
      tab.setAttribute("aria-pressed", String(tab.dataset.view === view));
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

  function selectedProfileSections() {
    const sections = [...document.querySelectorAll('#profile-form input[name="section"]:checked')]
      .map((input) => input.value);
    if (!sections.length) throw new Error("Select at least one Profile section.");
    return sections;
  }

  async function loadProfile() {
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      const sections = selectedProfileSections();
      const params = new URLSearchParams();
      for (const section of sections) params.append("sections", section);
      const budget = byId("profile-budget").value;
      if (budget !== "") params.set("max_tokens", budget);
      status("Loading Profile…");
      const profile = await json(route(`profile?${params}`));
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return false;
      showProfile(profile, sections);
      status(`Profile ${profile.freshness}; ${profile.tokens} estimated preview tokens.`);
      return true;
    } catch (error) {
      if (current(generation) && state.view === "profiles" && requestId === state.profileRequest) showError(error);
      return false;
    }
  }

  function showProfile(profile, sections) {
    const detail = byId("detail");
    detail.replaceChildren(
      element("span", "L3 Profile", "badge"),
      element("h3", "Read-only Profile"),
      labelValue("Freshness", profile.freshness),
      labelValue("Revision", profile.profile_revision ?? "No stored revision"),
      labelValue("Estimated preview tokens", profile.tokens),
      labelValue("Pending inputs", profile.pending_inputs),
    );
    for (const section of sections) {
      const fields = profile.sections?.[section] || [];
      detail.append(element("h4", `${section} (${fields.length})`));
      if (!fields.length) detail.append(element("p", "No grounded fields in this section."));
      for (const field of fields) {
        const row = element("div", undefined, "citation");
        row.append(labelValue(field.key, typeof field.value === "string" ? field.value : JSON.stringify(field.value)));
        if (field.qualifier) row.append(labelValue("Qualifier", field.qualifier));
        row.append(labelValue("State", field.state));
        for (const support of field.supports || []) {
          const id = support.resource_id || support.id || support.memory_id || support.insight_id;
          const supportRow = labelValue(`Support · ${support.kind}`, id);
          if (support.kind === "profile_resource") {
            supportRow.append(button("Inspect Resource", () => openProfileResource(support.resource_id)));
          }
          row.append(supportRow);
        }
        detail.append(row);
      }
    }
    for (const omitted of profile.omitted || []) {
      detail.append(labelValue(`Omitted ${omitted.field_id}`, omitted.reason));
    }
  }

  async function openProfileResource(resourceId) {
    setView("resources");
    const generation = state.generation;
    try {
      const resource = await json(route(`resources/${encodeURIComponent(resourceId)}`));
      if (current(generation) && state.view === "resources") showResource(resource);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function refreshProfile() {
    let sections;
    try { sections = selectedProfileSections(); }
    catch (error) { showError(error); return; }
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      status("Checking Profile inputs…");
      const outcome = await json(route("profile/refresh"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sections }),
      });
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return;
      if (outcome.status === "unchanged") {
        if (await loadProfile()) status("Nothing new to refresh. Stored Profile reused with zero generation calls.");
      } else {
        showReceipt(outcome.operation_id, outcome);
        byId("detail").append(button("View Profile", loadProfile));
        status(`Profile refresh pending; operation ${outcome.operation_id}.`);
      }
    } catch (error) {
      if (current(generation) && state.view === "profiles" && requestId === state.profileRequest) showError(error);
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
    const isProfile = item.resource_type === "profile";
    detail.replaceChildren(
      element("span", isProfile ? "Profile Resource" : "Evidence Resource", "badge"),
      element("h3", item.resource_id),
      labelValue("Media type", item.media_type),
      labelValue("Active version", item.active_version),
      labelValue("Original bytes", item.byte_length),
      labelValue("SHA-256", item.sha256),
      labelValue("Index operation", item.operation_status || "Pending"),
    );
    if (isProfile) {
      detail.append(labelValue("Registered subtype", item.subtype));
      for (const mapped of item.mapped_fields || []) {
        detail.append(
          labelValue(
            `${mapped.section} · ${mapped.key}`,
            typeof mapped.value === "string" ? mapped.value : JSON.stringify(mapped.value),
          ),
        );
      }
    } else {
      detail.append(labelValue("Abstract", item.abstract_status));
      if (item.abstract) detail.append(element("p", item.abstract, "source-text"));
    }
    const actions = element("div", undefined, "actions");
    actions.append(button("Inspect original", () => inspectOriginal(item)));
    actions.append(button("Download original", () => downloadOriginal(item)));
    actions.append(button("Revise", () => editResource(item)));
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

  async function editResource(item) {
    const generation = state.generation;
    try {
      const original = item.resource_type === "profile"
        ? await json(route(`resources/${encodeURIComponent(item.resource_id)}/original`))
        : null;
      if (current(generation) && state.view === "resources") showResourceForm(item, original);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function inspectOriginal(item) {
    const generation = state.generation;
    try {
      if (!item.media_type.startsWith("text/") && item.media_type !== "application/json") {
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

  function showResourceForm(existing = null, original = null) {
    const detail = byId("detail");
    const form = element("form", undefined, "resource-form");
    const id = element("input"); id.required = true; id.value = existing?.resource_id || ""; id.readOnly = !!existing;
    const resourceType = element("select");
    for (const value of ["evidence", "profile"]) {
      const option = element("option", value === "profile" ? "Profile Resource" : "Evidence Resource");
      option.value = value; resourceType.append(option);
    }
    resourceType.value = existing?.resource_type || "evidence";
    resourceType.disabled = !!existing;
    const evidenceInputs = element("div", undefined, "resource-inputs");
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
    evidenceInputs.append(field("Input", method), textField, urlField, mediaField, fileField);

    const profileInputs = element("div", undefined, "resource-inputs");
    const subtype = element("select");
    for (const value of ["basic-profile/v1", "preferences/v1"]) {
      const option = element("option", value); option.value = value; subtype.append(option);
    }
    subtype.value = original?.subtype || "basic-profile/v1";
    const basicInputs = element("div", undefined, "resource-inputs");
    const dateOfBirth = element("input"); dateOfBirth.type = "date"; dateOfBirth.value = original?.content?.date_of_birth || "";
    const sex = element("input"); sex.value = original?.content?.sex || "";
    const householdSize = element("input"); householdSize.type = "number"; householdSize.min = "1";
    householdSize.value = original?.content?.household_size || "";
    const interests = element("input"); interests.value = (original?.content?.interests || []).join(", ");
    basicInputs.append(
      field("Date of birth", dateOfBirth), field("Sex", sex),
      field("Household size", householdSize), field("Interests (comma separated)", interests),
    );
    const preferenceInputs = element("div", undefined, "resource-inputs");
    const exampleLanguage = element("input"); exampleLanguage.value = original?.content?.example_language || "";
    const domain = element("input"); domain.value = original?.content?.domain || "";
    preferenceInputs.append(field("Example language", exampleLanguage), field("Domain", domain));
    const sourceRevision = element("input"); sourceRevision.value = original?.source_revision || "";
    const asOf = element("input"); asOf.placeholder = "2026-09-26T00:00:00+00:00";
    asOf.value = original?.as_of || "";
    profileInputs.append(
      field("Registered subtype", subtype), basicInputs, preferenceInputs,
      field("Source revision (optional)", sourceRevision), field("As of (timezone-aware, optional)", asOf),
    );

    const update = () => {
      evidenceInputs.hidden = resourceType.value !== "evidence";
      profileInputs.hidden = resourceType.value !== "profile";
      textField.hidden = method.value !== "text";
      urlField.hidden = method.value !== "url";
      mediaField.hidden = method.value !== "url";
      fileField.hidden = method.value !== "file";
      basicInputs.hidden = subtype.value !== "basic-profile/v1";
      preferenceInputs.hidden = subtype.value !== "preferences/v1";
    };
    method.addEventListener("change", update);
    subtype.addEventListener("change", update);
    resourceType.addEventListener("change", update);
    form.addEventListener("input", () => { state.draftKey = null; });
    form.append(
      element("h3", existing ? `Revise ${existing.resource_id} (expected version ${existing.active_version})` : "Add Resource"),
      field("Resource ID", id), field("Resource type", resourceType), evidenceInputs, profileInputs,
    );
    update();
    const submit = element("button", existing ? "Create immutable revision" : "Create Resource");
    submit.type = "submit";
    form.append(submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const generation = state.generation;
      if (!state.draftKey) state.draftKey = crypto.randomUUID();
      const headers = { "Idempotency-Key": state.draftKey };
      if (existing) headers["If-Match"] = String(existing.active_version);
      let body;
      let path;
      let verb;
      if (resourceType.value === "profile") {
        const content = {};
        if (subtype.value === "basic-profile/v1") {
          if (dateOfBirth.value) content.date_of_birth = dateOfBirth.value;
          if (sex.value.trim()) content.sex = sex.value.trim();
          if (householdSize.value) content.household_size = Number(householdSize.value);
          if (interests.value.trim()) content.interests = interests.value.split(",").map((part) => part.trim()).filter(Boolean);
          if (!Object.keys(content).length) { status("Enter at least one registered Profile field.", true); return; }
        } else {
          if (!exampleLanguage.value.trim() || !domain.value.trim()) {
            status("Example language and domain are required.", true); return;
          }
          content.example_language = exampleLanguage.value.trim();
          content.domain = domain.value.trim();
        }
        const profile = {
          resource_id: id.value.trim(), resource_type: "profile", subtype: subtype.value, content,
        };
        if (sourceRevision.value.trim()) profile.source_revision = sourceRevision.value.trim();
        if (asOf.value.trim()) profile.as_of = asOf.value.trim();
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(profile);
        path = route("resources");
        verb = "POST";
      } else {
        const metadata = { input_type: "document", document_id: id.value.trim(), update_mode: "replace" };
        if (existing) metadata.user_id = state.user;
        else metadata.resource_type = "evidence";
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
        path = existing ? route(`resources/${encodeURIComponent(existing.resource_id)}`) : route("resources");
        verb = existing ? "PUT" : "POST";
      }
      try {
        const receipt = await json(path, { method: verb, headers, body });
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
    const view = state.view;
    const requestId = view === "profiles" ? ++state.profileRequest : null;
    try {
      const operation = await json(route(`operations/${encodeURIComponent(operationId)}`));
      if (!current(generation) || state.view !== view || (requestId !== null && requestId !== state.profileRequest)) return;
      byId("detail").replaceChildren(
        element("h3", `Operation ${operationId}`),
        labelValue("Status", operation.status),
        element("pre", JSON.stringify(operation.readiness || operation, null, 2), "source-text"),
        button("Refresh status", () => showOperation(operationId)),
      );
      if (state.view === "profiles") {
        byId("detail").append(button("View Profile", loadProfile));
      }
      status(`Operation ${operation.status}.`);
    } catch (error) {
      if (current(generation) && state.view === view && (requestId === null || requestId === state.profileRequest)) showError(error);
    }
  }

  async function deleteSource(sourceId, resource) {
    const type = resource ? "Resource" : "chat source";
    const target = `${type} ${sourceId}`;
    if (!window.confirm(`Delete ${target}? All versions and dependent passages, memories, Insights, and Profile fields become unavailable immediately. Cleanup is tracked separately.`)) return;
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
  byId("profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadProfile().catch(showError);
  });
  byId("refresh-profile").addEventListener("click", refreshProfile);
  byId("export-button").addEventListener("click", createExport);
  for (const tab of document.querySelectorAll("[data-view]")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
})();
