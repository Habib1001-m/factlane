import {
  currentLocale,
  renderStaleBanner,
  renderStatusBanner,
  setDrawerOpen,
  setHeaderState,
  setLocale,
  setNavigationDisabled,
  setPage,
  wireShell,
} from "./shell.js";
import { stateRenderers } from "./views.js";

function consumeStartupConnectHandoff() {
  const url = new URL(window.location.href);
  const fragment = url.hash;
  if (!fragment.startsWith("#connect=")) return { locator: null, error: null };
  const encoded = fragment.slice("#connect=".length);
  const duplicate = encoded.includes("&connect=");
  let locator = null;
  let decodeFailed = false;
  try {
    locator = decodeURIComponent(encoded);
  } catch (_error) {
    decodeFailed = true;
  }
  let acceptedLocator = null;
  let error = null;
  if (duplicate) {
    error = "CONNECT_LOCATOR_DUPLICATE";
  } else if (decodeFailed) {
    error = "CONNECT_LOCATOR_MALFORMED";
  } else if (!locator) {
    error = "CONNECT_LOCATOR_EMPTY";
  } else if (locator.length > 512) {
    error = "CONNECT_LOCATOR_OVERSIZE";
  } else {
    acceptedLocator = locator;
  }
  url.search = "";
  url.hash = "#connect";
  history.replaceState(history.state, "", `${url.pathname}${url.search}${url.hash}`);
  return { locator: acceptedLocator, error };
}

const startupConnectHandoff = consumeStartupConnectHandoff();
const startupConnectLocator = startupConnectHandoff.locator;

const ROUTES = Object.freeze({
  preauth: "/auth/preauth", claim: "/auth/connect/claim", poll: "/auth/connect/poll", logout: "/auth/logout",
  overview: "/api/read/overview", search: "/api/read/search", inventory: "/api/read/inventory", detail: "/api/read/detail", conflicts: "/api/read/conflicts",
  createCandidate: "/api/write/create_candidate", batchReview: "/api/write/batch_review", revoke: "/api/write/revoke", reject: "/api/write/reject", reverify: "/api/write/reverify", replace: "/api/write/replace",
  manualRecheck: "/api/write/manual_recheck", resolve: "/api/write/resolve", readmit: "/api/write/readmit", purgePreview: "/api/write/purge/preview", purgeCommit: "/api/write/purge/commit", purgeRetry: "/api/write/purge/retry",
  sensitiveChallenge: "/auth/sensitive/challenge", sensitiveGrant: "/auth/sensitive/grant",
  housekeepingPreview: "/api/operations/housekeeping_preview", runHousekeeping: "/api/operations/run_housekeeping", agents: "/api/operations/agent_inventory", settingsSnapshot: "/api/operations/settings_snapshot",
  settingsPreview: "/api/operations/settings_preview", settingsApply: "/api/operations/settings_apply", supportPreview: "/api/operations/support_report_preview",
});
const dbRoute = (suffix) => "/api/database/" + suffix;

const LIFECYCLE_ROUTES = Object.freeze({ BATCH_REVIEW: ROUTES.batchReview, REVOKE: ROUTES.revoke, REJECT: ROUTES.reject, REVERIFY: ROUTES.reverify, REPLACE: ROUTES.replace, MANUAL_RECHECK: ROUTES.manualRecheck, RESOLVE: ROUTES.resolve, READMIT: ROUTES.readmit });

const STATUS_POLL_MS = 15_000, STATUS_TIMEOUT_MS = 3_000, CONNECT_POLL_MS = 2_000, SESSION_WARNING_MS = 120_000;
const IMPORT_MAX = 10 * 1024 * 1024, RESTORE_MAX = 1024 * 1024 * 1024, EXPORT_STATES = "CANDIDATE,VALIDATED_CURRENT,SUPERSEDED,STALE,QUARANTINED,HISTORICAL,REVOKED,REJECTED".split(","), main = document.querySelector("[data-main]"), defaultHousekeepingDraft = () => ({ scope: "PROJECT", projectId: "", worktreeId: "", workflowId: "", agentId: "", maxRecords: "100", requiredFreeBytes: "0" });
let dbPoll = null;

