from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest

from factlane.owner_auth import (
    AuthError,
    NativeConfirmationRequest,
    OwnerAuthState,
)


OWNER_UID = 1000
ORIGIN = "https://factlane-test.test:8765"
REMOTE_ORIGIN = "https://factlane.example.test"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeNativeConfirmation:
    approved: bool = True

    def __post_init__(self) -> None:
        self.requests: list[NativeConfirmationRequest] = []

    def confirm(self, request: NativeConfirmationRequest) -> bool:
        self.requests.append(request)
        return self.approved


def make_state(
    *,
    clock: FakeClock | None = None,
    confirmation: FakeNativeConfirmation | None = None,
) -> tuple[OwnerAuthState, FakeClock, FakeNativeConfirmation]:
    clock = clock or FakeClock()
    confirmation = confirmation or FakeNativeConfirmation()
    state = OwnerAuthState(
        owner_uid=OWNER_UID,
        native_confirmation=confirmation,
        clock=clock,
    )
    return state, clock, confirmation


def enroll(
    state: OwnerAuthState,
    *,
    source: str = "127.0.0.1",
    origin: str = ORIGIN,
    device_label: str = "Primary browser",
):
    pending = state.begin_enrollment(
        caller_uid=OWNER_UID,
        source=source,
        device_label=device_label,
    )
    preauth = state.issue_preauth(origin=origin)
    claim = state.claim_pending(
        locator=pending.locator,
        origin=origin,
        preauth_secret=preauth,
    )
    assert claim.verification_phrase == pending.verification_phrase
    assert state.poll_pending(
        locator=pending.locator,
        origin=origin,
        preauth_secret=preauth,
    ) is None
    state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    credentials = state.poll_pending(
        locator=pending.locator,
        origin=origin,
        preauth_secret=preauth,
    )
    assert credentials is not None
    return credentials


def request_remote(
    state: OwnerAuthState,
    *,
    source: str = "203.0.113.8",
    origin: str = REMOTE_ORIGIN,
    device_label: str = "Remote laptop",
):
    preauth = state.issue_preauth(origin=origin)
    pending = state.begin_remote_enrollment(
        source=source,
        device_label=device_label,
        origin=origin,
        preauth_secret=preauth,
    )
    return pending, preauth


def approve_remote(
    state: OwnerAuthState,
    *,
    primary,
    pending,
    primary_origin: str = ORIGIN,
) -> None:
    preview = state.remote_enrollment_preview(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=primary_origin,
    )
    challenge = state.begin_sensitive_challenge(
        session_secret=primary.session_secret,
        origin=primary_origin,
        preview=preview,
    )
    state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=primary.session_secret,
        origin=primary_origin,
    )
    state.approve_remote_enrollment(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=primary_origin,
        csrf_secret=primary.csrf_secret,
        grant_secret=grant.grant_secret,
    )


def assert_error_code(exc_info: pytest.ExceptionInfo[AuthError], code: str) -> None:
    assert exc_info.value.code == code


def secret_entropy_bytes(secret: str) -> int:
    return len(base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4)))


