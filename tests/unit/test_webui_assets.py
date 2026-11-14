from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WEBUI = ROOT / "src" / "factlane" / "webui"
ASSETS = WEBUI / "assets"
EXPECTED_MARK_SHA256 = "b13ff2b69e21734d916c091acef12df2c7b15cc4d86f4ebb81cb099b703287e9"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _js_compact(value: str) -> str:
    """Remove formatting whitespace outside JS string/template literals only."""

    output: list[str] = []
    quote: str | None = None
    escaped = False
    for character in value:
        if quote is not None:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'", "`"}:
            quote = character
            output.append(character)
        elif not character.isspace():
            output.append(character)
    return "".join(output)


def _has_js(source: str, snippet: str) -> bool:
    return _js_compact(snippet) in _js_compact(source)


def _localized_rows(source: str, name: str) -> list[list[str]]:
    legacy = re.search(
        rf"const\s+{re.escape(name)}\s*=\s*(\[\[.*?\]\]);const\s+",
        source,
    )
    if legacy:
        return json.loads(legacy.group(1))
    compact = re.search(
        rf"const\s+{re.escape(name)}\s*=\s*"
        r'\[("(?:\\.|[^"\\])*")\.split\("([^"]+)"\),'
        r'("(?:\\.|[^"\\])*")\.split\("([^"]+)"\)\];const\s+',
        source,
    )
    assert compact, f"{name} localization table missing"
    first = json.loads(compact.group(1))
    second = json.loads(compact.group(3))
    return [first.split(compact.group(2)), second.split(compact.group(4))]


def _production_files() -> list[Path]:
    return sorted(path for path in WEBUI.rglob("*") if path.is_file())


def test_static_foundation_has_local_entrypoint_and_expected_modules() -> None:
    assert (WEBUI / "index.html").is_file()
    assert (ASSETS / "app.css").is_file()
    assert (ASSETS / "app.js").is_file()
    assert (ASSETS / "conflicts.js").is_file()
    assert (ASSETS / "novice.js").is_file()
    assert (ASSETS / "shell.js").is_file()
    assert (ASSETS / "views.js").is_file()
    assert (ASSETS / "factlane-mark.svg").is_file()

    index = _text(WEBUI / "index.html")
    assert 'href="assets/app.css"' in index
    assert 'src="assets/app.js"' in index
    assert '<script type="module" src="assets/app.js"></script>' in index


def test_static_assets_are_self_contained_and_avoid_unsafe_script_patterns() -> None:
    files = _production_files()
    assert files

    for path in files:
        text = _text(path)
        suffix = path.suffix.lower()
        if suffix != ".svg":
            assert not re.search(r"(?:https?:)?//", text, re.IGNORECASE), path
        assert "javascript:" not in text.lower(), path
        assert "eval(" not in text, path
        assert "new Function(" not in text, path

    html = _text(WEBUI / "index.html")
    assert not re.search(r"<script(?![^>]+\bsrc=)[^>]*>", html, re.IGNORECASE)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE)
    assert "style=" not in html.lower()
    assert "'unsafe-inline'" not in html
    assert "'unsafe-eval'" not in html

    css = _text(ASSETS / "app.css")
    assert "@import" not in css.lower()
    assert "url(" not in css.lower()

    javascript = "\n".join(_text(path) for path in ASSETS.glob("*.js"))
    assert ".innerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "document.write" not in javascript


def test_asset_bundle_stays_within_local_size_budget() -> None:
    files = _production_files()
    sizes = {path.relative_to(WEBUI): path.stat().st_size for path in files}
    assert sum(sizes.values()) <= 1024 * 1024, sizes
    assert sum(sizes.values()) <= 156 * 1024, sizes
    assert max(sizes.values()) <= 68 * 1024, sizes


def test_assets_keep_proven_lean_contract_without_changing_visible_copy() -> None:
    app = _text(ASSETS / "app.js")
    shell = _text(ASSETS / "shell.js")
    views = _text(ASSETS / "views.js")
    css = _text(ASSETS / "app.css")

    for dead_state in ("primaryLocal", "grantExpiresAt"):
        assert dead_state not in app
    assert "state.data.housekeepingPreview" not in app
    assert "housekeepingPreview: null" not in app
    assert "shellCopy" not in shell
    assert "freshnessKind:" not in views
    for badge_variant in ("success", "warning", "danger", "info"):
        assert f".badge.{badge_variant}" not in css

    assert "function sourceInputs" not in views
    assert "function scopeInputs" not in views
    assert (ASSETS / "novice.js").stat().st_size < 10 * 1024
    for scope in (
        "PROJECT",
        "GLOBAL_USER",
        "WORKFLOW",
        "TOOL_ENVIRONMENT",
        "CROSS_PROJECT_WORKFLOW",
    ):
        assert scope in _text(ASSETS / "novice.js")

    assert _has_js(app, "defaultHousekeepingDraft = () =>")
    assert app.count("defaultHousekeepingDraft()") == 2

    assert not re.search(r"^\s{4}(?:status|listTitle|restoreTitle):", views, re.MULTILINE)
    assert "c.status" not in views
    assert "c.listTitle" not in views
    assert "c.restoreTitle" not in views
    for visible_copy in ("State", "الحالة", "Memories", "الذكريات", "Restore preview", "معاينة الاستعادة"):
        assert visible_copy in views

    keys = re.search(r'const\s+KEYS\s*=\s*"([^"]+)"\.split\(","\);', views)
    assert keys
    key_list = keys.group(1).split(",")
    localized = _localized_rows(views, "WORDS")
    assert len(localized) == 2
    assert all(len(row) == len(key_list) for row in localized)