class ApiError extends Error {
  constructor(status, code, retryable = false, payload = {}) {
    super(code || `HTTP_${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || `HTTP_${status}`;
    this.retryable = Boolean(retryable);
    this.payload = payload;
  }
}

const state = {
  auth: {
    csrf: null,
    expiresAt: null,
    absoluteExpiresAt: null,
    idleWindowMs: null,
    warningShown: false,
  },
  connect: {
    preauthReady: false,
    locator: "",
    request: {},
    pollTimer: null,
    pollInFlight: false,
  },
  connection: {
    stale: true,
    lastReadAt: null,
    pollInFlight: false,
  },
  data: {
    overview: null,
    browse: null,
    detail: null,
    conflicts: null,
    agents: null,
    settings: null,
    supportPreview: null,
  },
  drafts: {
    candidate: null,
    review: null,
    lifecycle: null,
    settings: {},
    housekeeping: defaultHousekeepingDraft(),
  },
  browseFilters: {
    query: "",
    searchMode: "TEXT",
    scope: "PROJECT",
    projectId: "",
    worktreeId: "",
    workflowId: "",
    agentId: "",
    lifecycleState: "",
    sourceRef: "",
  },
  candidateOpen: false,
  purge: {
    operationId: null,
    purgePreview: null,
    authorizationPreview: null,
    previewDigest: null,
    typedConfirmation: null,
    typedInput: "",
    challengeId: null,
    verificationPhrase: "",
    grantSecret: null,
    recoveryRequired: false,
    status: "IDLE",
  },
  db: { artifacts: [], job: null, deletePreview: null, importFlow: null, restore: null },
  lastRetry: null,
};

if (startupConnectLocator) state.connect.locator = startupConnectLocator;

function requestId(prefix) {
  const uuid = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${uuid}`;
}

function newOperationId() {
  if (!globalThis.crypto?.randomUUID) throw new ApiError(500, "CRYPTO_UUID_UNAVAILABLE");
  return globalThis.crypto.randomUUID();
}

function requestedState() {
  const name = window.location.hash.slice(1);
  return Object.hasOwn(stateRenderers, name) ? name : "overview";
}

function navigate(name) {
  const next = Object.hasOwn(stateRenderers, name) ? name : "overview";
  if (window.location.hash === `#${next}`) {
    void loadRoute(next);
  } else {
    window.location.hash = next;
  }
}

const bind = (fn, ...head) => (...tail) => fn(...head, ...tail);

function clearLifecycleDraft() {
  state.drafts.review = null;
  state.drafts.lifecycle = null;
}

function resetPurgeState({ keepRecoveryIdentity = false } = {}) {
  const operationId = keepRecoveryIdentity ? state.purge.operationId : null;
  const purgePreview = keepRecoveryIdentity ? state.purge.purgePreview : null;
  const authorizationPreview = keepRecoveryIdentity ? state.purge.authorizationPreview : null;
  const previewDigest = keepRecoveryIdentity ? state.purge.previewDigest : null;
  const typedConfirmation = keepRecoveryIdentity ? state.purge.typedConfirmation : null;
  const typedInput = keepRecoveryIdentity ? state.purge.typedInput : "";
  state.purge = {
    operationId,
    purgePreview,
    authorizationPreview,
    previewDigest,
    typedConfirmation,
    typedInput,
    challengeId: null,
    verificationPhrase: "",
    grantSecret: null,
    recoveryRequired: keepRecoveryIdentity,
    status: keepRecoveryIdentity ? "RECOVERY_REQUIRED" : "IDLE",
  };
}

function clearSensitiveContext() {
  clearLifecycleDraft();
  resetPurgeState();
  if (state.db.restore) Object.assign(state.db.restore, { challengeId: null, verificationPhrase: "", grantSecret: null });
}

function clearAllDrafts() {
  clearSensitiveContext();
  state.drafts.candidate = null;
  state.drafts.settings = {};
  state.drafts.housekeeping = defaultHousekeepingDraft();
}

function expireSession(code = "SESSION_EXPIRED") {
  state.auth.csrf = null;
  state.auth.expiresAt = null;
  state.auth.absoluteExpiresAt = null;
  state.auth.idleWindowMs = null;
  state.auth.warningShown = false;
  clearSensitiveContext();
  stopConnectPolling();
  renderStatusBanner({
    tone: "warning",
    title: code === "SESSION_EXPIRED" ? "Session expired" : "Authentication required",
    message: "Reconnect through the approved browser flow. Pending lifecycle actions were cleared before recovery.",
  });
  navigate("connect");
}

function refreshSessionDeadline() {
  if (!state.auth.csrf || !Number.isFinite(state.auth.idleWindowMs) || !Number.isFinite(state.auth.absoluteExpiresAt)) return;
  const absoluteMs = state.auth.absoluteExpiresAt * 1000;
  state.auth.expiresAt = Math.min(Date.now() + state.auth.idleWindowMs, absoluteMs) / 1000;
  state.auth.warningShown = false;
}

function sessionDeadlineMs() {
  const idle = Number(state.auth.expiresAt) * 1000;
  const absolute = Number(state.auth.absoluteExpiresAt) * 1000;
  if (!Number.isFinite(idle) || !Number.isFinite(absolute)) return null;
  return Math.min(idle, absolute);
}

function checkSessionExpiry() {
  if (!state.auth.csrf) return;
  const deadline = sessionDeadlineMs();
  if (deadline == null) return;
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    expireSession("SESSION_EXPIRED");
    return;
  }
  if (remaining <= SESSION_WARNING_MS && !state.auth.warningShown) {
    state.auth.warningShown = true;
    renderStatusBanner({
      tone: "warning",
      title: "Session expires soon",
      message: "This Owner session will expire within two minutes. Sensitive lifecycle and purge authorization context will be cleared on expiry.",
    });
  }
}

function showError(error, retry) {
  const api = error instanceof ApiError ? error : null;
  const retryable = Boolean(api?.retryable || !api || api.status >= 500);
  state.lastRetry = retryable && typeof retry === "function" ? retry : null;
  renderStatusBanner({
    tone: "danger",
    title: api?.code || "Request failed",
    message: retryable ? "The request did not complete. The current page data and recoverable draft remain unchanged." : "The server rejected the request. Review the displayed state before trying another action.",
    actionLabel: state.lastRetry ? "Retry" : "",
    onAction: state.lastRetry || undefined,
  });
}

async function readJson(response) {
  try {
    const payload = await response.json();
    return payload && typeof payload === "object" ? payload : {};
  } catch (_error) {
    return {};
  }
}

async function post(path, body = {}, { authenticated = true, activity = true, method = "POST", contentType = "application/json", parse = true } = {}) {
  const headers = {};
  if (contentType) headers["Content-Type"] = contentType;
  if (authenticated) {
    if (!state.auth.csrf) throw new ApiError(401, "SESSION_EXPIRED");
    headers["X-FactLane-CSRF"] = state.auth.csrf;
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers,
      ...(method === "POST" ? { body: body instanceof Blob ? body : JSON.stringify(body) } : {}),
    });
  } catch (error) {
    markDisconnected();
    throw error;
  }
  const payload = parse || !response.ok ? await readJson(response) : {};
  if (response.status === 401 && authenticated) {
    expireSession(payload.code || "SESSION_EXPIRED");
    throw new ApiError(response.status, payload.code || "SESSION_EXPIRED", payload.retryable, payload);
  }
  if (!response.ok) throw new ApiError(response.status, payload.code, payload.retryable, payload);
  state.connection.lastReadAt = Date.now();
  if (authenticated && activity) refreshSessionDeadline();
  return { status: response.status, payload, response };
}

async function lifecycleAction(action, body) {
  const route = LIFECYCLE_ROUTES[action];
  if (!route) throw new ApiError(400, "LIFECYCLE_ACTION_UNSUPPORTED");
  return post(route, body);
}

function markDisconnected() {
  state.connection.stale = true;
  renderStaleBanner({
    stale: true,
    title: "Disconnected",
    message: "Displayed data may be stale. Writes remain blocked until a foreground request revalidates authorization and current record state.",
  });
  updateHeader();
}

function markConnected() {
  state.connection.stale = false;
  renderStaleBanner({ stale: false });
  updateHeader();
}