def test_pending_claim_is_bound_to_exact_origin_and_preauth_cookie_and_conflict_invalidates() -> None:
    state, _, confirmation = make_state()
    pending = state.begin_enrollment(
        caller_uid=OWNER_UID,
        source="127.0.0.1",
        device_label="Chrome local",
    )
    first_preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(
        locator=pending.locator,
        origin=ORIGIN,
        preauth_secret=first_preauth,
    )

    conflicting_preauth = state.issue_preauth(origin=ORIGIN)
    with pytest.raises(AuthError) as exc_info:
        state.claim_pending(
            locator=pending.locator,
            origin=ORIGIN,
            preauth_secret=conflicting_preauth,
        )
    assert_error_code(exc_info, "PENDING_CLAIM_CONFLICT")

    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_INVALID")
    assert confirmation.requests == []

    fresh = state.begin_enrollment(
        caller_uid=OWNER_UID,
        source="127.0.0.2",
        device_label="Chrome local retry",
    )
    preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(locator=fresh.locator, origin=ORIGIN, preauth_secret=preauth)
    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=fresh.locator, caller_uid=OWNER_UID + 1)
    assert_error_code(exc_info, "CALLER_UID_MISMATCH")
    assert confirmation.requests == []

    state.approve_pending(locator=fresh.locator, caller_uid=OWNER_UID)
    assert confirmation.requests[-1].kind == "ENROLLMENT"
    assert confirmation.requests[-1].caller_uid == OWNER_UID
    assert confirmation.requests[-1].source == "127.0.0.2"
    assert confirmation.requests[-1].origin == ORIGIN
    assert confirmation.requests[-1].device_label == "Chrome local retry"
    assert confirmation.requests[-1].verification_phrase == fresh.verification_phrase

    with pytest.raises(AuthError) as exc_info:
        state.poll_pending(
            locator=fresh.locator,
            origin="https://other.test:8765",
            preauth_secret=preauth,
        )
    assert_error_code(exc_info, "PENDING_BINDING_MISMATCH")

    credentials = state.poll_pending(
        locator=fresh.locator,
        origin=ORIGIN,
        preauth_secret=preauth,
    )
    assert credentials is not None
    assert credentials.primary_local is True


def test_local_pending_approval_distinguishes_open_not_claimed_from_invalid_or_expired() -> None:
    state, clock, confirmation = make_state()
    pending = state.begin_enrollment(
        caller_uid=OWNER_UID,
        source="LOCAL_NATIVE",
        device_label="FactLane local browser",
    )

    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_NOT_CLAIMED")
    assert exc_info.value.retryable is True
    assert confirmation.requests == []

    clock.advance(5 * 60)
    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_EXPIRED")


def test_remote_enrollment_needs_no_uid_is_browser_bound_and_cannot_use_local_uid_approval() -> None:
    state, _, confirmation = make_state()
    pending, preauth = request_remote(state)

    assert state.poll_pending(
        locator=pending.locator,
        origin=REMOTE_ORIGIN,
        preauth_secret=preauth,
    ) is None

    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "REMOTE_APPROVAL_REQUIRES_PRIMARY_SESSION")
    assert confirmation.requests == []

    with pytest.raises(AuthError) as exc_info:
        state.poll_pending(
            locator=pending.locator,
            origin="https://other.example.test",
            preauth_secret=preauth,
        )
    assert_error_code(exc_info, "PENDING_BINDING_MISMATCH")

    conflicting_preauth = state.issue_preauth(origin=REMOTE_ORIGIN)
    with pytest.raises(AuthError) as exc_info:
        state.claim_pending(
            locator=pending.locator,
            origin=REMOTE_ORIGIN,
            preauth_secret=conflicting_preauth,
        )
    assert_error_code(exc_info, "PENDING_CLAIM_CONFLICT")

    with pytest.raises(AuthError) as exc_info:
        state.poll_pending(
            locator=pending.locator,
            origin=REMOTE_ORIGIN,
            preauth_secret=preauth,
        )
    assert_error_code(exc_info, "PENDING_INVALID")