def test_database_browser_workflows_match_current_http_contract_and_fail_closed() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")
    owner_http = _text(ROOT / "src" / "factlane" / "owner_http.py")

    assert _has_js(app, '"/api/database/" + suffix')
    for suffix in (
        "artifacts/list",
        "artifacts/delete/preview",
        "artifacts/delete/commit",
        "backup/create",
        "export/create",
        "import/upload",
        "import/preview",
        "import/commit",
        "restore/upload",
        "restore/preview",
        "jobs/status",
    ):
        assert f'"{suffix}"' in app
        assert f'"/api/database/{suffix}"' in owner_http
    for suffix in ("restore/commit", "restore/retry", "switch/preview", "switch/commit", "switch/retry"):
        assert f'"/api/database/{suffix}"' in owner_http
    assert 'restore.switchMode?"switch":"restore"' in app

    assert _has_js(app, 'headers["Content-Type"] = contentType')
    assert '"application/octet-stream"' in app
    assert _has_js(app, 'contentType = "application/json"')
    assert _has_js(app, "10 * 1024 * 1024")
    assert _has_js(app, "1024 * 1024 * 1024")
    assert _has_js(app, "body instanceof Blob ? body : JSON.stringify(body)")
    assert "instanceof File" in app
    assert "multipart/form-data" not in app
    assert "Content-Length" not in app
    assert "Transfer-Encoding" not in app
    assert 'activity:!1' in app or _has_js(app, 'activity: false')
    assert _has_js(app, 'headers["X-FactLane-CSRF"] = $H.csrf')
    assert _has_js(app, 'method: "GET"')
    assert "encodeURIComponent(" in app

    poll_flow = app.split("async function runDbJob", 1)[1].split("async function database", 1)[0]
    assert "CONNECT_POLL_MS" in poll_flow
    assert ('document.visibilityState==="visible"' in poll_flow) or _has_js(poll_flow, 'document.visibilityState !== "visible"')
    assert "dbPoll" in poll_flow
    assert "dbPoll||(dbPoll=" in poll_flow or _has_js(poll_flow, "if (!dbPoll)")
    assert _has_js(poll_flow, "dbPoll = null")
    assert 'activity:!1' in poll_flow or _has_js(poll_flow, 'activity: false')
    queued_job = _js_compact(poll_flow).split("if(!dbPoll)", 1)[0]
    assert "$r()" in queued_job
    assert "$b(" in queued_job

    database_flow = app.split("async function database", 1)[1].split("function beginRestoreChallenge", 1)[0]
    for field in ("scope", "project_id", "worktree_id", "workflow_id", "agent_id"):
        assert field in novice
    assert "scopeKey" in database_flow
    assert 'new ApiError(400,"SCOPE_REQUIRED")' in database_flow
    for field in ("lifecycle_states", "max_records"):
        assert field in database_flow
    assert re.search(r"(?<![A-Za-z0-9_])records:", database_flow) is None
    for forbidden in ("factlane_version", "storage_contract_version", "embedding_profile_id"):
        assert forbidden not in database_flow

    restore_execute = app.split("async function executeRestore", 1)[1].split("async function", 1)[0]
    for field in ("artifact_id", "operation_id", "state_restoration_acknowledged", "grant_secret"):
        assert field in restore_execute
    assert "preview_digest" not in restore_execute
    assert "authorization_preview" not in restore_execute
    assert _has_js(restore_execute, "grantSecret: null")

    assert "trusted_origin_attested" in app
    assert "authorization_preview" in app
    assert app.count("RESTORE_RECOVERY_REQUIRED") >= 2
    assert "Imported facts remain Candidate/unverified until review." in views
    assert "unavailableWorkflows" not in views


def test_shell_exposes_ltr_rtl_and_responsive_accessibility_hooks() -> None:
    index = _text(WEBUI / "index.html")
    css = _text(ASSETS / "app.css")
    shell = _text(ASSETS / "shell.js")

    assert '<html lang="en" dir="ltr">' in index
    assert 'data-drawer-toggle' in index
    assert 'aria-expanded="false"' in index
    assert ':focus-visible' in css
    assert 'html[dir=rtl]' in css or 'html[dir="rtl"]' in css
    assert '@media' in css
    assert 'data-drawer-open=true' in css or 'data-drawer-open="true"' in css
    assert "document.documentElement.lang=" in shell
    assert "document.documentElement.dir=" in shell
    assert 'dir:"rtl"' in shell
    assert 'العربية' in shell
    assert ".inert=" in shell
    assert '.setAttribute("aria-hidden","true")' in shell


def test_shell_has_status_and_staleness_banner_regions_without_fake_runtime_state() -> None:
    index = _text(WEBUI / "index.html")
    shell = _text(ASSETS / "shell.js")
    combined = index + shell

    assert 'data-status-banner' in index
    assert 'data-stale-banner' in index
    assert 'export function renderStatusBanner' in shell
    assert 'export function renderStaleBanner' in shell

    forbidden_defaults = (
        "example: connected",
        "synthetic examples",
        "20 results",
        "181 memories",
        "181",
        "237",
        "db-01",
        "b-204",
        "cobalt",
        "harbor",
        "citadel",
        "falcon",
    )
    lower = combined.lower()
    for value in forbidden_defaults:
        assert value not in lower


@pytest.mark.parametrize(
    "state",
    [
        "overview",
        "browse",
        "detail",
        "review",
        "conflicts",
        "database",
        "restore",
        "agents",
        "activity",
        "settings",
        "recovery",
        "connect",
    ],
)
def test_view_module_exports_required_state_renderer(state: str) -> None:
    views = _text(ASSETS / "views.js")
    expected = f"export function render{state.title()}State"
    assert expected in views


def test_primary_navigation_matches_normative_management_screen_order() -> None:
    index = _text(WEBUI / "index.html")
    keys = re.findall(r'data-nav-key="([a-z]+)"', index)
    assert keys == [
        "overview",
        "browse",
        "review",
        "conflicts",
        "database",
        "agents",
        "activity",
        "settings",
    ]


def test_state_registry_covers_normative_screen_families_and_existing_routes() -> None:
    views = _text(ASSETS / "views.js")
    for state in (
        "overview",
        "browse",
        "detail",
        "review",
        "conflicts",
        "database",
        "restore",
        "agents",
        "settings",
        "recovery",
        "connect",
    ):
        assert re.search(rf"(?:^|[{{,]){state}:render[A-Z][A-Za-z]+State(?:[,}}])", _js_compact(views)), state
    assert re.search(r"(?:^|[{{,])activity:renderActivityState(?:[,}])", _js_compact(views))
    assert "renderListState" not in views


