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


def _production_files() -> list[Path]:
    return sorted(path for path in WEBUI.rglob("*") if path.is_file())


def test_static_foundation_has_local_entrypoint_and_expected_modules() -> None:
    assert (WEBUI / "index.html").is_file()
    assert (ASSETS / "app.css").is_file()
    assert (ASSETS / "app.js").is_file()
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
    assert sum(sizes.values()) <= 128 * 1024, sizes
    assert max(sizes.values()) <= 64 * 1024, sizes


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

    assert "const SCOPE_OPTIONS = Object.freeze" in views
    assert "Object.freeze([value, value])" in views
    assert "SCOPE_OPTIONS" in views.split("function scopeInputs", 1)[1].split("function", 1)[0]
    for scope in (
        "PROJECT",
        "GLOBAL_USER",
        "WORKFLOW",
        "TOOL_ENVIRONMENT",
        "CROSS_PROJECT_WORKFLOW",
    ):
        assert scope in views

    assert "defaultHousekeepingDraft = () =>" in app
    assert app.count("defaultHousekeepingDraft()") == 2

    assert not re.search(r"^\s{4}(?:status|listTitle|restoreTitle):", views, re.MULTILINE)
    assert "c.status" not in views
    assert "c.listTitle" not in views
    assert "c.restoreTitle" not in views
    for visible_copy in ("State", "الحالة", "Memories", "الذكريات", "Restore preview", "معاينة الاستعادة"):
        assert visible_copy in views

    keys = re.search(r'const KEYS = "([^"]+)"\.split\(","\);', views)
    words = re.search(r"const WORDS = (\[\[.*\]\]);", views)
    assert keys and words
    key_list = keys.group(1).split(",")
    localized = json.loads(words.group(1))
    assert len(localized) == 2
    assert all(len(row) == len(key_list) for row in localized)


def test_database_browser_workflows_match_current_http_contract_and_fail_closed() -> None:
    app = _text(ASSETS / "app.js")
    views = _text(ASSETS / "views.js")
    owner_http = _text(ROOT / "src" / "factlane" / "owner_http.py")

    assert '"/api/database/" + suffix' in app
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
        "restore/commit",
        "restore/retry",
        "jobs/status",
    ):
        assert f'"{suffix}"' in app
        assert f'"/api/database/{suffix}"' in owner_http

    assert 'headers["Content-Type"] = contentType' in app
    assert '"application/octet-stream"' in app
    assert 'contentType = "application/json"' in app
    assert "10 * 1024 * 1024" in app
    assert "1024 * 1024 * 1024" in app
    assert "body instanceof Blob ? body : JSON.stringify(body)" in app
    assert "const file = values.file" in app
    assert "multipart/form-data" not in app
    assert "Content-Length" not in app
    assert "Transfer-Encoding" not in app
    assert 'activity: false' in app
    assert 'headers["X-FactLane-CSRF"] = state.auth.csrf' in app
    assert 'method: "GET"' in app
    assert "encodeURIComponent(id)" in app

    poll_flow = app.split("async function runDbJob", 1)[1].split("async function database", 1)[0]
    assert "CONNECT_POLL_MS" in poll_flow
    assert 'document.visibilityState !== "visible"' in poll_flow
    assert "dbPoll" in poll_flow
    assert "if (!dbPoll)" in poll_flow
    assert "dbPoll = null" in poll_flow
    assert 'activity: false' in poll_flow
    queued_job = poll_flow.split("if (!dbPoll)", 1)[0]
    assert "renderCurrent()" in queued_job
    assert "renderStatusBanner" in queued_job

    export_flow = app.split('if (action === "export")', 1)[1].split('if (action === "commitImport")', 1)[0]
    scope_flow = app.split("function scopePayload", 1)[1].split("function", 1)[0]
    for field in ("scope", "project_id", "worktree_id", "workflow_id", "agent_id"):
        assert field in scope_flow
    assert "...scopePayload(values)" in export_flow
    for field in ("lifecycle_states", "max_records"):
        assert field in export_flow
    assert "records:" not in export_flow
    for forbidden in ("factlane_version", "storage_contract_version", "embedding_profile_id"):
        assert forbidden not in export_flow

    restore_execute = app.split("async function executeRestore", 1)[1].split("async function", 1)[0]
    for field in ("artifact_id", "operation_id", "state_restoration_acknowledged", "grant_secret"):
        assert field in restore_execute
    assert "preview_digest" not in restore_execute
    assert "authorization_preview" not in restore_execute
    assert "grantSecret: null" in restore_execute

    assert "trusted_origin_attested" in app
    assert "authorization_preview" in app
    assert app.count('error?.code === "RESTORE_RECOVERY_REQUIRED"') == 2
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
    assert 'html[dir="rtl"]' in css
    assert '@media' in css
    assert 'data-drawer-open="true"' in css
    assert 'document.documentElement.lang = locale' in shell
    assert 'document.documentElement.dir = copy.dir' in shell
    assert 'dir: "rtl"' in shell
    assert 'العربية' in shell
    assert 'drawer.inert = closed' in shell
    assert 'drawer.setAttribute("aria-hidden", "true")' in shell


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
        "list",
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
        "list",
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
    ):
        assert re.search(rf"^\s*{state}: render[A-Z][A-Za-z]+State,?$", views, re.MULTILINE), state