def test_primary_local_session_approves_remote_with_csrf_and_exact_fresh_single_use_grant() -> None:
    state, _, _ = make_state()
    primary = enroll(state)
    assert primary.primary_local is True
    pending, preauth = request_remote(state)

    preview = state.remote_enrollment_preview(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=ORIGIN,
    )
    assert preview == (
        '{"device_label":"Remote laptop","locator":"'
        + pending.locator
        + '","operation":"APPROVE_REMOTE_ENROLLMENT","origin":"https://factlane.example.test",'
        '"source":"203.0.113.8"}'
    )
    challenge = state.begin_sensitive_challenge(
        session_secret=primary.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=primary.session_secret,
        origin=ORIGIN,
    )

    with pytest.raises(AuthError) as exc_info:
        state.approve_remote_enrollment(
            locator=pending.locator,
            session_secret=primary.session_secret,
            origin=ORIGIN,
            csrf_secret="wrong",
            grant_secret=grant.grant_secret,
        )
    assert_error_code(exc_info, "CSRF_INVALID")

    state.approve_remote_enrollment(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=ORIGIN,
        csrf_secret=primary.csrf_secret,
        grant_secret=grant.grant_secret,
    )
    with pytest.raises(AuthError) as exc_info:
        state.consume_sensitive_grant(
            grant_secret=grant.grant_secret,
            session_secret=primary.session_secret,
            origin=ORIGIN,
            preview=preview,
        )
    assert_error_code(exc_info, "SENSITIVE_GRANT_INVALID")

    remote = state.poll_pending(
        locator=pending.locator,
        origin=REMOTE_ORIGIN,
        preauth_secret=preauth,
    )
    assert remote is not None
    assert remote.primary_local is False
    context = state.validate_session(
        session_secret=remote.session_secret,
        origin=REMOTE_ORIGIN,
    )
    assert context.primary_local is False


def test_remote_approval_rejects_wrong_preview_grant_and_remote_sessions_cannot_approve_devices() -> None:
    state, _, _ = make_state()
    primary = enroll(state)
    pending, preauth = request_remote(state)
    preview = state.remote_enrollment_preview(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=ORIGIN,
    )
    wrong_challenge = state.begin_sensitive_challenge(
        session_secret=primary.session_secret,
        origin=ORIGIN,
        preview=preview + " changed",
    )
    state.approve_sensitive_challenge(challenge_id=wrong_challenge.challenge_id, caller_uid=OWNER_UID)
    wrong_grant = state.mint_sensitive_grant(
        challenge_id=wrong_challenge.challenge_id,
        session_secret=primary.session_secret,
        origin=ORIGIN,
    )
    with pytest.raises(AuthError) as exc_info:
        state.approve_remote_enrollment(
            locator=pending.locator,
            session_secret=primary.session_secret,
            origin=ORIGIN,
            csrf_secret=primary.csrf_secret,
            grant_secret=wrong_grant.grant_secret,
        )
    assert_error_code(exc_info, "SENSITIVE_PREVIEW_MISMATCH")

    challenge = state.begin_sensitive_challenge(
        session_secret=primary.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=primary.session_secret,
        origin=ORIGIN,
    )
    state.approve_remote_enrollment(
        locator=pending.locator,
        session_secret=primary.session_secret,
        origin=ORIGIN,
        csrf_secret=primary.csrf_secret,
        grant_secret=grant.grant_secret,
    )
    remote = state.poll_pending(
        locator=pending.locator,
        origin=REMOTE_ORIGIN,
        preauth_secret=preauth,
    )
    assert remote is not None and remote.primary_local is False

    second_pending, _ = request_remote(
        state,
        source="203.0.113.9",
        device_label="Remote tablet",
    )
    with pytest.raises(AuthError) as exc_info:
        state.remote_enrollment_preview(
            locator=second_pending.locator,
            session_secret=remote.session_secret,
            origin=REMOTE_ORIGIN,
        )
    assert_error_code(exc_info, "PRIMARY_LOCAL_SESSION_REQUIRED")

    remote_challenge = state.begin_sensitive_challenge(
        session_secret=remote.session_secret,
        origin=REMOTE_ORIGIN,
        preview="forged remote approval",
    )
    state.approve_sensitive_challenge(challenge_id=remote_challenge.challenge_id, caller_uid=OWNER_UID)
    remote_grant = state.mint_sensitive_grant(
        challenge_id=remote_challenge.challenge_id,
        session_secret=remote.session_secret,
        origin=REMOTE_ORIGIN,
    )
    with pytest.raises(AuthError) as exc_info:
        state.approve_remote_enrollment(
            locator=second_pending.locator,
            session_secret=remote.session_secret,
            origin=REMOTE_ORIGIN,
            csrf_secret=remote.csrf_secret,
            grant_secret=remote_grant.grant_secret,
        )
    assert_error_code(exc_info, "PRIMARY_LOCAL_SESSION_REQUIRED")