def test_actions_fail_closed_without_callback() -> None:
    views = _text(ASSETS / "views.js")
    assert _has_js(views, 'button.disabled = typeof onClick !== "function"')
    assert _has_js(views, 'if (typeof onClick === "function") button.addEventListener("click", onClick)')


def test_official_factlane_mark_is_preserved_byte_for_byte() -> None:
    brand = (ASSETS / "factlane-mark.svg").read_bytes()
    assert hashlib.sha256(brand).hexdigest() == EXPECTED_MARK_SHA256
    assert brand == (ROOT / "docs" / "assets" / "brand" / "logo" / "factlane-mark.svg").read_bytes()


def test_app_uses_same_origin_http_routes_and_keeps_browser_secrets_in_memory() -> None:
    app = _text(ASSETS / "app.js")

    required_routes = (
        "/auth/preauth",
        "/auth/connect/claim",
        "/auth/connect/poll",
        "/auth/session/resume",
        "/auth/logout",
        "/api/read/overview",
        "/api/read/discovery",
        "/api/read/search",
        "/api/read/detail",
        "/api/read/conflicts",
        "/api/write/create_candidate",
        "/api/write/batch_review",
        "/api/write/revoke",
        "/api/write/reject",
        "/api/write/reverify",
        "/api/write/replace",
        "/api/write/manual_recheck",
        "/api/write/resolve",
        "/api/write/readmit",
        "/api/write/purge/preview",
        "/api/write/purge/commit",
        "/api/write/purge/retry",
        "/auth/sensitive/challenge",
        "/auth/sensitive/grant",
        "/api/operations/housekeeping_preview",
        "/api/operations/run_housekeeping",
        "/api/operations/agent_inventory",
        "/api/operations/settings_snapshot",
        "/api/operations/settings_preview",
        "/api/operations/settings_apply",
        "/api/operations/support_report_preview",
        "/api/service/status",
        "/api/service/stop",
    )
    for route in required_routes:
        assert route in app

    forbidden_routes = (
        "/api/backup",
        "/api/import",
        "/api/restore",
        "/api/purge",
        "/api/service/start",
        "/api/service/restart",
    )
    for route in forbidden_routes:
        assert route not in app

    assert 'serviceStatus:"/api/service/status"' in app
    assert 'serviceStop:"/api/service/stop"' in app
    assert 'post(ROUTES.serviceStatus,{})' in app
    assert 'request_id:$T.requestId' in app
    assert "Stop outcome unverified" in app
    assert "FactLane launcher" in app
    assert "/api/service/start" not in app
    assert "/api/service/restart" not in app

    assert "fetch(path" in app
    assert _has_js(app, 'credentials: "same-origin"')
    assert "window.localStorage" not in app
    assert "window.sessionStorage" not in app
    assert "localStorage" not in app
    assert "sessionStorage" not in app
    assert "URLSearchParams" in app
    assert 'p.set("csrf"' not in app
    assert 'p.set("session"' not in app
    assert 'p.set("grant"' not in app
    assert 'p.set("locator"' not in app
    assert "$H.csrf" in app
    assert 'setAttribute("aria-busy",String(v))' in app
    assert '.app-main[aria-busy=true]:before{content:"Loading…"' in _text(ASSETS / "app.css")


def test_owner_wide_discovery_and_candidate_review_expose_cursor_pagination() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    discovery = app.split("async function ensureDiscovery", 1)[1].split("function normalizedBrowseFilters", 1)[0]
    assert _has_js(discovery, "do {")
    assert "limit:100" in discovery
    assert "limit:200" not in discovery
    assert "payload.next_cursor" in discovery
    assert "DISCOVERY_CURSOR_LOOP" in discovery
    assert "Array.from(scopes.values())" in discovery

    browse = app.split("async function loadBrowsePage", 1)[1].split("async function createCandidate", 1)[0]
    assert "common.cursor=cursor" in browse
    assert "browseNextPage" in browse
    assert "browsePreviousPage" in browse

    review = app.split("async function loadReviewQueue", 1)[1].split("async function selectReviewCandidate", 1)[0]
    assert "body.cursor=cursor" in review
    assert "reviewNextPage" in review
    assert "reviewPreviousPage" in review

    localized = _localized_rows(views, "UX_WORDS")
    assert "Previous" in localized[0]
    assert "Next" in localized[0]
    assert "السابق" in localized[1]
    assert "التالي" in localized[1]


def test_prime_overview_and_review_preserve_scope_partialness_and_fresh_comparison() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    overview = app.split("function overviewProps", 1)[1].split("function defaultCandidateDraft", 1)[0]
    assert "scope_index" in overview
    assert "friendlyScopeLabel(i.scope)" in overview
    assert "next_cursor" in overview
    assert "project_breakdown" in overview
    assert "revisions:i.revisions" in overview and "conflicts:i.conflicts" in overview
    assert 'actionLabel:"Retry",onAction:pollHealth' in app
    assert "GROUP_VERSION_CONFLICT" in app and "refreshConflict" in app

    overview_view = views.split("export function renderOverviewState", 1)[1].split(
        "export function renderBrowseState", 1
    )[0]
    assert "coveragePartial" in overview_view
    assert "recordedRevisions" in overview_view and "conflictGroups" in overview_view
    assert "value.completeness" in views
    assert "groupChanged" in views

    review_view = views.split("export function renderReviewState", 1)[1].split(
        "export function renderConflictsState", 1
    )[0]
    assert "keeps this content unchanged" in review_view

    review_action = app.split("async function reviewCandidate", 1)[1].split(
        "async function loadData", 1
    )[0]
    assert "showConflict(reviewed" in review_action
    assert "refreshConflict(reviewed" in review_action
    assert "state.lifecycleConflict" in app


def test_every_webui_http_route_exists_on_the_current_production_http_surface() -> None:
    app = _text(ASSETS / "app.js")
    owner_http = _text(ROOT / "src" / "factlane" / "owner_http.py")
    app_routes = set(re.findall(r'"(/(?:api|auth)/[^" ]+)"', app))
    production_routes = set(re.findall(r'"(/(?:api|auth)/[^" ]+)"', owner_http))

    assert app_routes
    app_routes.discard("/api/database/")
    assert app_routes <= production_routes, sorted(app_routes - production_routes)