async function pollHealth() {
  if (document.visibilityState !== "visible" || state.connection.pollInFlight) return;
  state.connection.pollInFlight = true;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);
  try {
    const response = await fetch("/health", {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new ApiError(response.status, "HEALTH_UNAVAILABLE", true);
    markConnected();
  } catch (_error) {
    markDisconnected();
  } finally {
    window.clearTimeout(timer);
    state.connection.pollInFlight = false;
  }
}

function updateHeader() {
  const age = state.connection.lastReadAt == null
    ? "Not refreshed"
    : `Updated ${Math.max(0, Math.floor((Date.now() - state.connection.lastReadAt) / 1000))}s ago`;
  setHeaderState({
    database: "Database identity unavailable",
    connection: state.auth.csrf ? (state.connection.stale ? "Stale" : "Connected") : "Authentication required",
    refreshAge: age,
    authenticated: Boolean(state.auth.csrf),
  });
}

function scopePayload(values = {}) {
  return {
    scope: values.scope || "PROJECT",
    project_id: values.projectId || null,
    worktree_id: values.worktreeId || null,
    workflow_id: values.workflowId || null,
    agent_id: values.agentId || null,
  };
}

function normalizedBrowseFilters(values = {}) {
  return {
    query: String(values.query || ""),
    searchMode: values.searchMode === "SEMANTIC" ? "SEMANTIC" : "TEXT",
    scope: String(values.scope || "PROJECT"),
    projectId: String(values.projectId || ""),
    worktreeId: String(values.worktreeId || ""),
    workflowId: String(values.workflowId || ""),
    agentId: String(values.agentId || ""),
    lifecycleState: String(values.lifecycleState || ""),
    sourceRef: String(values.sourceRef || ""),
  };
}

function publicItem(item) {
  return {
    ...item,
    id: item.memory_id || item.id,
    state: item.lifecycle_state || item.state,
    content: item.fact || item.content,
    source: item.source_provenance?.source_ref || item.source,
  };
}

function overviewProps() {
  const data = state.data.overview || {};
  const totals = data.totals || {};
  const coverage = (data.project_breakdown || []).map((item) => ({
    project: item.project_id || item.project || item.scope?.project_id || item.scope?.scope || "Unscoped",
    memories: item.logical_memories,
    candidates: item.candidates,
    lastObserved: data.snapshot_at,
  }));
  return {
    totals: {
      logicalMemories: totals.logical_memories,
      revisions: totals.revisions,
      candidates: totals.candidates,
      conflicts: totals.conflicts,
    },
    coverage,
    activity: [],
    attention: [],
    activityUnavailable: true,
    attentionUnavailable: true,
    onReviewCandidates: () => {
      state.browseFilters.lifecycleState = "CANDIDATE";
      navigate("browse");
    },
    onRetryOverview: () => void loadOverview(),
  };
}

function browseSummary(data) {
  if (!data) return "";
  if (data.search?.degraded) return `Semantic search unavailable (${data.search.degradation}); showing text results.`;
  const filtered = data.counts?.filtered_logical;
  if (filtered?.value != null) return `${filtered.value} matching logical memories.`;
  if (Array.isArray(data.items)) return `${data.items.length} records on this page.`;
  return "";
}

function sourceDraft(item = {}) {
  const provenance = item.source_provenance || {};
  return {
    sourceClass: provenance.source_class || "OWNER",
    sourceRef: provenance.source_ref || "",
    sourceHash: provenance.source_hash || "",
    reviewRef: provenance.review_ref || "",
    extractionMethod: provenance.extraction_method || "manual",
    sourceTimestamp: item.source_timestamp || "",
  };
}

function defaultCandidateDraft() {
  return {
    idempotencyKey: requestId("candidate"),
    fact: "",
    memoryType: "PROJECT_LEARNED_FACT",
    scope: state.browseFilters.scope || "PROJECT",
    projectId: state.browseFilters.projectId || "",
    worktreeId: state.browseFilters.worktreeId || "",
    workflowId: state.browseFilters.workflowId || "",
    agentId: state.browseFilters.agentId || "",
    ...sourceDraft(),
    subject: "",
  };
}

function verificationFromDraft(draft) {
  return {
    source_provenance: {
      source_class: draft.sourceClass,
      source_ref: draft.sourceRef,
      source_hash: draft.sourceHash,
      review_ref: draft.reviewRef,
      extraction_method: draft.extractionMethod,
    },
    freshness_policy: draft.freshnessPolicy || { kind: "manual" },
    source_timestamp: draft.sourceTimestamp,
    verified_by: "OWNER",
  };
}

async function ensurePreauth() {
  if (state.connect.preauthReady) return;
  await post(ROUTES.preauth, {}, { authenticated: false });
  state.connect.preauthReady = true;
}

async function claimConnect(locator) {
  const value = String(locator || "");
  if (!value || value.length > 512) {
    showError(new ApiError(400, "LOCATOR_REQUIRED"));
    return;
  }
  state.connect.locator = value;
  try {
    await ensurePreauth();
    const { payload } = await post(ROUTES.claim, { locator: value }, { authenticated: false });
    state.connect.request = {
      id: payload.locator || value,
      phrase: payload.verification_phrase ? String(payload.verification_phrase).split(/\s+/) : [],
      status: payload.state || "CLAIMED",
    };
    renderCurrent();
    startConnectPolling();
  } catch (error) {
    state.connect.preauthReady = false;
    showError(error, () => void claimConnect(value));
  }
}

async function pollConnect() {
  if (!state.connect.locator || state.connect.pollInFlight || document.visibilityState !== "visible") return;
  state.connect.pollInFlight = true;
  try {
    const { status, payload } = await post(ROUTES.poll, { locator: state.connect.locator }, { authenticated: false });
    if (status === 202) {
      state.connect.request.status = "PENDING";
      renderCurrent();
      return;
    }
    state.auth.csrf = payload.csrf;
    state.auth.expiresAt = payload.expires_at;
    state.auth.absoluteExpiresAt = payload.absolute_expires_at;
    state.auth.idleWindowMs = Math.max(0, Number(payload.expires_at) * 1000 - Date.now());
    state.auth.warningShown = false;
    state.connect.preauthReady = false;
    state.connect.request = {};
    state.connect.locator = "";
    stopConnectPolling();
    markConnected();
    renderStatusBanner({ tone: "success", title: "Connected", message: "Owner session established for this page." });
    navigate("overview");
  } catch (error) {
    if (error instanceof ApiError && [401, 403, 409].includes(error.status)) {
      stopConnectPolling();
      state.connect.preauthReady = false;
      state.connect.request = { ...state.connect.request, status: error.status === 401 ? "EXPIRED" : "DENIED" };
      showError(error, () => void claimConnect(state.connect.locator));
    } else {
      showError(error, () => void pollConnect());
    }
  } finally {
    state.connect.pollInFlight = false;
  }
}

function startConnectPolling() {
  stopConnectPolling();
  state.connect.pollTimer = window.setInterval(() => void pollConnect(), CONNECT_POLL_MS);
  void pollConnect();
}

function stopConnectPolling() {
  if (state.connect.pollTimer != null) window.clearInterval(state.connect.pollTimer);
  state.connect.pollTimer = null;
  state.connect.pollInFlight = false;
}

async function logout() {
  try {
    if (state.auth.csrf) await post(ROUTES.logout, {});
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) showError(error);
  } finally {
    state.auth.csrf = null;
    clearAllDrafts();
    navigate("connect");
  }
}

async function loadOverview() {
  try {
    const { payload } = await post(ROUTES.overview, { limit: 100, request_id: requestId("overview") });
    state.data.overview = payload;
    markConnected();
    renderStatusBanner();
    renderCurrent();
  } catch (error) {
    if (error instanceof ApiError && error.status !== 401) {
      const unavailable = { value: null, completeness: "UNKNOWN", reason: error.code };
      state.data.overview = {
        totals: {
          logical_memories: unavailable,
          revisions: unavailable,
          candidates: unavailable,
          conflicts: unavailable,
        },
        project_breakdown: [],
      };
    }
    showError(error, () => void loadOverview());
    renderCurrent();
  }
}

async function runBrowse(values = state.browseFilters) {
  const filters = normalizedBrowseFilters(values);
  state.browseFilters = filters;
  const scope = scopePayload(filters);
  const common = {
    ...scope,
    limit: 50,
    request_id: requestId("browse"),
  };
  if (filters.lifecycleState) common.lifecycle_state = filters.lifecycleState;
  try {
    let payload;
    if (filters.query.trim()) {
      const body = {
        ...common,
        query: filters.query.trim(),
        search_mode: filters.searchMode,
      };
      if (filters.sourceRef) body.source_ref = filters.sourceRef;
      ({ payload } = await post(ROUTES.search, body));
    } else {
      ({ payload } = await post(ROUTES.inventory, common));
    }
    state.data.browse = payload;
    markConnected();
    renderStatusBanner();
    renderCurrent();
  } catch (error) {
    showError(error, () => void runBrowse(filters));
    renderCurrent();
  }
}