def test_preauth_session_and_csrf_secrets_are_independent_256_bit_values_and_not_stored_plaintext() -> None:
    state, _, _ = make_state()
    pending = state.begin_enrollment(caller_uid=OWNER_UID, source="local", device_label="Browser")
    preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    credentials = state.poll_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    assert credentials is not None

    secrets = {preauth, credentials.session_secret, credentials.csrf_secret}
    assert len(secrets) == 3
    for secret in secrets:
        assert secret_entropy_bytes(secret) == 32
        assert secret not in repr(state.__dict__)

    state.validate_session(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        csrf_secret=credentials.csrf_secret,
        require_csrf=True,
        user_activity=True,
    )
    with pytest.raises(AuthError) as exc_info:
        state.validate_session(
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            csrf_secret="wrong",
            require_csrf=True,
            user_activity=True,
        )
    assert_error_code(exc_info, "CSRF_INVALID")


def test_pending_expires_after_five_minutes() -> None:
    state, clock, _ = make_state()
    pending = state.begin_enrollment(caller_uid=OWNER_UID, source="local", device_label="Browser")
    preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    clock.advance(300)

    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_EXPIRED")


def test_polling_does_not_renew_idle_session_but_user_activity_does_and_absolute_age_wins() -> None:
    state, clock, _ = make_state()
    credentials = enroll(state)

    clock.advance(1_700)
    state.validate_session(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        user_activity=False,
    )
    clock.advance(100)
    with pytest.raises(AuthError) as exc_info:
        state.validate_session(
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            user_activity=False,
        )
    assert_error_code(exc_info, "SESSION_EXPIRED")

    state, clock, _ = make_state()
    credentials = enroll(state)
    for _ in range(16):
        clock.advance(1_700)
        state.validate_session(
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            user_activity=True,
        )
    clock.advance(1_599)
    state.validate_session(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        user_activity=True,
    )
    clock.advance(1)
    with pytest.raises(AuthError) as exc_info:
        state.validate_session(
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            user_activity=True,
        )
    assert_error_code(exc_info, "SESSION_EXPIRED")


def test_sensitive_grant_is_preview_bound_single_use_and_expires_from_challenge_approval() -> None:
    state, clock, confirmation = make_state()
    credentials = enroll(state)
    preview = "Purge 2 exact records from PROJECT project-a"

    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    state.approve_sensitive_challenge(
        challenge_id=challenge.challenge_id,
        caller_uid=OWNER_UID,
    )
    assert confirmation.requests[-1].kind == "SENSITIVE_ACTION"
    assert confirmation.requests[-1].preview == preview

    clock.advance(299)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
    )
    clock.advance(2)
    with pytest.raises(AuthError) as exc_info:
        state.consume_sensitive_grant(
            grant_secret=grant.grant_secret,
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            preview=preview,
        )
    assert_error_code(exc_info, "SENSITIVE_GRANT_EXPIRED")

    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
    )
    with pytest.raises(AuthError) as exc_info:
        state.consume_sensitive_grant(
            grant_secret=grant.grant_secret,
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            preview=preview + " changed",
        )
    assert_error_code(exc_info, "SENSITIVE_PREVIEW_MISMATCH")

    state.consume_sensitive_grant(
        grant_secret=grant.grant_secret,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    with pytest.raises(AuthError) as exc_info:
        state.consume_sensitive_grant(
            grant_secret=grant.grant_secret,
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            preview=preview,
        )
    assert_error_code(exc_info, "SENSITIVE_GRANT_INVALID")


def test_begin_sensitive_challenge_is_idempotent_for_same_preview_and_supersedes_changed_preview() -> None:
    state, _, _ = make_state()
    credentials = enroll(state)

    first = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Purge exact record A",
    )
    duplicate = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Purge exact record A",
    )
    assert duplicate == first

    replacement = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Purge exact record B",
    )
    assert replacement.challenge_id != first.challenge_id
    with pytest.raises(AuthError) as exc_info:
        state.mint_sensitive_grant(
            challenge_id=first.challenge_id,
            session_secret=credentials.session_secret,
            origin=ORIGIN,
        )
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_INVALID")