def test_app_handles_session_expiry_stale_detection_and_visibility_aware_polling() -> None:
    app = _text(ASSETS / "app.js")

    assert _has_js(app, "response.status === 401")
    assert "SESSION_EXPIRED" in app
    assert 'fetch("/health"' in app
    assert "STATUS_POLL_MS=15e3" in app
    assert "CONNECT_POLL_MS=2e3" in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert _has_js(app, 'document.visibilityState === "visible"')
    assert "renderStaleBanner" in app
    assert "pollInFlight" in app


def test_app_resumes_existing_session_before_connect_without_persisting_auth_secrets() -> None:
    app = _text(ASSETS / "app.js")

    assert _has_js(app, 'resume: "/auth/session/resume"')
    assert "let dbPoll=null,resumeTried=!1" in app or "let dbPoll=null,resumeTried=false" in app
    assert "async function resumeSession()" in app
    assert _has_js(app, "if (resumeTried) return")
    assert "resumeTried=!0" in app or _has_js(app, "resumeTried = true")
    assert _has_js(app, "acceptSession(payload, anchor)")
    assert "resumeTried=!1,resumeSession()" in app or _has_js(app, "resumeTried = false; void resumeSession()")
    assert "resumeSession();" in app or "void resumeSession()" in app
    assert "window.localStorage" not in app
    assert "window.sessionStorage" not in app

    compact_app = _js_compact(app)
    claim = compact_app.rfind("claimConnect(startupConnectLocator)")
    resume = compact_app.rfind("resumeSession()")
    assert claim >= 0 and resume >= 0 and claim < resume

    health_poll = app.split("async function pollHealth()", 1)[1].split("function updateHeader()", 1)[0]
    assert 'fetch("/health"' in health_poll
    assert "ROUTES." not in health_poll


def test_app_keeps_recoverable_drafts_in_page_memory_and_clears_lifecycle_context() -> None:
    app = _text(ASSETS / "app.js")

    assert "drafts:" in app
    assert "candidate:" in app
    assert "review:" in app
    assert "settings:" in app
    assert "housekeeping:" in app
    assert "clearLifecycleDraft" in app
    assert _has_js(app, "$F.review = null")
    assert "$F.candidate" in app


def test_shell_header_recovery_navigation_and_table_bidi_contracts_are_present() -> None:
    index = _text(WEBUI / "index.html")
    shell = _text(ASSETS / "shell.js")
    views = _text(ASSETS / "views.js")
    css = _text(ASSETS / "app.css")

    for hook in (
        "data-database-label",
        "data-connection-state",
        "data-refresh-age",
        "data-global-search",
        "data-session-control",
    ):
        assert hook in index

    assert "export function setHeaderState" in shell
    assert "export function setNavigationDisabled" in shell
    assert '.setAttribute("aria-disabled","true")' in shell
    assert '"bdi"' in views and 'dir:"auto"' in views
    assert _has_js(views, 'attrs: { role: "region", "aria-label":')
    assert "@media" in css and "max-width:1199px" in css


def test_scope_controls_use_only_current_public_scope_enum_and_reason_codes() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")

    scope_options = app.split("function scopeOptions()", 1)[1].split("function scopeForKey", 1)[0]
    assert "$A.discovery?.items" in scope_options
    assert "exactScope(item)" in scope_options
    assert "scopeFingerprint(scope)" in scope_options
    assert '"WORKTREE"' not in views
    assert '"WORKTREE"' not in novice

    for reason in (
        "INSUFFICIENT_EVIDENCE",
        "POLICY_REJECTED",
        "OWNER_ADJUDICATION",
        "STRONGER_EVIDENCE",
        "SOURCE_PRECEDENCE",
        "NEWER_VERIFIED_TRUTH",
    ):
        assert reason in views


def test_memories_default_to_owner_wide_discovery_and_global_search_clears_scope_filter() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    assert _has_js(app, 'discovery: "/api/read/discovery"')
    browse = app.split("async function loadBrowsePage", 1)[1].split("async function createCandidate", 1)[0]
    assert "$p(filters.scopeKey)" in browse
    assert "Object.assign(common,scope)" in browse
    assert "post(ROUTES.discovery,common)" in browse
    assert "scopePayload(filters)" not in browse

    global_search = app.split("onGlobalSearch:", 1)[1].split("onSessionControl:", 1)[0]
    assert _has_js(global_search, 'scopeKey: ""')
    assert _has_js(global_search, 'lifecycleState: ""')
    assert _has_js(global_search, 'sourceRef: ""')

    browse_view = views.split("export function renderBrowseState", 1)[1].split("export function renderListState", 1)[0]
    assert _has_js(browse_view, '$p("scopeKey", f.scopeKey, scopeOptions, { allowAll: true })')
    assert "scopeInputs(c, f)" not in browse_view
    assert "All memories" in views
    assert "كل الذكريات" in views
    memory_card = views.split("function memoryCard", 1)[1].split("export function renderOverviewState", 1)[0]
    assert "identifier(item.memory_id" not in memory_card
    assert "u.memoryItem" in memory_card


def test_scope_selection_keys_are_stable_exact_scope_fingerprints() -> None:
    app = _text(ASSETS / "app.js")
    scope_options = app.split("function scopeOptions()", 1)[1].split("function scopeForKey", 1)[0]

    assert "Array.from(byFingerprint,([key,scope])" in scope_options
    assert "{key,label:friendlyScopeLabel(scope),scope}" in scope_options or "{key:key,label:friendlyScopeLabel(scope),scope:scope}" in scope_options
    assert '"scope-" + (index + 1)' not in scope_options
    assert "scopeFingerprint(scope)" in app


def test_setup_navigation_lock_copy_is_platform_neutral() -> None:
    app = _text(ASSETS / "app.js")
    marker = "const navigationReason" if "const navigationReason" in app else "navigationReason="
    navigation = app.split(marker, 1)[1].split("setNavigationDisabled", 1)[0]

    assert "until setup finishes" in navigation
    assert "Windows setup" not in navigation