async function createCandidate(values) {
  const previous = state.drafts.candidate || defaultCandidateDraft();
  state.drafts.candidate = { ...previous, ...values, idempotencyKey: previous.idempotencyKey };
  const draft = state.drafts.candidate;
  const body = {
    ...scopePayload(draft),
    fact: draft.fact,
    memory_type: draft.memoryType,
    source_provenance: {
      source_class: draft.sourceClass,
      source_ref: draft.sourceRef,
      source_hash: draft.sourceHash,
      review_ref: draft.reviewRef,
      extraction_method: draft.extractionMethod,
    },
    freshness_policy: { kind: "manual" },
    idempotency_key: draft.idempotencyKey,
    source_timestamp: draft.sourceTimestamp || null,
    subject: draft.subject || null,
    request_id: requestId("candidate-create"),
  };
  try {
    await post(ROUTES.createCandidate, body);
    state.drafts.candidate = null;
    state.candidateOpen = false;
    renderStatusBanner({ tone: "success", title: "Candidate created", message: "The Candidate was stored and still requires review." });
    await runBrowse(state.browseFilters);
  } catch (error) {
    showError(error, () => void createCandidate(state.drafts.candidate));
    renderCurrent();
  }
}

async function loadDetail(item, { navigateAfter = true } = {}) {
  const source = item || state.data.detail?.latest;
  if (!source) return null;
  const scope = source.scope && typeof source.scope === "object"
    ? source.scope
    : (typeof source.scope === "string"
      ? {
        scope: source.scope,
        project_id: source.project_id ?? null,
        worktree_id: source.worktree_id ?? null,
        workflow_id: source.workflow_id ?? null,
        agent_id: source.agent_id ?? null,
      }
      : (state.data.detail?.scope || scopePayload(state.browseFilters)));
  const body = {
    ...scope,
    memory_id: source.memory_id || source.id,
    limit: 100,
    request_id: requestId("detail"),
  };
  try {
    const { payload } = await post(ROUTES.detail, body);
    state.data.detail = payload;
    markConnected();
    if (navigateAfter) navigate("detail");
    else renderCurrent();
    return payload;
  } catch (error) {
    showError(error, () => void loadDetail(source, { navigateAfter }));
    return null;
  }
}

function detailLifecycleDraft(action) {
  const detail = state.data.detail;
  const latest = detail?.latest;
  if (!latest || !detail?.group_cas?.observed_group_token) return null;
  return {
    action,
    idempotencyKey: requestId(`lifecycle-${action.toLowerCase()}`),
    expectedRecordId: latest.record_id,
    expectedRevision: latest.revision,
    observedGroupToken: detail.group_cas.observed_group_token,
    ...sourceDraft(latest),
    freshnessPolicy: latest.freshness_policy || { kind: "manual" },
    fact: latest.fact || "",
    reason: action === "REVOKE" ? "OUTDATED" : "",
    claim: "INDETERMINATE",
    recheckRef: "",
    observedAt: new Date().toISOString(),
    observedSourceHash: "",
    evidenceRef: "",
    method: "OWNER_REVIEWED_SOURCE",
  };
}

function openDetailLifecycleAction(action) {
  state.drafts.lifecycle = detailLifecycleDraft(action);
  renderCurrent();
}

async function mutateDetailLifecycle(values) {
  if (!state.drafts.lifecycle || !state.data.detail?.latest) return;
  state.drafts.lifecycle = { ...state.drafts.lifecycle, ...values };
  const draft = state.drafts.lifecycle;
  const source = state.data.detail.latest;
  const refreshed = await loadDetail(source, { navigateAfter: false });
  if (!refreshed?.latest) return;
  const freshToken = refreshed.group_cas?.observed_group_token;
  if (
    refreshed.latest.record_id !== draft.expectedRecordId ||
    refreshed.latest.revision !== draft.expectedRevision ||
    freshToken !== draft.observedGroupToken
  ) {
    clearLifecycleDraft();
    showError(new ApiError(409, "VERSION_CONFLICT"));
    renderCurrent();
    return;
  }

  const common = {
    ...refreshed.scope,
    memory_id: refreshed.latest.memory_id,
    observed_group_token: freshToken,
    idempotency_key: draft.idempotencyKey,
    request_id: requestId(`lifecycle-${draft.action.toLowerCase()}-submit`),
  };
  let body;
  if (draft.action === "READMIT") {
    body = {
      ...common,
      revoked_record_id: refreshed.latest.record_id,
      expected_revoked_revision: refreshed.latest.revision,
      verification: verificationFromDraft(draft),
    };
  } else {
    body = {
      ...common,
      expected_record_id: refreshed.latest.record_id,
      expected_revision: refreshed.latest.revision,
    };
    if (draft.action === "REVOKE") body.reason_code = draft.reason;
    if (draft.action === "REVERIFY") body.verification = verificationFromDraft(draft);
    if (draft.action === "REPLACE") {
      body.replacement = {
        ...verificationFromDraft(draft),
        fact: draft.fact,
      };
    }
    if (draft.action === "MANUAL_RECHECK") {
      body.evidence = {
        origin: "OWNER_SUPPLIED",
        claim: draft.claim,
        recheck_ref: draft.recheckRef,
        observed_at: draft.observedAt,
        observed_source_hash: draft.claim === "OBSERVED_HASH" ? draft.observedSourceHash : null,
        evidence_ref: draft.evidenceRef || null,
        method: draft.method,
      };
    }
  }

  try {
    await lifecycleAction(draft.action, body);
    clearLifecycleDraft();
    renderStatusBanner({ tone: "success", title: `${draft.action} complete`, message: "The server confirmed the exact lifecycle mutation." });
    await loadDetail(refreshed.latest, { navigateAfter: false });
  } catch (error) {
    showError(error, () => void mutateDetailLifecycle(state.drafts.lifecycle || values));
    renderCurrent();
  }
}

function purgeTargetFromDetail(detail) {
  const latest = detail?.latest;
  const scope = detail?.scope;
  if (!latest || !scope) return null;
  return {
    scope: scope.scope,
    project_id: scope.project_id ?? null,
    worktree_id: scope.worktree_id ?? null,
    workflow_id: scope.workflow_id ?? null,
    agent_id: scope.agent_id ?? null,
    memory_id: latest.memory_id,
    record_id: latest.record_id,
    revision: latest.revision,
  };
}