def test_approve_single_pending_sensitive_challenge_none_and_cross_session_ambiguity_do_not_prompt() -> None:
    state, _, confirmation = make_state()
    first_session = enroll(state, source="127.0.0.1", device_label="Browser one")
    second_session = enroll(state, source="127.0.0.2", device_label="Browser two")
    prompts_before = len(confirmation.requests)

    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_NONE")
    assert len(confirmation.requests) == prompts_before

    state.begin_sensitive_challenge(
        session_secret=first_session.session_secret,
        origin=ORIGIN,
        preview="Purge browser one preview",
    )
    state.begin_sensitive_challenge(
        session_secret=second_session.session_secret,
        origin=ORIGIN,
        preview="Purge browser two preview",
    )
    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_AMBIGUOUS")
    assert len(confirmation.requests) == prompts_before


def test_approve_single_pending_sensitive_challenge_preserves_exact_binding_and_mints_only_to_browser_session() -> None:
    state, _, confirmation = make_state()
    credentials = enroll(state)
    other = enroll(state, source="127.0.0.2", device_label="Other browser")
    preview = "Restore exact artifact backup-123"
    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview=preview,
    )

    approved = state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)

    assert approved.challenge_id == challenge.challenge_id
    assert approved.preview == preview
    assert approved.approved_at is not None
    request = confirmation.requests[-1]
    assert request.kind == "SENSITIVE_ACTION"
    assert request.verification_phrase == challenge.verification_phrase
    assert request.origin == ORIGIN
    assert request.preview == preview
    with pytest.raises(AuthError) as exc_info:
        state.mint_sensitive_grant(
            challenge_id=challenge.challenge_id,
            session_secret=other.session_secret,
            origin=ORIGIN,
        )
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_BINDING_MISMATCH")
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
    )
    assert grant.grant_secret


def test_approve_single_pending_sensitive_challenge_freezes_selection_when_second_challenge_appears_during_dialog() -> None:
    class HookedConfirmation(FakeNativeConfirmation):
        def __init__(self) -> None:
            super().__init__(approved=True)
            self.hook = None

        def confirm(self, request: NativeConfirmationRequest) -> bool:
            self.requests.append(request)
            if request.kind == "SENSITIVE_ACTION" and self.hook is not None:
                hook, self.hook = self.hook, None
                hook()
            return self.approved

    confirmation = HookedConfirmation()
    state, _, _ = make_state(confirmation=confirmation)
    first_session = enroll(state, source="127.0.0.1", device_label="Browser one")
    second_session = enroll(state, source="127.0.0.2", device_label="Browser two")
    selected = state.begin_sensitive_challenge(
        session_secret=first_session.session_secret,
        origin=ORIGIN,
        preview="Selected preview",
    )
    created: list[object] = []

    def create_second() -> None:
        created.append(
            state.begin_sensitive_challenge(
                session_secret=second_session.session_secret,
                origin=ORIGIN,
                preview="Later preview",
            )
        )

    confirmation.hook = create_second
    approved = state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)

    assert approved.challenge_id == selected.challenge_id
    assert confirmation.requests[-1].preview == "Selected preview"
    assert len(created) == 1
    later = state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert later.challenge_id == created[0].challenge_id
    assert confirmation.requests[-1].preview == "Later preview"