def test_review_route_loads_cross_scope_queue_then_exact_detail_before_review() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    queue = app.split("async function loadReviewQueue", 1)[1].split("async function selectReviewCandidate", 1)[0]
    assert "post(ROUTES.discovery" in queue
    assert 'lifecycle_state:"CANDIDATE"' in queue
    assert "scopePayload" not in queue

    select = app.split("async function selectReviewCandidate", 1)[1].split("async function reviewCandidate", 1)[0]
    assert "loadDetail(item,{navigateAfter:!1,renderAfter:!1})" in select or "loadDetail(item,{navigateAfter:false,renderAfter:false})" in select
    assert "openReview()" in select

    compact_app = _js_compact(app)
    route = compact_app.split('elseif(name==="review")', 1)[1].split('elseif(name==="conflicts")', 1)[0]
    assert "loadReviewQueue()" in route

    review = views.split("export function renderReviewState", 1)[1].split("export function renderConflictsState", 1)[0]
    assert _has_js(review, "queue = []")
    assert "onSelectCandidate" in review
    assert "noReviewCandidates" in review


def test_review_timestamp_preserves_original_until_edit_and_converts_local_time_fail_closed() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")

    assert _has_js(app, "sourceTimestampOriginal: sourceTimestamp")
    update = app.split("function updateReviewDraft", 1)[1].split("async function loadReviewQueue", 1)[0]
    assert _has_js(update, 'values.sourceTimestampLocal !== before.sourceTimestampLocal')
    assert "convertLocalTimestamp(values.sourceTimestampLocal)" in update
    assert _has_js(update, "next.sourceTimestamp = converted.iso || null")

    conversion = novice.split("export function convertLocalTimestamp", 1)[1].split("export function exactScope", 1)[0]
    assert "TIMESTAMP_INVALID" in conversion
    assert "TIMESTAMP_AMBIGUOUS" in conversion
    assert ".toISOString()" in conversion
    assert "1!==" in conversion and ">1" in conversion
    assert "getTimezoneOffset()" in novice
    assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in novice

    review = views.split("export function renderReviewState", 1)[1].split("export function renderConflictsState", 1)[0]
    assert _has_js(views, 'type: "datetime-local"')
    assert "field-error" in views
    assert "sourceTimestampError" in review
    assert "timezoneLabel" in review


def test_review_ui_does_not_render_record_revision_or_cas_inputs() -> None:
    views = _text(ASSETS / "views.js")
    review = views.split("export function renderReviewState", 1)[1].split("export function renderConflictsState", 1)[0]

    for raw_internal in ("observedGroupToken", "expectedRecordId", "expectedRevision", "record_id", "observed_group_token"):
        assert raw_internal not in review
    assert "advancedEvidence" in review
    assert "sourceSummary" in review


def test_context_required_operations_never_synthesize_blank_project_scope() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    detail = app.split("async function loadDetail", 1)[1].split("function detailLifecycleDraft", 1)[0]
    assert "exactScope(source)" in detail
    assert "$p(state.browseFilters.scopeKey)" in detail
    assert "scopePayload(state.browseFilters)" not in detail

    conflicts = app.split("async function loadConflicts", 1)[1].split("async function resolveConflict", 1)[0]
    assert "scopeKey=state.scopeContextKey" in conflicts
    assert "$p(scopeKey)" in conflicts
    assert "generation!==conflictLoadGeneration" in conflicts
    assert "scopeKey!==state.scopeContextKey" in conflicts
    assert "scopePayload(state.browseFilters)" not in conflicts
    assert "$p(scopeKey)" in conflicts
    assert "$A.conflicts=null" in conflicts

    resolve = app.split("async function resolveConflict", 1)[1].split("function loadAgents", 1)[0]
    assert '$q("conflicts-refresh")' in resolve
    assert "generation=conflictLoadGeneration" in resolve
    assert "scopeKey=state.scopeContextKey" in resolve
    assert "selectedKey=state.selectedConflictKey" in resolve
    assert "page=$P.conflicts" in resolve
    assert "generation!==conflictLoadGeneration||scopeKey!==state.scopeContextKey||selectedKey!==state.selectedConflictKey||page.cursor!==$P.conflicts.cursor" in resolve
    assert "conflictKey(item)===selectedKey" in resolve
    assert "draft.t!==currentGroup.observed_group_token" in resolve
    assert "group.observed_group_token!==draft.t" in resolve
    assert "group.observed_group_token" in resolve
    assert "winner.revision" in resolve

    housekeeping = app.split("function housekeepingBody", 1)[1].split("async function housekeeping", 1)[0]
    assert "$p(" in housekeeping and "scopeKey" in housekeeping
    assert 'new ApiError(400,"SCOPE_REQUIRED")' in housekeeping
    assert "scopePayload(next)" not in housekeeping

    conflicts_view = views.split("export function renderConflictsState", 1)[1].split("export function renderDatabaseState", 1)[0]
    database_view = views.split("export function renderDatabaseState", 1)[1].split("export function renderRestoreState", 1)[0]
    settings_view = views.split("export function renderSettingsState", 1)[1].split("export function renderPurgeRecoveryState", 1)[0]
    assert "$p(" in conflicts_view
    assert "$p(" in database_view
    assert "$p(" in settings_view
    assert "filesystem_free_bytes_before" in database_view
    assert "filesystem_free_bytes_after" in database_view
    assert "filesystem_free_bytes_before??_.a" in database_view
    assert "filesystem_free_bytes_after??_.a" in database_view
    assert '"Estimate","Before","After"' in database_view
    assert '"التقدير","قبل","بعد"' in database_view
    assert "physical_file_shrink_claimed" not in database_view