def test_actions_fail_closed_without_callback() -> None:
    views = _text(ASSETS / "views.js")
    assert 'button.disabled = typeof onClick !== "function"' in views
    assert 'if (typeof onClick === "function") button.addEventListener("click", onClick)' in views


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
        "/auth/logout",
        "/api/read/overview",
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
    )
    for route in required_routes:
        assert route in app

    forbidden_routes = (
        "/api/backup",
        "/api/import",
        "/api/restore",
        "/api/purge",
        "/api/service/start",
        "/api/service/stop",
        "/api/service/restart",
    )
    for route in forbidden_routes:
        assert route not in app

    assert "fetch(path" in app
    assert 'credentials: "same-origin"' in app
    assert "window.localStorage" not in app
    assert "window.sessionStorage" not in app
    assert "localStorage" not in app
    assert "sessionStorage" not in app
    assert "URLSearchParams" not in app
    assert "state.auth.csrf" in app


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

    assert "response.status === 401" in app
    assert "SESSION_EXPIRED" in app
    assert 'fetch("/health"' in app
    assert "15_000" in app
    assert "2_000" in app
    assert 'document.addEventListener("visibilitychange"' in app
    assert 'document.visibilityState === "visible"' in app
    assert "renderStaleBanner" in app
    assert "pollInFlight" in app

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
    assert "state.drafts.review = null" in app
    assert "state.drafts.candidate" in app


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
    assert 'nav.setAttribute("aria-disabled", "true")' in shell
    assert 'element("bdi"' in views
    assert 'attrs: { role: "region", "aria-label":' in views
    assert '@media (max-width: 1199px)' in css


def test_scope_controls_use_only_current_public_scope_enum_and_reason_codes() -> None:
    views = _text(ASSETS / "views.js")

    for scope in (
        "GLOBAL_USER",
        "PROJECT",
        "WORKFLOW",
        "TOOL_ENVIRONMENT",
        "CROSS_PROJECT_WORKFLOW",
    ):
        assert f'"{scope}"' in views.split("const SCOPE_OPTIONS", 1)[1].split(";", 1)[0]
    assert '"WORKTREE"' not in views.split("const SCOPE_OPTIONS", 1)[1].split(";", 1)[0]
    assert "Object.freeze([value, value])" in views

    for reason in (
        "INSUFFICIENT_EVIDENCE",
        "POLICY_REJECTED",
        "OWNER_ADJUDICATION",
        "STRONGER_EVIDENCE",
        "SOURCE_PRECEDENCE",
        "NEWER_VERIFIED_TRUTH",
    ):
        assert reason in views


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
    assert "state.purge.grantSecret = null" in app
    assert 'mode === "retry" ? ROUTES.purgeRetry : ROUTES.purgeCommit' in app

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


def test_session_warning_uses_server_expiry_without_authenticated_polling() -> None:
    app = _text(ASSETS / "app.js")

    assert "SESSION_WARNING_MS = 120_000" in app
    assert "idleWindowMs" in app
    assert "function refreshSessionDeadline" in app
    assert "function checkSessionExpiry" in app
    assert "Session expires soon" in app
    assert "clearSensitiveContext" in app

    health_poll = app.split("async function pollHealth()", 1)[1].split("function updateHeader()", 1)[0]
    assert "ROUTES." not in health_poll


def test_native_connect_locator_is_scrubbed_before_application_fetch_or_render() -> None:
    app = _text(ASSETS / "app.js")
    index = _text(WEBUI / "index.html")

    assert 'fragment.startsWith("#connect=")' in app
    assert 'fragment.slice("#connect=".length)' in app
    assert 'encoded.includes("&connect=")' in app
    assert "decodeURIComponent(encoded)" in app
    assert "CONNECT_LOCATOR_MALFORMED" in app
    assert "locator.length > 512" in app
    assert 'url.search = ""' in app
    assert "history.replaceState" in app
    assert "startupConnectLocator" in app
    assert '<meta name="referrer" content="no-referrer">' in index

    scrub = app.index("history.replaceState")
    first_fetch = app.index("fetch(")
    first_render_call = app.index("wireShell(")
    assert scrub < first_fetch
    assert scrub < first_render_call