def test_approve_single_pending_sensitive_challenge_denial_removes_selected_challenge() -> None:
    state, _, confirmation = make_state()
    credentials = enroll(state)
    state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Denied preview",
    )
    confirmation.approved = False

    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "NATIVE_CONFIRMATION_DENIED")
    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_NONE")


def test_approve_single_pending_sensitive_challenge_does_not_replay_approved_or_expired_session_challenges() -> None:
    state, clock, confirmation = make_state()
    credentials = enroll(state)
    state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Approve once",
    )
    state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    prompt_count = len(confirmation.requests)

    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_NONE")
    assert len(confirmation.requests) == prompt_count

    expiring = enroll(state, source="127.0.0.9", device_label="Expiring browser")
    state.begin_sensitive_challenge(
        session_secret=expiring.session_secret,
        origin=ORIGIN,
        preview="Expired session preview",
    )
    clock.advance(30 * 60)
    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_NONE")
    assert len(confirmation.requests) == prompt_count + 1


def test_approve_single_pending_sensitive_challenge_rejects_epoch_or_session_expiry_during_dialog() -> None:
    class HookedConfirmation(FakeNativeConfirmation):
        def __init__(self) -> None:
            super().__init__(approved=True)
            self.hook = None

        def confirm(self, request: NativeConfirmationRequest) -> bool:
            self.requests.append(request)
            if request.kind == "SENSITIVE_ACTION" and self.hook is not None:
                hook, self.hook = self.hook, None
                hook()
            return self.approved

    confirmation = HookedConfirmation()
    state, clock, _ = make_state(confirmation=confirmation)
    credentials = enroll(state)
    state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Epoch changes during confirmation",
    )
    confirmation.hook = state.invalidate_all_sessions_after_maintenance
    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_INVALID")

    credentials = enroll(state, source="127.0.0.3", device_label="Fresh browser")
    state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Session expires during confirmation",
    )
    confirmation.hook = lambda: clock.advance(30 * 60)
    with pytest.raises(AuthError) as exc_info:
        state.approve_single_pending_sensitive_challenge(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SESSION_EXPIRED")


def test_sensitive_grant_expiry_is_capped_by_remaining_session_lifetime() -> None:
    state, clock, _ = make_state()
    credentials = enroll(state)
    for _ in range(16):
        clock.advance(1_700)
        state.validate_session(
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            user_activity=True,
        )
    clock.advance(1_400)
    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Full restore backup abc",
    )
    approved = state.approve_sensitive_challenge(
        challenge_id=challenge.challenge_id,
        caller_uid=OWNER_UID,
    )
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
    )
    assert approved.approved_at is not None
    assert approved.approved_at + 300 > credentials.absolute_expires_at
    assert grant.expires_at == pytest.approx(credentials.absolute_expires_at)

    clock.advance(200)
    with pytest.raises(AuthError) as exc_info:
        state.consume_sensitive_grant(
            grant_secret=grant.grant_secret,
            session_secret=credentials.session_secret,
            origin=ORIGIN,
            preview="Full restore backup abc",
        )
    assert_error_code(exc_info, "SESSION_EXPIRED")


def test_credential_recovery_increments_epoch_and_revokes_sessions_pending_and_challenges() -> None:
    state, _, confirmation = make_state()
    credentials = enroll(state)
    pending = state.begin_enrollment(caller_uid=OWNER_UID, source="remote", device_label="Remote")
    preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=preauth)
    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview="Purge one record",
    )

    old_epoch = state.auth_epoch
    new_epoch = state.recover_credentials(caller_uid=OWNER_UID)
    assert new_epoch == old_epoch + 1
    assert confirmation.requests[-1].kind == "CREDENTIAL_RECOVERY"

    with pytest.raises(AuthError) as exc_info:
        state.validate_session(session_secret=credentials.session_secret, origin=ORIGIN)
    assert_error_code(exc_info, "SESSION_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_INVALID")