def test_conflict_hydration_is_human_readable_exact_member_bound_and_fail_closed() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    conflicts = _text(ASSETS / "conflicts.js")

    assert 'hydrateConflictSelection,selectedConflictGroup' in app
    assert "+=4" in conflicts
    for field in ("memory_id", "record_id", "revision", "scope"):
        assert field in conflicts
    assert '"STALE_CONFLICT_LOAD"' in conflicts
    assert '"CONFLICT_HYDRATION_MISMATCH"' in conflicts
    assert "export function selectedConflictGroup" in conflicts
    assert "export async function hydrateConflictSelection" in conflicts
    assert "$A.conflictReady=!1" in app or "$A.conflictReady=false" in app
    assert "$A.conflictReady&&members.length?resolveConflict:void 0" in app or "$A.conflictReady&&members.length?resolveConflict:undefined" in app

    conflict_view = views.split("export function renderConflictsState", 1)[1].split(
        "export function renderDatabaseState", 1
    )[0]
    assert "identifier(member.record_id" not in conflict_view
    assert "group.id" not in conflict_view
    assert "member.record_id || member.id" not in conflict_view
    assert "member.label || member.content" in conflict_view
    assert "member.content" in conflict_view
    assert 'member.lifecycle_state==="CANDIDATE"&&!candidateSeeds[value]' in conflict_view
    assert 'name:"verifiedNow"' in conflict_view
    assert "timestampInput(conflictDraft.sourceTimestampLocal" in conflict_view
    assert _has_js(views, 'name: "sourceTimestampLocal"')
    assert '$l(c.sourceClass,"sourceClass"' in conflict_view
    assert '$l(c.sourceRef,"sourceRef"' in conflict_view
    assert '$l(c.sourceHash,"sourceHash"' in conflict_view
    assert '$l(c.extractionMethod,"extractionMethod"' in conflict_view
    resolve = app.split("async function resolveConflict", 1)[1].split("function loadAgents", 1)[0]
    assert 'verification=verificationFromDraft(draft)' in resolve
    assert 'idempotency_key:draft.idempotencyKey' in resolve
    assert '!draft.verifiedNow||draft.sourceTimestampError' in resolve
    assert 'verification?{verification}:{}' in resolve or 'verification?{verification:verification}:{}' in resolve
    assert 'candidateSeeds:selected?.candidate_verification_seeds||{}' in app


def test_novice_candidate_creation_derives_truthful_provenance_without_hash_inputs() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")

    candidate_view = views.split("export function renderBrowseState", 1)[1].split(
        "function renderDetailLifecyclePanel", 1
    )[0]
    assert 'name: "sourceHash"' not in candidate_view
    assert 'name: "reviewRef"' not in candidate_view
    assert '"evidenceMode"' in candidate_view
    evidence_panel = views.split("function externalEvidencePanel", 1)[1].split("function memoryCard", 1)[0]
    assert '"evidenceText"' in evidence_panel
    assert '"sourceTimestampLocal"' in evidence_panel
    assert _has_js(app, "preparedEvidence(draft, draft.fact)")
    assert 'sourceClass:' in novice and '"OWNER_REQUEST"' in novice
    assert 'extractionMethod:' in novice and '"direct-owner-statement"' in novice
    assert "crypto.subtle.digest" in novice
    assert "(new TextEncoder).encode(String(" in novice
    assert "owner-panel-review-required:" in novice


def test_lifecycle_evidence_ui_hides_raw_hash_recheck_and_reference_choreography() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")
    lifecycle_view = views.split("function renderDetailLifecyclePanel", 1)[1].split(
        "export function renderDetailState", 1
    )[0]

    for raw_name in ("recheckRef", "observedSourceHash", "evidenceRef"):
        assert f'"{raw_name}"' not in lifecycle_view
    assert '"observedAtLocal"' in lifecycle_view
    assert _has_js(lifecycle_view, 'draft.claim === "OBSERVED_HASH"')
    assert '"evidenceText"' in lifecycle_view
    assert _has_js(app, "manualRecheckEvidence(draft, refreshed.latest)")
    assert "freshness_policy?.recheck_ref" in novice
    assert "observedEvidenceHash(" in novice and ".evidenceText" in novice
    assert _has_js(app, "replacementVerification(draft)")
    assert 'return"owner-panel-review:"+e.idempotencyKey' in novice
    assert "review_ref:o(" in novice
    replacement = novice.split("export async function replacementVerification", 1)[1]
    assert "review_ref:o(" in replacement


def test_detail_history_and_events_expose_independent_bounded_continuation() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")
    detail = app.split("async function loadDetail", 1)[1].split("function detailLifecycleDraft", 1)[0]

    assert "detailLoadGeneration" in detail
    assert "$c(payload)" in detail
    assert '$c(payload,"events_next_cursor")' in detail
    assert "async function loadDetailMore(stream)" in detail
    assert _has_js(detail, "body.event_cursor = cursor")
    assert _has_js(detail, "body.cursor = cursor")
    assert _has_js(detail, "pageKey !== $P.detail.key")
    assert _has_js(detail, "appendDetailPage($A.detail, payload, stream)")
    assert "export function appendDetailPage" in novice
    detail_view = views.split("export function renderDetailState", 1)[1].split(
        "export function renderReviewState", 1
    )[0]
    assert "onMoreHistory" in detail_view
    assert "onMoreEvents" in detail_view


def test_conflicts_expose_human_group_selection_page_navigation_and_fresh_same_page_rebind() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    conflicts = _text(ASSETS / "conflicts.js")

    assert "conflictNextPage" in app
    assert "conflictPreviousPage" in app
    assert "state.selectedConflictKey" in app
    assert "page.cursor&&(body.cursor=page.cursor)" in app or "if(page.cursor)body.cursor=page.cursor" in app
    resolve = app.split("async function resolveConflict", 1)[1].split("function loadAgents", 1)[0]
    assert "selectedKey!==state.selectedConflictKey" in resolve
    assert "page.cursor!==$P.conflicts.cursor" in resolve
    assert "winner.revision" in resolve
    assert "export function conflictKey" in conflicts
    conflict_view = views.split("export function renderConflictsState", 1)[1].split(
        "export function renderDatabaseState", 1
    )[0]
    assert _has_js(conflict_view, 'name: "conflictGroup"')
    assert "onGroupChange" in conflict_view
    assert "onPreviousPage" in conflict_view
    assert "onNextPage" in conflict_view