async function previewPurge() {
  const target = purgeTargetFromDetail(state.data.detail);
  if (!target) return;
  let operationId;
  try {
    operationId = newOperationId();
  } catch (error) {
    showError(error);
    return;
  }
  resetPurgeState();
  state.purge.operationId = operationId;
  state.purge.status = "PREVIEWING";
  try {
    const { payload } = await post(ROUTES.purgePreview, {
      operation_id: operationId,
      targets: [target],
    });
    state.purge.purgePreview = payload.preview;
    state.purge.authorizationPreview = payload.authorization_preview;
    state.purge.previewDigest = payload.preview?.preview_digest || null;
    state.purge.typedConfirmation = payload.preview?.typed_confirmation || null;
    state.purge.typedInput = "";
    state.purge.status = "PREVIEW_READY";
    navigate("recovery");
  } catch (error) {
    resetPurgeState();
    showError(error, () => void previewPurge());
  }
}

async function beginSensitive(target, retry) {
  target.challengeId = null;
  target.verificationPhrase = "";
  target.grantSecret = null;
  try {
    const { payload } = await post(ROUTES.sensitiveChallenge, { preview: target.authorizationPreview });
    target.challengeId = payload.challenge_id;
    target.verificationPhrase = payload.verification_phrase || "";
    target.status = "AWAITING_NATIVE_APPROVAL";
    renderCurrent();
  } catch (error) {
    showError(error, retry);
  }
}

async function mintSensitive(target, retry) {
  if (!target.challengeId) {
    showError(new ApiError(400, "SENSITIVE_CHALLENGE_REQUIRED"));
    return;
  }
  try {
    const { payload } = await post(ROUTES.sensitiveGrant, { challenge_id: target.challengeId });
    target.grantSecret = payload.grant;
    target.status = "GRANT_READY";
    renderCurrent();
  } catch (error) {
    target.grantSecret = null;
    showError(error, retry);
  }
}

function beginPurgeChallenge() {
  if (!state.purge.authorizationPreview || state.purge.typedInput !== state.purge.typedConfirmation) return showError(new ApiError(400, "PURGE_CONFIRMATION_MISMATCH"));
  return beginSensitive(state.purge, () => void beginPurgeChallenge());
}

function mintPurgeGrant() {
  return mintSensitive(state.purge, () => void mintPurgeGrant());
}

async function executePurge(mode = "commit") {
  if (state.purge.typedInput !== state.purge.typedConfirmation) {
    showError(new ApiError(400, "PURGE_CONFIRMATION_MISMATCH"));
    return;
  }
  if (!state.purge.grantSecret) {
    showError(new ApiError(403, "PURGE_GRANT_REQUIRED"));
    return;
  }
  const grantSecret = state.purge.grantSecret;
  state.purge.grantSecret = null;
  state.purge.challengeId = null;
  state.purge.verificationPhrase = "";
  state.purge.status = "EXECUTING";
  const route = mode === "retry" ? ROUTES.purgeRetry : ROUTES.purgeCommit;
  const body = {
    operation_id: state.purge.operationId,
    preview_digest: state.purge.previewDigest,
    typed_confirmation: state.purge.typedInput,
    grant_secret: grantSecret,
  };
  try {
    const { payload } = await post(route, body);
    state.data.detail = null;
    resetPurgeState();
    renderStatusBanner({ tone: "success", title: "Permanent purge verified", message: payload.status || "The exact purge completed." });
    navigate("overview");
  } catch (error) {
    const recoveryRequired = error instanceof ApiError && error.payload?.recovery_required === true;
    if (recoveryRequired) {
      resetPurgeState({ keepRecoveryIdentity: true });
      renderStatusBanner({
        tone: "danger",
        title: "Purge recovery required",
        message: "The same purge operation identity is retained. Request a fresh native-approved sensitive grant before retrying; the consumed grant was discarded.",
      });
      navigate("recovery");
      return;
    }
    if (error instanceof ApiError && ["PURGE_PREVIEW_STALE", "PURGE_PREVIEW_MISMATCH", "PURGE_RECOVERY_STATE_MISMATCH"].includes(error.code)) {
      resetPurgeState();
      showError(error);
      const current = state.data.detail?.latest;
      if (current) await loadDetail(current, { navigateAfter: false });
      navigate("detail");
      return;
    }
    state.purge.status = "AUTHORIZATION_REQUIRED";
    showError(error, () => void beginPurgeChallenge());
    renderCurrent();
  }
}

function openReview() {
  const detail = state.data.detail;
  if (!detail?.latest) return;
  const seed = detail.latest;
  state.drafts.review = {
    idempotencyKey: requestId("review"),
    ...sourceDraft(seed),
    reason: "",
    expectedRecordId: seed.record_id,
    expectedRevision: seed.revision,
    observedGroupToken: detail.group_cas?.observed_group_token,
  };
  navigate("review");
}

async function reviewCandidate(action, values) {
  if (!state.drafts.review || !state.data.detail?.latest) return;
  state.drafts.review = { ...state.drafts.review, ...values };
  const before = state.drafts.review;
  const refreshed = await loadDetail(state.data.detail.latest, { navigateAfter: false });
  if (!refreshed?.latest) return;
  if (
    refreshed.latest.record_id !== before.expectedRecordId ||
    refreshed.latest.revision !== before.expectedRevision ||
    refreshed.group_cas?.observed_group_token !== before.observedGroupToken
  ) {
    clearLifecycleDraft();
    showError(new ApiError(409, "VERSION_CONFLICT"));
    navigate("detail");
    return;
  }
  const item = {
    action,
    memory_id: refreshed.latest.memory_id,
    expected_record_id: refreshed.latest.record_id,
    expected_revision: refreshed.latest.revision,
    observed_group_token: refreshed.group_cas.observed_group_token,
    idempotency_key: before.idempotencyKey,
  };
  if (action === "APPROVE") item.verification = verificationFromDraft(before);
  else item.reason_code = before.reason;
  try {
    await lifecycleAction("BATCH_REVIEW", {
      ...refreshed.scope,
      items: [item],
      request_id: requestId("review-submit"),
    });
    clearLifecycleDraft();
    renderStatusBanner({ tone: "success", title: action === "APPROVE" ? "Candidate approved" : "Candidate rejected", message: "The server confirmed the lifecycle result." });
    await loadDetail(refreshed.latest, { navigateAfter: false });
    navigate("detail");
  } catch (error) {
    showError(error, () => void reviewCandidate(action, state.drafts.review));
    renderCurrent();
  }
}

async function loadData(name, route, body) {
  try {
    state.data[name] = (await post(route, body)).payload;
    markConnected();
    renderCurrent();
  } catch (error) {
    showError(error, () => void loadData(name, route, body));
    renderCurrent();
  }
}

function loadConflicts() {
  return loadData("conflicts", ROUTES.conflicts, { ...scopePayload(state.browseFilters), limit: 100, request_id: requestId("conflicts") });
}

