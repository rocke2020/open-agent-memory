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
    detailRequest: 0,
    savedProfileInstruction: "",
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
    state.detailRequest += 1;
    byId("query").value = "";
    state.savedProfileInstruction = "";
    byId("profile-instruction").value = "";
    byId("profile-instruction-status").textContent = "";
    byId("profile-default-prompt").textContent = "";
    byId("profile-prompt-details").open = false;
    byId("profile-customization").open = false;
    byId("profile-use-default").hidden = true;
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
    state.detailRequest += 1;
    state.items = [];
    state.nextAfter = null;
    byId("item-list").replaceChildren();
    byId("detail").replaceChildren();
    byId("list-title").textContent = {resources: "Resources", "profile-data": "Profile Data", profiles: "User Profile", memories: "Memories"}[view];
    byId("new-resource").hidden = !["resources", "profile-data"].includes(view);
    byId("new-resource").textContent = view === "profile-data" ? "Add Profile Data" : "Add Resource";
    byId("search-form").hidden = view !== "memories";
    byId("profile-form").hidden = view !== "profiles";
    byId("refresh-profile").hidden = view !== "profiles";
    byId("detail-title").textContent = view === "profiles" ? "User Profile" : "Details";
    document.querySelector(".list-panel").hidden = view === "profiles";
    byId("workspace-grid").classList.toggle("profile-view", view === "profiles");
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

  async function loadProfile() {
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      status("Loading User Profile…");
      const profile = await json(route("profile"));
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return false;
      const message = showProfile(profile);
      status(message || "User Profile loaded.");
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
      current: "Up to date",
      dirty: "Out of date",
      stale: "Out of date",
      partial: "Some inputs unavailable",
      uncovered: "Up to date",
      missing: profile.profile_revision == null ? "Not created" : "Needs rebuild",
    };
    const line = element("div", undefined, "profile-freshness");
    line.setAttribute("aria-label", "User Profile status");
    const badge = element("span", labels[profile.freshness] || "Status unknown", "profile-state");
    badge.dataset.freshness = profile.freshness || "unknown";
    line.append(badge);
    const updated = profile.updated_at ? new Date(profile.updated_at) : null;
    if (updated && !Number.isNaN(updated.getTime())) {
      const elapsed = (Date.now() - updated.getTime()) / 1000;
      const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
      const [unit, seconds] = units.find(([, seconds]) => Math.abs(elapsed) >= seconds) || units.at(-1);
      const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(-Math.round(elapsed / seconds), unit);
      const time = element("time", `Updated ${relative}`);
      time.dateTime = profile.updated_at;
      time.title = updated.toLocaleString();
      line.append(element("span", "·"), time);
    } else if (profile.profile_revision != null) {
      line.append(element("span", "·"), element("span", "Updated time not recorded"));
    }
    return line;
  }

  function showProfile(profile) {
    const instruction = profile.instruction || "";
    const editor = byId("profile-instruction");
    if (editor.value === state.savedProfileInstruction) editor.value = instruction;
    state.savedProfileInstruction = instruction;
    byId("profile-default-prompt").textContent = profile.default_prompt;
    byId("profile-instruction-status").textContent = profile.instruction
      ? "Saved custom instruction active; it supplements the default prompt."
      : "Default prompt active; no saved custom instruction.";
    byId("profile-use-default").hidden = !instruction;
    byId("refresh-profile").textContent = profile.profile_revision == null ? "Create profile" : "Refresh profile";
    const detail = byId("detail");
    const failedInputs = profile.failed_inputs || 0;
    const blockedInputs = profile.blocked_inputs || 0;
    const uncoveredInputs = profile.uncovered_inputs || 0;
    detail.replaceChildren();
    detail.append(profileStatusLine(profile));
    if (profile.content) detail.append(renderProfileContent(profile.content));
    if (failedInputs > 0) detail.append(labelValue("Failed Insight inputs", failedInputs));
    if (blockedInputs > 0) detail.append(labelValue("Failed initial processing", blockedInputs));
    let message = "";
    if (!profile.content) {
      if ((profile.omitted || []).some(item => item.reason === "budget")) {
        message = "This User Profile exceeds the server's display limit.";
      } else if (profile.profile_revision == null) {
        message = "No generated User Profile yet. Use Create profile to create one from eligible inputs.";
      } else if (profile.freshness === "missing") {
        message = "This stored revision has no User Profile document. Use Refresh profile to build one from current sources.";
      } else {
        message = "The stored User Profile is unavailable for these inputs. Refresh it to rebuild from current sources.";
      }
      detail.append(element("p", message));
    }
    if (failedInputs > 0) detail.append(element("p", `${failedInputs} Insight input${failedInputs === 1 ? "" : "s"} failed. ${profile.content ? "This User Profile uses" : "A refreshed User Profile can use"} only ready information.`));
    if (blockedInputs > 0) detail.append(element("p", `${blockedInputs} memory input${blockedInputs === 1 ? "" : "s"} could not be prepared. Check the input operation before refreshing the User Profile.`));
    if (uncoveredInputs > 0) {
      const generationDetails = element("details");
      generationDetails.append(element("summary", "Generation details"));
      generationDetails.append(labelValue("Insights outside this summary", uncoveredInputs));
      detail.append(generationDetails);
    }
    const references = profile.references || [];
    if (references.length) {
      const referenceDetails = element("details", undefined, "profile-references");
      referenceDetails.append(element("summary", `Reference details (${references.length})`));
      for (const [index, reference] of references.entries()) {
        const row = element("div", undefined, "citation");
        if (reference.kind === "profile_data") {
          row.append(element("p", `[${index + 1}] Provided profile details`));
          row.append(labelValue("Profile Data", reference.profile_data_id));
          row.append(button("Inspect Profile Data", () => openProfileData(reference.profile_data_id)));
        } else {
          const passages = reference.passages || [reference];
          const dates = [...new Set(passages.filter(passage => passage.event_time).map(passage => passage.event_time.slice(0, 10)))];
          row.append(element("p", `[${index + 1}] Conversation${dates.length ? ` on ${dates.join("; ")}` : ""}`));
          for (const passage of passages) {
            row.append(labelValue("Conversation", passage.source_id));
            row.append(labelValue("Passage", passage.passage_id));
          }
        }
        referenceDetails.append(row);
      }
      detail.append(referenceDetails);
    }
    return message;
  }

  async function openProfileData(resourceId) {
    setView("profile-data");
    const generation = state.generation;
    try {
      const resource = await json(route(`profile-data/${encodeURIComponent(resourceId)}`));
      if (current(generation) && state.view === "profile-data") showResource(resource);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function refreshProfile(body = {}) {
    const generation = state.generation;
    const requestId = ++state.profileRequest;
    try {
      status("Checking User Profile inputs…");
      const outcome = await json(route("profile/refresh"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!current(generation) || state.view !== "profiles" || requestId !== state.profileRequest) return;
      if (body.reset_instruction) byId("profile-instruction").value = "";
      if (outcome.status === "unchanged") {
        if (await loadProfile()) status("Nothing new to create or refresh. Stored User Profile reused with zero generation calls.");
      } else {
        showReceipt(outcome.operation_id, outcome);
        byId("detail").append(button("View User Profile", loadProfile));
        status(`User Profile creation/refresh pending; operation ${outcome.operation_id}.`);
      }
    } catch (error) {
      if (current(generation) && state.view === "profiles" && requestId === state.profileRequest) {
        if (error.message === "ProfileInputsNotReady") {
          status("User Profile inputs are not ready. Check the input operation, then try again.", true);
        } else {
          showError(error);
        }
      }
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
      const id = state.view === "resources" ? item.resource_id : state.view === "profile-data" ? item.profile_data_id
        : item.kind === "insight" ? item.insight_id : item.memory_id;
      const kind = state.view === "resources" ? "Resource" : state.view === "profile-data" ? "Profile Data"
        : item.kind === "insight" && item.subtype ? `insight · ${item.subtype}` : item.kind;
      const caption = ["resources", "profile-data"].includes(state.view) ? `${item.media_type} · version ${item.active_version}` : item.text;
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
    const requestId = ++state.detailRequest;
    try {
      if (["resources", "profile-data"].includes(state.view)) {
        const view = state.view;
        const detail = await json(route(itemPath(item)));
        if (current(generation) && state.view === view && requestId === state.detailRequest) showResource(detail);
      } else {
        await showMemory(item, generation, requestId);
      }
    } catch (error) {
      if (!current(generation)) return;
      showError(error);
      byId("detail").replaceChildren(element("p", "This item is no longer available under current policy."));
    }
  }

  async function showMemory(item, generation, requestId) {
    const detail = byId("detail");
    const citations = item.citations || [];
    const isInsight = item.kind === "insight";
    const targetId = isInsight ? item.insight_id : item.memory_id;
    const revision = item.insight_revision ?? item.memory_revision ?? item.revision;
    const sourceMessages = element("div");
    sourceMessages.append(element("h4", "Source messages"));
    if (citations.length) sourceMessages.append(element("p", "Loading source messages…"));
    else sourceMessages.append(element("p", "No source messages available."));
    detail.replaceChildren(
      element("span", item.kind === "insight" ? "Insight" : "L1 Memory", "badge"),
      element("h3", targetId),
      element("h4", item.kind === "insight" ? "Insight" : "Extracted fact"),
      element("p", item.text, "source-text"),
      sourceMessages,
      labelValue("Origin", item.origin),
      labelValue("Canonical ID", targetId),
      labelValue("Revision", revision),
    );
    if (isInsight && item.subtype) detail.append(labelValue("Subtype", item.subtype));
    detail.append(element("h4", "Supporting memories"));
    if (item.supports?.length) {
      for (const support of item.supports) {
        detail.append(labelValue("Memory", support.memory_id));
        if (support.support_revision !== undefined) detail.append(labelValue("Support revision", support.support_revision));
      }
    } else {
      detail.append(element("p", "No supporting memories recorded."));
    }
    const affected = item.affected_insight_ids || [];
    detail.append(element("h4", "Affected dependent Insights"));
    if (affected.length) for (const insightId of affected) detail.append(element("p", insightId));
    else detail.append(element("p", "No dependent Insights are affected."));
    detail.append(element("div", undefined, "actions"));
    detail.lastElementChild.append(
      button(isInsight ? "Remove Insight" : "Remove Memory Unit", () => removeMemoryUnit(item)),
    );
    detail.append(element("h4", "Source provenance"));
    for (const citation of citations) {
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
    if (!citations.length) return;
    const sourceIds = [...new Set(citations.map((citation) => citation.source_id).filter(Boolean))];
    const sources = await Promise.all(sourceIds.map(async (sourceId) => {
      try { return await json(route(`evidence/${encodeURIComponent(sourceId)}`)); }
      catch (_) { return null; }
    }));
    if (!current(generation) || state.view !== "memories" || requestId !== state.detailRequest) return;
    sourceMessages.replaceChildren(element("h4", "Source messages"));
    for (const [index, source] of sources.entries()) {
      const group = element("div", undefined, "citation");
      group.append(element("h4", sourceIds[index]));
      if (!source) group.append(element("p", "Source messages unavailable."));
      else if (!source.original?.messages?.length) group.append(element("p", "No source messages available."));
      else for (const message of source.original.messages) {
        group.append(labelValue("Role", message.role));
        group.append(element("pre", typeof message.content === "string" ? message.content : JSON.stringify(message.content), "source-text"));
      }
      sourceMessages.append(group);
    }
    if (!sources.length) sourceMessages.append(element("p", "No source messages available."));
  }

  async function removeMemoryUnit(item) {
    const isInsight = item.kind === "insight";
    const targetId = isInsight ? item.insight_id : item.memory_id;
    const target = isInsight ? `Insight ${targetId}` : `Memory Unit ${targetId}`;
    if (!targetId) {
      status("This item has no canonical removal ID.", true);
      return;
    }
    if (!window.confirm(`Remove ${target}? The selected ${isInsight ? "Insight" : "Memory Unit"} will be excluded without deleting its source.`)) return;
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
          status(`${target} excluded; list refresh failed: ${error.message}`, true);
        }
        return;
      }
      if (current(removalGeneration)) {
        showReceipt(receipt.operation_id, receipt);
        status(`${target} excluded. Cleanup is ${receipt.cleanup_status}.`);
      }
    } catch (error) { if (current(generation)) showError(error); }
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

  const itemId = (item) => item.profile_data_id ?? item.resource_id;
  const itemCollection = (item) => item.profile_data_id !== undefined ? "profile-data" : "resources";
  const itemPath = (item) => `${itemCollection(item)}/${encodeURIComponent(itemId(item))}`;

  function showResource(item) {
    const detail = byId("detail");
    const isProfile = item.profile_data_id !== undefined;
    detail.replaceChildren(
      element("span", isProfile ? "Profile Data" : "Resource", "badge"),
      element("h3", itemId(item)),
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
    actions.append(button(isProfile ? "Delete Profile Data" : "Delete Resource", () => deleteSource(itemId(item), itemCollection(item))));
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
      const original = item.profile_data_id !== undefined
        ? await json(route(`${itemPath(item)}/original`))
        : null;
      if (current(generation) && state.view === itemCollection(item)) showResourceForm(item, original);
    } catch (error) { if (current(generation)) showError(error); }
  }

  async function inspectOriginal(item) {
    const generation = state.generation;
    try {
      if (!item.media_type.startsWith("text/") && item.media_type !== "application/json") {
        status("This original is binary. Use Download original to inspect it in a local viewer.");
        return;
      }
      const response = await request(route(`${itemPath(item)}/original`));
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
      const response = await request(route(`${itemPath(item)}/original`));
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
    const isProfile = state.view === "profile-data";
    const id = element("input"); id.required = true; id.value = existing ? itemId(existing) : ""; id.readOnly = !!existing;
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
      evidenceInputs.hidden = isProfile;
      profileInputs.hidden = !isProfile;
      textField.hidden = method.value !== "text";
      urlField.hidden = method.value !== "url";
      mediaField.hidden = method.value !== "url";
      fileField.hidden = method.value !== "file";
      basicInputs.hidden = subtype.value !== "basic-profile/v1";
      preferenceInputs.hidden = subtype.value !== "preferences/v1";
    };
    method.addEventListener("change", update);
    subtype.addEventListener("change", update);
    form.addEventListener("input", () => { state.draftKey = null; });
    form.append(
      element("h3", existing ? `Revise ${itemId(existing)} (expected version ${existing.active_version})` : isProfile ? "Add Profile Data" : "Add Resource"),
      field(isProfile ? "Profile Data ID" : "Resource ID", id), evidenceInputs, profileInputs,
    );
    update();
    const submit = element("button", existing ? "Create immutable revision" : isProfile ? "Create Profile Data" : "Create Resource");
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
      if (isProfile) {
        const content = {};
        if (subtype.value === "basic-profile/v1") {
          if (dateOfBirth.value) content.date_of_birth = dateOfBirth.value;
          if (sex.value.trim()) content.sex = sex.value.trim();
          if (householdSize.value) content.household_size = Number(householdSize.value);
          if (interests.value.trim()) content.interests = interests.value.split(",").map((part) => part.trim()).filter(Boolean);
          if (!Object.keys(content).length) { status("Enter at least one registered User Profile field.", true); return; }
        } else {
          if (!exampleLanguage.value.trim() || !domain.value.trim()) {
            status("Example language and domain are required.", true); return;
          }
          content.example_language = exampleLanguage.value.trim();
          content.domain = domain.value.trim();
        }
        const profile = {
          profile_data_id: id.value.trim(), subtype: subtype.value, content,
        };
        if (sourceRevision.value.trim()) profile.source_revision = sourceRevision.value.trim();
        if (asOf.value.trim()) profile.as_of = asOf.value.trim();
        headers["Content-Type"] = "application/json";
        body = JSON.stringify(profile);
        path = existing ? route(itemPath(existing)) : route("profile-data");
        verb = existing ? "PUT" : "POST";
      } else {
        const metadata = { input_type: "document", document_id: id.value.trim(), update_mode: "replace" };
        if (existing) metadata.user_id = state.user;
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
        byId("detail").append(button("View User Profile", loadProfile));
      }
      status(`Operation ${operation.status}.`);
    } catch (error) {
      if (current(generation) && state.view === view && (requestId === null || requestId === state.profileRequest)) showError(error);
    }
  }

  async function deleteSource(sourceId, collection) {
    const type = collection === "profile-data" ? "Profile Data" : collection ? "Resource" : "chat source";
    const target = `${type} ${sourceId}`;
    if (!window.confirm(`Delete ${target}? All versions and dependent passages, memories, Insights, and User Profile fields become unavailable immediately. Cleanup is tracked separately.`)) return;
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
        body: JSON.stringify({ user_id: state.user, query, recall_level: "fast", allow_partial: true }),
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
    const instruction = byId("profile-instruction").value.trim();
    if (!instruction) {
      status("Enter custom instructions before applying them.", true);
      return;
    }
    refreshProfile({ instruction });
  });
  byId("refresh-profile").addEventListener("click", () => refreshProfile());
  byId("profile-use-default").addEventListener("click", () => refreshProfile({ reset_instruction: true }));
  byId("export-button").addEventListener("click", createExport);
  for (const tab of document.querySelectorAll("[data-view]")) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
  }
})();