def test_diagnostics_are_locally_consumable_without_upload_or_new_transport() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    novice = _text(ASSETS / "novice.js")

    assert "downloadSupportPreview" in app
    assert "downloadRedactedSupportPreview($A.supportPreview)" in app
    settings = views.split("export function renderSettingsState", 1)[1].split(
        "export function renderPurgeRecoveryState", 1
    )[0]
    assert "generated_at" in settings
    assert "downloadDiagnostics" in settings
    assert "new Blob" in novice
    assert "URL.createObjectURL" in novice
    assert ".requires_explicit_download" in novice
    assert ".automatic_upload" in novice
    assert ".external_telemetry" in novice
    assert '"SUPPORT_PREVIEW_UNSAFE"' in novice
    assert "fetch(" not in novice
    assert "sendBeacon" not in novice


def test_activity_is_a_first_class_prime_surface_and_database_copy_is_bounded_to_real_operations() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    assert 'activity:"/api/operations/activity"' in app
    assert 'if(name==="activity"){$n("settings");return}' not in _js_compact(app)
    assert "export function renderActivityState" in views
    assert "Partial history; more results." in views
    assert "Database jobs" in views
    assert "Compatibility/integrity" in views
    assert "Manage backup, restore, import/export, and bounded housekeeping." in views
    assert "t.failure&&v(_.action,[t.failure.code,t.failure.action])" in views
    assert "e.source_approval_policy" in views
    assert "e.destination_results?.length" in views
    assert "o.preview&&$m" in views
    assert "o.job.result?.outcome" in views
    assert "Object.entries(i.result||i.detail||{})" in views


def test_purge_flow_binds_exact_preview_and_never_resubmits_targets_or_reuses_grant() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    assert "purgePreview:" in app
    assert "authorizationPreview:" in app
    assert "typedConfirmation:" in app
    assert "grantSecret:" in app
    assert "recoveryRequired:" in app
    assert "async function previewPurge" in app
    assert "function beginPurgeChallenge" in app
    assert "function mintPurgeGrant" in app
    assert "async function beginSensitive" in app
    assert "async function mintSensitive" in app
    assert "async function executePurge" in app
    assert "clearGrant(state.purge)" in app
    assert _has_js(app, 'mode === "retry" ? ROUTES.purgeRetry : ROUTES.purgeCommit')

    execute = app.split("async function executePurge", 1)[1].split("function", 1)[0]
    assert "targets" not in execute
    assert "operation_id" in execute
    assert "preview_digest" in execute
    assert "typed_confirmation" in execute
    assert "grant_secret" in execute

    assert "renderPurgeRecoveryState" in views
    assert "previewDigest" in views
    assert "typedConfirmation" in views


def test_detail_lifecycle_actions_recheck_latest_record_and_group_cas_before_mutation() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    for action in ("REVOKE", "REVERIFY", "REPLACE", "MANUAL_RECHECK", "READMIT"):
        assert action in app
    assert "async function mutateDetailLifecycle" in app
    lifecycle = app.split("async function mutateDetailLifecycle", 1)[1].split("async function", 1)[0]
    assert "loadDetail" in lifecycle
    assert "expectedRecordId" in lifecycle
    assert "expectedRevision" in lifecycle
    assert "observedGroupToken" in lifecycle
    assert "VERSION_CONFLICT" in lifecycle

    assert "renderDetailLifecyclePanel" in views
    assert "onLifecycleAction" in views


def test_prime_residual_review_gaps_stay_closed_in_static_owner_ui_contract() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    owner_auth = _text(ROOT / "src" / "factlane" / "owner_auth.py")
    owner_http = _text(ROOT / "src" / "factlane" / "owner_http.py")

    lifecycle = views.split("function renderDetailLifecyclePanel", 1)[1].split(
        "export function renderDetailState", 1
    )[0]
    assert _has_js(
        lifecycle,
        'memory.freshness_gate_state !== "SOURCE_CHANGE_REVIEW_REQUIRED" && '
        'actions.push($a(c.reverify',
    )
    assert "Ordinary reverify is unavailable; use Manual recheck." in lifecycle
    assert 'READMIT:"VALIDATED_CURRENT / CANDIDATE / existing equivalent"' in lifecycle
    assert 'READMIT:"VALIDATED_CURRENT"' not in lifecycle

    review_props = app.split('if(viewState==="review")', 1)[1].split(
        'if(viewState==="conflicts")', 1
    )[0]
    assert "memory_id:p.memory_id" in review_props
    assert "scope:friendlyScopeLabel(exactScope(p.latest))" in review_props
    review_view = views.split("export function renderReviewState", 1)[1].split(
        "export function renderConflictsState", 1
    )[0]
    assert "$n(item.memory_id)" in review_view
    assert "item.scope" in review_view

    for route in (
        'remoteEnroll:"/auth/enroll/remote"',
        'remotePreview:"/auth/remote/preview"',
        'remoteApprove:"/auth/remote/approve"',
    ):
        assert route in app
    for function_name in ("requestRemote", "previewRemote", "approveRemote"):
        assert f"function {function_name}" in app
    assert "q.deviceLabel" in views
    assert "q.locator" in views
    assert "q.requestPhrase" in views
    assert "q.verificationPhrase" in views
    assert '"verification_phrase": pending.verification_phrase' in owner_auth
    assert '"challenge_id": challenge.challenge_id' in owner_http
    assert '{"locator", "grant", "challenge_id", "csrf"}' in owner_http


def test_session_warning_uses_server_expiry_without_authenticated_polling() -> None:
    app = _text(ASSETS / "app.js")

    assert "SESSION_WARNING_MS=12e4" in app
    assert "idleWindowMs" in app
    assert "idle_remaining_seconds" in app
    assert "absolute_remaining_seconds" in app
    assert "SESSION_TIMING_INVALID" in app
    assert "Number(payload.expires_at) * 1000 - Date.now()" not in app
    assert "payload.expires_at" not in app
    assert "payload.absolute_expires_at" not in app
    assert "const anchor=Date.now()" in app
    assert "!Number.isFinite(idle)" in app
    assert "!Number.isFinite(abs)" in app
    assert "idle<0" in app
    assert "abs<0" in app
    assert "idle*=1e3,abs*=1e3" in app or "idle*=1e3;abs*=1e3" in app
    assert "expiresAt:(anchor+Math.min(idle,abs))/1e3" in app
    assert "absoluteExpiresAt:(anchor+abs)/1e3" in app

    epoch_ms = 1_794_004_214_700.0
    monotonic_idle_deadline_seconds = 41_419.468
    assert monotonic_idle_deadline_seconds * 1000 - epoch_ms < 0

    idle_remaining_ms = 1_800.0 * 1000
    absolute_remaining_ms = 28_800.0 * 1000
    expires_at_ms = epoch_ms + min(idle_remaining_ms, absolute_remaining_ms)
    absolute_expires_at_ms = epoch_ms + absolute_remaining_ms
    assert expires_at_ms - epoch_ms == pytest.approx(1_800_000.0)
    assert absolute_expires_at_ms - epoch_ms == pytest.approx(28_800_000.0)
    assert "function refreshSessionDeadline" in app
    assert "function checkSessionExpiry" in app
    assert "Session expires soon" in app
    assert "clearSensitiveContext" in app

    health_poll = app.split("async function pollHealth()", 1)[1].split("function updateHeader()", 1)[0]
    assert "ROUTES." not in health_poll