async function resolveConflict(selection) {
  const first = state.data.conflicts?.items?.[0];
  const group = first?.group_cas;
  if (!group) return;
  const winner = group.members?.find((member) => member.record_id === selection.winner);
  if (!winner) {
    showError(new ApiError(400, "WINNER_REQUIRED"));
    return;
  }
  if (winner.lifecycle_state === "CANDIDATE") {
    showError(new ApiError(400, "WINNER_PROOF_REQUIRED"));
    return;
  }
  try {
    await lifecycleAction("RESOLVE", {
      ...group.scope,
      contradiction_key: group.contradiction_key,
      observed_group_token: group.observed_group_token,
      winner_memory_id: winner.memory_id,
      winner_record_id: winner.record_id,
      winner_expected_revision: winner.revision,
      idempotency_key: requestId("resolve"),
      reason_code: selection.reason,
      request_id: requestId("resolve-request"),
    });
    renderStatusBanner({ tone: "success", title: "Conflict resolved", message: "The server confirmed the exact group result." });
    await loadConflicts();
  } catch (error) {
    showError(error, () => void resolveConflict(selection));
  }
}

function loadAgents() {
  return loadData("agents", ROUTES.agents, { request_id: requestId("agents") });
}

function loadSettings() {
  return loadData("settings", ROUTES.settingsSnapshot, { request_id: requestId("settings") });
}

function settingChanges(formValues) {
  const output = {};
  const configured = state.data.settings?.configured || {};
  const effective = state.data.settings?.effective || {};
  for (const [name, raw] of Object.entries(formValues)) {
    if (!name.startsWith("setting:")) continue;
    const setting = name.slice("setting:".length);
    const current = Object.hasOwn(configured, setting) ? configured[setting] : effective[setting];
    if (typeof current === "boolean") output[setting] = raw === "true";
    else if (typeof current === "number") output[setting] = Number(raw);
    else output[setting] = raw;
  }
  return output;
}

async function mutateSettings(mode, formValues) {
  state.drafts.settings = settingChanges(formValues);
  const route = mode === "apply" ? ROUTES.settingsApply : ROUTES.settingsPreview;
  try {
    const { payload } = await post(route, { changes: state.drafts.settings, request_id: requestId(`settings-${mode}`) });
    renderStatusBanner({ tone: "success", title: mode === "apply" ? "Settings applied" : "Settings preview", message: payload.reload_state || payload.predicted_reload_state || payload.status });
    if (mode === "apply") {
      state.drafts.settings = {};
      await loadSettings();
    }
  } catch (error) {
    showError(error, () => void mutateSettings(mode, formValues));
  }
}

function housekeepingBody(values) {
  const next = { ...state.drafts.housekeeping, ...values };
  state.drafts.housekeeping = next;
  return {
    ...scopePayload(next),
    max_records: Math.max(1, Number(next.maxRecords) || 1),
    required_free_bytes: Math.max(0, Number(next.requiredFreeBytes) || 0),
    request_id: requestId("housekeeping"),
  };
}

async function housekeeping(mode, values = state.drafts.housekeeping) {
  const body = housekeepingBody(values);
  const route = mode === "run" ? ROUTES.runHousekeeping : ROUTES.housekeepingPreview;
  try {
    const { payload } = await post(route, body);
    renderStatusBanner({
      tone: mode === "run" ? "success" : "info",
      title: mode === "run" ? "Housekeeping complete" : "Housekeeping preview",
      message: payload.status || "Preview ready",
    });
    renderCurrent();
  } catch (error) {
    showError(error, () => void housekeeping(mode, values));
  }
}

async function loadSupportPreview() {
  try {
    const { payload } = await post(ROUTES.supportPreview, housekeepingBody(state.drafts.housekeeping));
    state.data.supportPreview = payload;
    renderStatusBanner({ tone: "info", title: "Support preview ready", message: "No external upload occurred." });
    renderCurrent();
  } catch (error) {
    showError(error, () => void loadSupportPreview());
  }
}

const jobDone = (job) => ["SUCCEEDED", "FAILED"].includes(job?.phase);

async function runDbJob(route,body,job=null,fail=true){
  if(!job){
    if(state.db.job&&!jobDone(state.db.job))throw new ApiError(409,"DATABASE_JOB_IN_PROGRESS");
    job=(await post(route,body)).payload.job;
    state.db.job=job;renderStatusBanner({message:job.phase});renderCurrent();
  }
  if (!dbPoll) {
    dbPoll=(async()=>{
      while(!jobDone(job)){
        await new Promise(resolve=>window.setTimeout(resolve,CONNECT_POLL_MS));
        if (document.visibilityState !== "visible") continue;
        job=(await post(dbRoute("jobs/status"),{job_id:job.job_id},{activity: false})).payload.job;
        state.db.job=job;
      }
      return job;
    })().finally(()=>{dbPoll = null;});
  }
  job=await dbPoll;
  if(fail&&job.phase==="FAILED")throw new ApiError(409,job.error?.code||"DATABASE_JOB_FAILED",false,{job});
  return job;
}

async function database(action, values = {}) {
  try {
    if (action === "load") {
      if (state.db.job && !jobDone(state.db.job)) state.db.job = await runDbJob(null, null, state.db.job, false);
      if (state.db.job?.kind === "RESTORE_COMMIT" && state.db.restore && jobDone(state.db.job)) Object.assign(state.db.restore, { recoveryRequired: state.db.job.error?.code === "RESTORE_RECOVERY_REQUIRED", status: state.db.job.phase });
      state.db.artifacts = (await post(dbRoute("artifacts/list"), {})).payload.artifacts || [];
      markConnected();
      return renderCurrent();
    }
    if (action === "download") {
      const id = values.artifact_id;
      const { response } = await post(dbRoute(`artifacts/${encodeURIComponent(id)}/download`), {}, { method: "GET", contentType: null, parse: false });
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url; link.download = id; link.click(); URL.revokeObjectURL(url);
      return;
    }
    if (action === "delete") {
      const preview = state.db.deletePreview;
      if (preview?.artifact_id === values.artifact_id) {
        await post(dbRoute("artifacts/delete/commit"), { artifact_id: values.artifact_id, preview_digest: preview.preview_digest });
        state.db.deletePreview = null;
        return database("load");
      }
      state.db.deletePreview = (await post(dbRoute("artifacts/delete/preview"), { artifact_id: values.artifact_id })).payload.preview;
      return renderCurrent();
    }
    if (action === "backup") { await runDbJob(dbRoute("backup/create"), {}); return database("load"); }
    if (action === "export") {
      const lifecycle_states = String(values.lifecycleStates || "").split(",").map((item) => item.trim()).filter(Boolean);
      const max_records = Number(values.maxRecords);
      if (!lifecycle_states.length || new Set(lifecycle_states).size !== lifecycle_states.length || lifecycle_states.some((item) => !EXPORT_STATES.includes(item))) throw new ApiError(400, "INVALID_LIFECYCLE_STATES");
      if (!Number.isInteger(max_records) || max_records < 1 || max_records > 1000) throw new ApiError(400, "INVALID_MAX_RECORDS");
      const body = { ...scopePayload(values), lifecycle_states, max_records };
      if (values.operationId) body.operation_id = String(values.operationId);
      const job = await runDbJob(dbRoute("export/create"), body);
      await database("load");
      return database("download", { artifact_id: job.result?.artifact_id || job.artifact_id });
    }
    if (action === "commitImport") {
      const flow = state.db.importFlow;
      if (!flow) return;
      await runDbJob(dbRoute("import/commit"), { artifact_id: flow.artifactId, operation_id: flow.operationId });
      return database("load");
    }
    const restore = action === "restore";
    if (!restore && action !== "import") throw new ApiError(400, "DATABASE_ACTION_UNSUPPORTED");
    if (restore && values.trustedOrigin !== "on") throw new ApiError(400, "TRUSTED_ORIGIN_REQUIRED");
    const file = values.file;
    const maximum = restore ? RESTORE_MAX : IMPORT_MAX;
    if (!(file instanceof File) || file.size < 1 || file.size > maximum) throw new ApiError(400, "UPLOAD_SIZE_INVALID");
    const upload = (await post(dbRoute(restore ? "restore/upload" : "import/upload"), file, { contentType: restore ? "application/octet-stream" : "application/json" })).payload;
    const operationId = newOperationId();
    const body = { artifact_id: upload.artifact_id, operation_id: operationId };
    if (restore) body.trusted_origin_attested = true;
    const result = (await runDbJob(dbRoute(restore ? "restore/preview" : "import/preview"), body)).result || {};
    if (restore) {
      if (typeof result.authorization_preview !== "string") throw new ApiError(502, "RESTORE_PREVIEW_INVALID");
      state.db.restore = { artifactId: upload.artifact_id, operationId, authorizationPreview: result.authorization_preview, challengeId: null, verificationPhrase: "", grantSecret: null, acknowledged: false, recoveryRequired: false, status: "PREVIEW_READY" };
      navigate("restore");
    } else state.db.importFlow = { artifactId: upload.artifact_id, operationId, preview: result.destination_summary || {} };
    return database("load");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    if (action === "delete") state.db.deletePreview = null;
    showError(error, action === "delete" ? undefined : () => void database(action, values));
    renderCurrent();
  }
}