def test_internal_post_maintenance_invalidation_revokes_all_auth_state_without_native_prompt() -> None:
    state, _, confirmation = make_state()
    credentials = enroll(state)

    pending = state.begin_enrollment(caller_uid=OWNER_UID, source="maintenance-pending", device_label="Pending browser")
    pending_preauth = state.issue_preauth(origin=ORIGIN)
    state.claim_pending(locator=pending.locator, origin=ORIGIN, preauth_secret=pending_preauth)
    unbound_preauth = state.issue_preauth(origin=REMOTE_ORIGIN)

    preview = "Post-maintenance invalidation exact test preview"
    challenge = state.begin_sensitive_challenge(
        session_secret=credentials.session_secret,
        origin=ORIGIN,
        preview=preview,
    )
    state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    grant = state.mint_sensitive_grant(
        challenge_id=challenge.challenge_id,
        session_secret=credentials.session_secret,
        origin=ORIGIN,
    )

    requests_before = list(confirmation.requests)
    old_epoch = state.auth_epoch
    new_epoch = state.invalidate_all_sessions_after_maintenance()

    assert new_epoch == old_epoch + 1
    assert state.auth_epoch == new_epoch
    assert confirmation.requests == requests_before
    assert state._preauth == {}
    assert state._pending == {}
    assert state._sessions == {}
    assert state._challenges == {}
    assert state._grants == {}

    with pytest.raises(AuthError) as exc_info:
        state.validate_session(session_secret=credentials.session_secret, origin=ORIGIN)
    assert_error_code(exc_info, "SESSION_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.approve_pending(locator=pending.locator, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "PENDING_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.approve_sensitive_challenge(challenge_id=challenge.challenge_id, caller_uid=OWNER_UID)
    assert_error_code(exc_info, "SENSITIVE_CHALLENGE_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.begin_remote_enrollment(
            source="203.0.113.15",
            device_label="Old preauth browser",
            origin=REMOTE_ORIGIN,
            preauth_secret=unbound_preauth,
        )
    assert_error_code(exc_info, "PREAUTH_INVALID")
    assert grant.grant_secret


def test_internal_post_maintenance_invalidation_advances_epoch_exactly_once_per_call() -> None:
    state, _, confirmation = make_state()
    assert state.auth_epoch == 1
    first = state.invalidate_all_sessions_after_maintenance()
    second = state.invalidate_all_sessions_after_maintenance()
    assert first == 2
    assert second == 3
    assert state.auth_epoch == 3
    assert confirmation.requests == []


def test_credential_recovery_remains_native_confirmed_and_denial_does_not_rotate_epoch() -> None:
    confirmation = FakeNativeConfirmation(approved=True)
    state, _, _ = make_state(confirmation=confirmation)
    credentials = enroll(state)
    confirmation.approved = False
    old_epoch = state.auth_epoch

    with pytest.raises(AuthError) as exc_info:
        state.recover_credentials(caller_uid=OWNER_UID)
    assert_error_code(exc_info, "NATIVE_CONFIRMATION_DENIED")
    assert state.auth_epoch == old_epoch
    assert confirmation.requests[-1].kind == "CREDENTIAL_RECOVERY"
    state.validate_session(session_secret=credentials.session_secret, origin=ORIGIN)


def test_credential_recovery_revokes_remote_sessions_and_remote_pending_requests() -> None:
    state, _, _ = make_state()
    primary = enroll(state)
    approved_pending, approved_preauth = request_remote(state)
    approve_remote(state, primary=primary, pending=approved_pending)
    remote = state.poll_pending(
        locator=approved_pending.locator,
        origin=REMOTE_ORIGIN,
        preauth_secret=approved_preauth,
    )
    assert remote is not None and remote.primary_local is False
    waiting_pending, waiting_preauth = request_remote(
        state,
        source="203.0.113.10",
        device_label="Waiting phone",
    )

    state.recover_credentials(caller_uid=OWNER_UID)

    with pytest.raises(AuthError) as exc_info:
        state.validate_session(session_secret=primary.session_secret, origin=ORIGIN)
    assert_error_code(exc_info, "SESSION_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.validate_session(session_secret=remote.session_secret, origin=REMOTE_ORIGIN)
    assert_error_code(exc_info, "SESSION_INVALID")
    with pytest.raises(AuthError) as exc_info:
        state.poll_pending(
            locator=waiting_pending.locator,
            origin=REMOTE_ORIGIN,
            preauth_secret=waiting_preauth,
        )
    assert_error_code(exc_info, "PENDING_INVALID")


def test_rate_limits_enforce_five_per_source_per_minute_and_twenty_live_pending() -> None:
    state, clock, _ = make_state()
    for index in range(5):
        state.begin_enrollment(caller_uid=OWNER_UID, source="same", device_label=f"same-{index}")
    with pytest.raises(AuthError) as exc_info:
        state.begin_enrollment(caller_uid=OWNER_UID, source="same", device_label="sixth")
    assert_error_code(exc_info, "SOURCE_RATE_LIMIT")

    clock.advance(60)
    state.begin_enrollment(caller_uid=OWNER_UID, source="same", device_label="after-window")

    state, _, _ = make_state()
    for index in range(20):
        state.begin_enrollment(caller_uid=OWNER_UID, source=f"source-{index}", device_label=f"device-{index}")
    with pytest.raises(AuthError) as exc_info:
        state.begin_enrollment(caller_uid=OWNER_UID, source="source-overflow", device_label="overflow")
    assert_error_code(exc_info, "PENDING_LIMIT")


def test_remote_enrollment_uses_the_same_source_and_total_pending_limits() -> None:
    state, _, _ = make_state()
    for index in range(5):
        request_remote(state, source="198.51.100.7", device_label=f"remote-{index}")
    preauth = state.issue_preauth(origin=REMOTE_ORIGIN)
    with pytest.raises(AuthError) as exc_info:
        state.begin_remote_enrollment(
            source="198.51.100.7",
            device_label="remote-sixth",
            origin=REMOTE_ORIGIN,
            preauth_secret=preauth,
        )
    assert_error_code(exc_info, "SOURCE_RATE_LIMIT")

    state, _, _ = make_state()
    for index in range(19):
        state.begin_enrollment(
            caller_uid=OWNER_UID,
            source=f"local-{index}",
            device_label=f"local-{index}",
        )
    request_remote(state, source="198.51.100.8", device_label="twentieth")
    preauth = state.issue_preauth(origin=REMOTE_ORIGIN)
    with pytest.raises(AuthError) as exc_info:
        state.begin_remote_enrollment(
            source="198.51.100.9",
            device_label="overflow",
            origin=REMOTE_ORIGIN,
            preauth_secret=preauth,
        )
    assert_error_code(exc_info, "PENDING_LIMIT")


def test_at_most_eight_live_owner_sessions_are_minted() -> None:
    state, _, _ = make_state()
    sessions = [enroll(state, source=f"source-{index}") for index in range(8)]
    assert len(sessions) == 8

    pending, preauth = request_remote(state, source="source-8", device_label="ninth remote")
    approve_remote(state, primary=sessions[0], pending=pending)
    with pytest.raises(AuthError) as exc_info:
        state.poll_pending(locator=pending.locator, origin=REMOTE_ORIGIN, preauth_secret=preauth)
    assert_error_code(exc_info, "SESSION_LIMIT")

    state.logout(sessions[1].session_secret)
    ninth = state.poll_pending(locator=pending.locator, origin=REMOTE_ORIGIN, preauth_secret=preauth)
    assert ninth is not None
    assert ninth.primary_local is False