def test_windows_setup_shell_ui_is_gated_by_authenticated_setup_signal_and_reports_restart_truthfully() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    assert _has_js(app, 'setupStatus: "/api/setup/status"')
    assert _has_js(app, 'setupBind: "/api/setup/bind"')
    assert "payload.setup_required===!0" in app or _has_js(app, "payload.setup_required === true")
    assert "async function loadSetupStatus" in app
    assert "async function submitSetup" in app
    assert 'status:"CONFIG_PUBLISHED_RESTART_REQUESTED"' in app
    assert "renderSetupState" in views
    assert "CONFIG_PUBLISHED_RESTART_REQUIRED" in views
    assert "Non-secret host ID" in views


def test_owner_capability_projection_disables_unavailable_destructive_and_database_actions() -> None:
    app = _text(ASSETS / "app.js")

    assert _has_js(app, 'state.capabilities = Object.hasOwn(payload, "capabilities") ? payload.capabilities : null')
    assert '$u("permanent_purge")?previewPurge:void 0' in app or _has_js(app, '$u("permanent_purge") ? previewPurge : undefined')
    assert 'databaseSupported=$u("database_management")' in app or _has_js(app, 'const databaseSupported = $u("database_management")')
    assert 'restoreSupported=$u("full_restore")' in app or _has_js(app, 'const restoreSupported = $u("full_restore")')
    assert '$u("database_management")' in app
    assert 'dbRoute("status")' in app
    assert 'dbRoute("jobs/list")' in app


def test_native_connect_locator_is_scrubbed_before_application_fetch_or_render() -> None:
    app = _text(ASSETS / "app.js")
    index = _text(WEBUI / "index.html")

    assert 'fragment.startsWith("#connect=")' in app
    assert 'fragment.slice(9)' in app or 'fragment.slice("#connect=".length)' in app
    assert 'encoded.includes("&connect=")' in app
    assert "decodeURIComponent(encoded)" in app
    assert "CONNECT_LOCATOR_MALFORMED" in app
    assert _has_js(app, "locator.length > 512")
    assert _has_js(app, 'url.search = ""')
    assert "history.replaceState" in app
    assert "startupConnectLocator" in app
    assert '<meta name="referrer" content="no-referrer">' in index

    scrub = app.index("history.replaceState")
    first_fetch = app.index("fetch(")
    first_render_call = app.index("wireShell(")
    assert scrub < first_fetch
    assert scrub < first_render_call


def test_novice_read_surfaces_hide_transport_internals_without_masking_destructive_identity() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")

    assert "new Intl.DateTimeFormat" in views
    assert "function formatSessionDeadline(value)" in views
    assert '"ar-EG":"en-US"' in views
    assert 'timeZoneName:"short"' in views

    overview = views.split("export function renderOverviewState", 1)[1].split(
        "export function renderBrowseState", 1
    )[0]
    assert "lastObserved" in overview and "$o(" in overview

    detail = views.split("export function renderDetailState", 1)[1].split(
        "export function renderReviewState", 1
    )[0]
    assert "identifier(memory.memory_id" not in detail
    assert "identifier(record.record_id" not in detail
    assert "event.event_id" not in detail
    for field in ("memory.last_verified_at", "memory.source_timestamp", "record.last_verified_at", "event.occurred_at"):
        assert field in detail
    assert 'text:`${n[5]} ${record.revision}`' in _js_compact(detail)

    review = views.split("export function renderReviewState", 1)[1].split(
        "export function renderConflictsState", 1
    )[0]
    assert "source.sourceTimestampOriginal||verification.source_timestamp" in _js_compact(review)

    database = views.split("export function renderDatabaseState", 1)[1].split(
        "export function renderRestoreState", 1
    )[0]
    for field in ("registered_at", "artifact_id", "sha256", "preview_digest"):
        assert field in database

    agents = views.split("export function renderAgentsState", 1)[1].split(
        "export function renderActivityState", 1
    )[0]
    assert "last_seen" in agents and "$o(" in agents

    settings = views.split("export function renderSettingsState", 1)[1].split(
        "export function renderPurgeRecoveryState", 1
    )[0]
    assert settings.count("formatSessionDeadline(") >= 2
    assert "inactivity" in settings
    assert "maximumAge" in settings
    assert "sessionIdleDeadline" in settings
    assert "sessionAbsoluteDeadline" in settings

    purge = views.split("export function renderPurgeRecoveryState", 1)[1]
    assert "$n(target.memory_id)" in purge
    assert "$n(target.record_id)" in purge
    assert _has_js(purge, "[c.previewDigest, purge.previewDigest ? $n(purge.previewDigest) : null]")
    assert _has_js(purge, "[c.expectedConfirmation, purge.typedConfirmation]")
    assert _has_js(purge, "[c.nativeApprovalPhrase, purge.verificationPhrase]")

    restore = views.split("export function renderRestoreState", 1)[1].split(
        "export function renderAgentsState", 1
    )[0]
    assert "operationId" in restore and "$n(" in restore
    assert ".authorizationPreview" in restore
    assert ".verificationPhrase" in restore

    assert _has_js(app, 'session: { inactivity: $H.expiresAt || "Server enforced", maximumAge: $H.absoluteExpiresAt || "Server enforced" }')