function beginRestoreChallenge() {
  const restore = state.db.restore;
  if (!restore?.authorizationPreview || !restore.acknowledged) return showError(new ApiError(400, "RESTORE_ACK_REQUIRED"));
  return beginSensitive(restore, () => void beginRestoreChallenge());
}

function mintRestoreGrant() {
  if (state.db.restore) return mintSensitive(state.db.restore, () => void mintRestoreGrant());
}

async function executeRestore(mode = "commit") {
  const restore = state.db.restore;
  if (!restore?.acknowledged || !restore.grantSecret) return showError(new ApiError(403, "RESTORE_GRANT_REQUIRED"));
  const grantSecret = restore.grantSecret;
  Object.assign(restore, { grantSecret: null, challengeId: null, verificationPhrase: "" });
  try {
    await runDbJob(dbRoute(mode === "retry" ? "restore/retry" : "restore/commit"), { artifact_id: restore.artifactId, operation_id: restore.operationId, state_restoration_acknowledged: true, grant_secret: grantSecret });
    Object.assign(restore, { recoveryRequired: false, status: "SUCCEEDED" });
    await database("load");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return;
    const recoveryRequired = state.db.job?.error?.code === "RESTORE_RECOVERY_REQUIRED";
    Object.assign(restore, { recoveryRequired, status: recoveryRequired ? "RECOVERY_REQUIRED" : "AUTHORIZATION_REQUIRED" });
    showError(error); renderCurrent();
  }
}

function renderCurrent() {
  if (!main) return;
  const requested = requestedState();
  const authenticated = Boolean(state.auth.csrf);
  const viewState = authenticated || requested === "connect" ? requested : "connect";
  let props = {};

  if (viewState === "overview") props = overviewProps();
  if (viewState === "browse") {
    props = {
      filters: state.browseFilters,
      items: (state.data.browse?.items || []).map(publicItem),
      searchSummary: browseSummary(state.data.browse),
      candidateDraft: state.candidateOpen ? (state.drafts.candidate || defaultCandidateDraft()) : null,
      onSearch: runBrowse,
      onAddCandidate: () => {
        state.candidateOpen = true;
        if (!state.drafts.candidate) state.drafts.candidate = defaultCandidateDraft();
        renderCurrent();
      },
      onCandidateDraftChange: (values) => {
        const existing = state.drafts.candidate || defaultCandidateDraft();
        state.drafts.candidate = { ...existing, ...values, idempotencyKey: existing.idempotencyKey };
      },
      onCreateCandidate: createCandidate,
      onOpenDetail: loadDetail,
    };
  }
  if (viewState === "detail") {
    const detail = state.data.detail || {};
    props = {
      memory: publicItem(detail.latest || {}),
      records: detail.records || [],
      events: detail.events || [],
      lifecycleDraft: state.drafts.lifecycle,
      onReview: detail.latest?.lifecycle_state === "CANDIDATE" ? openReview : undefined,
      onOpenLifecycleAction: openDetailLifecycleAction,
      onLifecycleDraftChange: (values) => {
        if (state.drafts.lifecycle) state.drafts.lifecycle = { ...state.drafts.lifecycle, ...values };
      },
      onLifecycleAction: mutateDetailLifecycle,
      onPurgePreview: previewPurge,
    };
  }
  if (viewState === "review") {
    const detail = state.data.detail || {};
    props = {
      candidate: publicItem(detail.latest || {}),
      verification: {
        source: detail.latest?.source_provenance?.source_ref,
        status: detail.latest?.freshness_gate_state || detail.latest?.lifecycle_state,
        source_provenance: detail.latest?.source_provenance,
        source_timestamp: detail.latest?.source_timestamp,
      },
      draft: state.drafts.review || {},
      onDraftChange: (values) => {
        if (state.drafts.review) state.drafts.review = { ...state.drafts.review, ...values };
      },
      onApprove: bind(reviewCandidate, "APPROVE"),
      onReject: bind(reviewCandidate, "REJECT"),
    };
  }
  if (viewState === "conflicts") {
    const first = state.data.conflicts?.items?.[0];
    const group = first?.group_cas || {};
    props = {
      group: { id: group.contradiction_key },
      members: (group.members || []).map((member) => ({ ...member, id: member.record_id, verificationStatus: member.freshness_gate_state })),
      preview: (group.members || []).map((member) => `${member.record_id}: ${member.lifecycle_state}`),
      onWinnerChange: () => {},
      onReasonChange: () => {},
      onResolve: resolveConflict,
    };
  }
  if (viewState === "database") {
    const busy = state.db.job && !jobDone(state.db.job);
    props = {
      artifacts: state.db.artifacts,
      deletePreview: state.db.deletePreview,
      importFlow: state.db.importFlow,
      housekeeping: state.drafts.housekeeping,
      onCreateBackup: busy ? undefined : bind(database, "backup"),
      onDownload: bind(database, "download"),
      onDeleteArtifact: busy ? undefined : bind(database, "delete"),
      onExport: busy ? undefined : bind(database, "export"),
      onImportPreview: busy ? undefined : bind(database, "import"),
      onImportCommit: busy || !state.db.importFlow ? undefined : bind(database, "commitImport"),
      onRestore: bind(navigate, "restore"),
      onHousekeeping: bind(housekeeping, "preview"),
      onRunHousekeeping: bind(housekeeping, "run"),
      onHousekeepingDraftChange: (values) => { state.drafts.housekeeping = { ...state.drafts.housekeeping, ...values }; },
    };
  }
  if (viewState === "agents") {
    props = {
      agents: state.data.agents?.items || [],
      guidance: "Configured state and direct runtime observations are shown separately.",
      onReviewSettings: bind(navigate, "settings"),
    };
  }
  if (viewState === "activity") {
    props = {
      events: [],
      diagnostics: state.data.supportPreview ? { description: "A local redacted support preview is available. No external upload occurred." } : { description: "Activity listing is unavailable because no production HTTP route exposes it in this slice." },
      onPreviewDiagnostics: loadSupportPreview,
    };
  }
  if (viewState === "settings") {
    props = {
      service: { state: "Unavailable through current HTTP routes", enabledAtBoot: "Unavailable", lastObserved: "Unavailable", observationSource: "Unavailable" },
      session: { inactivity: state.auth.expiresAt || "Server enforced", maximumAge: state.auth.absoluteExpiresAt || "Server enforced" },
      appearance: { language: currentLocale(), availableLanguages: ["en", "ar"], theme: "light", availableThemes: ["light"] },
      settings: state.data.settings || {},
      settingsDraft: state.drafts.settings,
      devices: [],
      onEndSession: logout,
      onLanguageChange: (locale) => { setLocale(locale); renderCurrent(); },
      onSettingsDraftChange: (values) => { state.drafts.settings = settingChanges(values); },
      onSettingsPreview: bind(mutateSettings, "preview"),
      onSettingsApply: bind(mutateSettings, "apply"),
    };
  }
  if (viewState === "recovery") {
    const purgeActive = Boolean(state.purge.operationId && state.purge.purgePreview);
    props = {
      message: purgeActive
        ? (state.purge.recoveryRequired
          ? "Permanent purge committed into a recovery-required state. Keep this exact operation identity and obtain a fresh sensitive grant before retrying."
          : "Review the exact permanent-purge preview and complete native-approved sensitive authorization before commit.")
        : "Recovery controls are unavailable because the current production HTTP surface exposes no general recovery resume route.",
      steps: purgeActive ? [
        { title: "Review exact preview", detail: "Verify target identities, digest, backup-retention warning, and required typed confirmation." },
        { title: "Native approval", detail: "Request a sensitive challenge and approve its phrase through the native Owner confirmation surface." },
        { title: state.purge.recoveryRequired ? "Fresh recovery retry" : "Commit once", detail: "A fresh grant is consumed for one attempt and is never reused." },
      ] : [],
      purge: purgeActive ? state.purge : null,
      onPurgeTypedChange: (value) => {
        state.purge.typedInput = value;
        state.purge.challengeId = null;
        state.purge.verificationPhrase = "";
        state.purge.grantSecret = null;
      },
      onPurgeChallenge: purgeActive ? () => void beginPurgeChallenge() : undefined,
      onPurgeGrant: purgeActive && state.purge.challengeId ? () => void mintPurgeGrant() : undefined,
      onPurgeExecute: purgeActive && state.purge.grantSecret
        ? () => void executePurge(state.purge.recoveryRequired ? "retry" : "commit")
        : undefined,
    };
  }
  if (viewState === "restore") {
    const restore = state.db.restore;
    const busy = state.db.job && !jobDone(state.db.job);
    props = {
      restore: restore || {},
      onPreview: busy ? undefined : bind(database, "restore"),
      onAcknowledge: restore ? (checked) => {
        restore.acknowledged = checked;
        restore.challengeId = null;
        restore.verificationPhrase = "";
        restore.grantSecret = null;
        renderCurrent();
      } : undefined,
      onChallenge: !busy && restore?.acknowledged ? beginRestoreChallenge : undefined,
      onGrant: !busy && restore?.challengeId ? mintRestoreGrant : undefined,
      onExecute: !busy && restore?.grantSecret ? bind(executeRestore, restore.recoveryRequired ? "retry" : "commit") : undefined,
    };
  }
  if (viewState === "connect") {
    props = {
      request: state.connect.request,
      locator: state.connect.locator,
      onLocatorChange: (value) => { state.connect.locator = value; },
      onClaim: claimConnect,
      onCancel: () => {
        stopConnectPolling();
        state.connect.request = {};
        state.connect.locator = "";
        state.connect.preauthReady = false;
        renderCurrent();
      },
    };
  }

  const view = stateRenderers[viewState](props);
  view.dataset.view = viewState;
  main.replaceChildren(view);
  const activeLink = document.querySelector(`[data-nav-key="${viewState}"]`);
  const viewTitle = view.querySelector("h1")?.textContent;
  setPage(viewState, activeLink?.textContent || viewTitle || viewState);
  const navigationBlocked = !authenticated || viewState === "recovery";
  setNavigationDisabled(navigationBlocked, viewState === "recovery" ? "Normal memory navigation is disabled while recovery requires attention." : "Authenticate to use Owner navigation.");
  setDrawerOpen(false);
  updateHeader();
}

async function loadRoute(name = requestedState()) {
  if (!state.auth.csrf && name !== "connect") {
    navigate("connect");
    return;
  }
  if (name === "connect") {
    try {
      await ensurePreauth();
    } catch (error) {
      showError(error, () => void loadRoute("connect"));
    }
    renderCurrent();
    return;
  }
  if (name === "overview") await loadOverview();
  else if (name === "browse") await runBrowse(state.browseFilters);
  else if (name === "conflicts") await loadConflicts();
  else if (name === "database" || name === "restore") await database("load");
  else if (name === "agents") await loadAgents();
  else if (name === "settings") await loadSettings();
  else renderCurrent();
}

wireShell({
  onLocaleChange: () => renderCurrent(),
  onGlobalSearch: (query) => {
    if (!state.auth.csrf) {
      navigate("connect");
      return;
    }
    state.browseFilters = { ...state.browseFilters, query };
    navigate("browse");
  },
  onSessionControl: () => {
    if (state.auth.csrf) void logout();
    else navigate("connect");
  },
});

setLocale(currentLocale());
window.addEventListener("hashchange", () => void loadRoute());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    void pollHealth();
    if (state.connect.locator) void pollConnect();
    checkSessionExpiry();
  }
});
window.setInterval(() => void pollHealth(), STATUS_POLL_MS);
window.setInterval(checkSessionExpiry, STATUS_POLL_MS);
if (startupConnectHandoff.error || startupConnectLocator) {
  renderCurrent();
  if (startupConnectHandoff.error) {
    showError(new ApiError(400, startupConnectHandoff.error));
  } else {
    void claimConnect(startupConnectLocator);
  }
  void pollHealth();
} else {
  void pollHealth();
  void loadRoute();
}
