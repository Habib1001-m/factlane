from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import urlsplit


PENDING_TTL_SECONDS = 5 * 60
SESSION_IDLE_TTL_SECONDS = 30 * 60
SESSION_ABSOLUTE_TTL_SECONDS = 8 * 60 * 60
SENSITIVE_CHALLENGE_FRESH_SECONDS = 5 * 60
SOURCE_RATE_WINDOW_SECONDS = 60
MAX_PENDING_PER_SOURCE_PER_MINUTE = 5
MAX_PENDING_TOTAL = 20
MAX_LIVE_SESSIONS = 8


class AuthError(RuntimeError):
    """Stable owner-auth failure with a machine-readable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class NativeConfirmationRequest:
    """Data passed to the injected native confirmation implementation."""

    kind: str
    verification_phrase: str
    auth_epoch: int
    caller_uid: int
    locator: str | None = None
    source: str | None = None
    origin: str | None = None
    device_label: str | None = None
    preview: str | None = None


class NativeConfirmation(Protocol):
    def confirm(self, request: NativeConfirmationRequest) -> bool: ...


@dataclass(frozen=True)
class PendingEnrollment:
    locator: str
    verification_phrase: str
    expires_at: float


@dataclass(frozen=True)
class PendingClaim:
    locator: str
    verification_phrase: str
    device_label: str
    origin: str
    expires_at: float


@dataclass(frozen=True)
class SessionCredentials:
    session_secret: str
    csrf_secret: str
    expires_at: float
    absolute_expires_at: float
    idle_remaining_seconds: float
    absolute_remaining_seconds: float
    auth_epoch: int
    primary_local: bool


@dataclass(frozen=True)
class SessionContext:
    expires_at: float
    absolute_expires_at: float
    auth_epoch: int
    primary_local: bool


@dataclass(frozen=True)
class SessionResumeCredentials:
    csrf_secret: str
    idle_remaining_seconds: float
    absolute_remaining_seconds: float
    auth_epoch: int
    primary_local: bool


@dataclass(frozen=True)
class SensitiveChallenge:
    challenge_id: str
    verification_phrase: str
    preview: str
    approved_at: float | None


@dataclass(frozen=True)
class SensitiveGrant:
    grant_secret: str
    expires_at: float


@dataclass
class _PreauthState:
    origin: str
    expires_at: float
    auth_epoch: int
    locator: str | None = None


@dataclass
class _PendingState:
    source: str
    device_label: str
    verification_phrase: str
    created_at: float
    expires_at: float
    auth_epoch: int
    primary_local: bool
    state: str = "OPEN"
    preauth_digest: bytes | None = None
    origin: str | None = None


@dataclass
class _SessionState:
    csrf_digest: bytes
    origin: str
    created_at: float
    last_user_activity_at: float
    absolute_expires_at: float
    auth_epoch: int
    primary_local: bool

    def expires_at(self) -> float:
        return min(
            self.last_user_activity_at + SESSION_IDLE_TTL_SECONDS,
            self.absolute_expires_at,
        )


@dataclass
class _SensitiveChallengeState:
    session_digest: bytes
    origin: str
    preview: str
    preview_digest: bytes
    verification_phrase: str
    auth_epoch: int
    approved_at: float | None = None
    grant_minted: bool = False


@dataclass
class _SensitiveGrantState:
    session_digest: bytes
    origin: str
    preview_digest: bytes
    expires_at: float
    auth_epoch: int


class OwnerAuthState:
    """In-memory one-human-Owner authentication state independent of memory storage.

    Browser/session secrets are returned only to the transport caller. Internal state
    retains SHA-256 digests, exact origin bindings and timestamps. Native approval is
    injected as a typed Python interface; this module never executes a shell command.
    """

    _PHRASE_WORDS = (
        "amber",
        "anchor",
        "birch",
        "canyon",
        "cedar",
        "cobalt",
        "coral",
        "delta",
        "ember",
        "falcon",
        "fern",
        "harbor",
        "indigo",
        "juniper",
        "lantern",
        "maple",
        "meadow",
        "mercury",
        "orchid",
        "pebble",
        "quartz",
        "raven",
        "river",
        "saffron",
        "silver",
        "spruce",
        "summit",
        "tiger",
        "umber",
        "violet",
        "willow",
        "zephyr",
    )

    def __init__(
        self,
        *,
        owner_uid: int,
        native_confirmation: NativeConfirmation,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
            raise ValueError("owner_uid must be a non-negative integer")
        self._owner_uid = owner_uid
        self._native_confirmation = native_confirmation
        self._clock = clock
        self._lock = threading.RLock()
        self._auth_epoch = 1
        self._preauth: dict[bytes, _PreauthState] = {}
        self._pending: dict[str, _PendingState] = {}
        self._sessions: dict[bytes, _SessionState] = {}
        self._challenges: dict[str, _SensitiveChallengeState] = {}
        self._grants: dict[bytes, _SensitiveGrantState] = {}
        self._source_attempts: dict[str, deque[float]] = defaultdict(deque)

    @property
    def auth_epoch(self) -> int:
        with self._lock:
            return self._auth_epoch

    def begin_enrollment(
        self,
        *,
        caller_uid: int,
        source: str,
        device_label: str,
    ) -> PendingEnrollment:
        self._require_owner_uid(caller_uid)
        source = self._bounded_text(source, "source", 512)
        device_label = self._bounded_text(device_label, "device_label", 256)
        now = self._clock()
        with self._lock:
            self._reserve_enrollment_slot(source, now)
            locator = self._new_locator()
            while locator in self._pending:
                locator = self._new_locator()
            phrase = self._new_verification_phrase()
            expires_at = now + PENDING_TTL_SECONDS
            self._pending[locator] = _PendingState(
                source=source,
                device_label=device_label,
                verification_phrase=phrase,
                created_at=now,
                expires_at=expires_at,
                auth_epoch=self._auth_epoch,
                primary_local=True,
            )
            return PendingEnrollment(
                locator=locator,
                verification_phrase=phrase,
                expires_at=expires_at,
            )

    def begin_remote_enrollment(
        self,
        *,
        source: str,
        device_label: str,
        origin: str,
        preauth_secret: str,
    ) -> PendingClaim:
        source = self._bounded_text(source, "source", 512)
        device_label = self._bounded_text(device_label, "device_label", 256)
        origin = self._validate_origin(origin)
        preauth_digest = self._secret_digest(preauth_secret)
        now = self._clock()
        with self._lock:
            self._cleanup_expired_preauth(now)
            preauth = self._preauth.get(preauth_digest)
            if (
                preauth is None
                or preauth.auth_epoch != self._auth_epoch
                or now >= preauth.expires_at
                or preauth.origin != origin
                or preauth.locator is not None
            ):
                raise AuthError("PREAUTH_INVALID", "The browser pre-authentication state is invalid.")
            self._reserve_enrollment_slot(source, now)
            locator = self._new_locator()
            while locator in self._pending:
                locator = self._new_locator()
            phrase = self._new_verification_phrase()
            expires_at = now + PENDING_TTL_SECONDS
            pending = _PendingState(
                source=source,
                device_label=device_label,
                verification_phrase=phrase,
                created_at=now,
                expires_at=expires_at,
                auth_epoch=self._auth_epoch,
                primary_local=False,
                state="CLAIMED",
                preauth_digest=preauth_digest,
                origin=origin,
            )
            self._pending[locator] = pending
            preauth.locator = locator
            return self._public_claim(locator, pending)

    def issue_preauth(self, *, origin: str) -> str:
        origin = self._validate_origin(origin)
        now = self._clock()
        secret = self._new_secret()
        digest = self._secret_digest(secret)
        with self._lock:
            self._cleanup_expired_preauth(now)
            self._preauth[digest] = _PreauthState(
                origin=origin,
                expires_at=now + PENDING_TTL_SECONDS,
                auth_epoch=self._auth_epoch,
            )
        return secret

    def claim_pending(
        self,
        *,
        locator: str,
        origin: str,
        preauth_secret: str,
    ) -> PendingClaim:
        origin = self._validate_origin(origin)
        digest = self._secret_digest(preauth_secret)
        now = self._clock()
        with self._lock:
            pending = self._pending_for_use(locator, now)

            if pending.state in {"CLAIMED", "APPROVED"}:
                if pending.preauth_digest == digest and pending.origin == origin:
                    return self._public_claim(locator, pending)
                self._invalidate_pending(locator, pending, conflicting_digest=digest)
                raise AuthError(
                    "PENDING_CLAIM_CONFLICT",
                    "A different browser claim invalidated this enrollment attempt.",
                )
            if pending.state != "OPEN":
                raise AuthError("PENDING_INVALID", "The enrollment request is no longer valid.")

            preauth = self._preauth.get(digest)
            if (
                preauth is None
                or preauth.auth_epoch != self._auth_epoch
                or now >= preauth.expires_at
                or preauth.origin != origin
            ):
                raise AuthError("PREAUTH_INVALID", "The browser pre-authentication state is invalid.")
            if preauth.locator is not None and preauth.locator != locator:
                self._invalidate_pending(locator, pending, conflicting_digest=digest)
                raise AuthError(
                    "PENDING_CLAIM_CONFLICT",
                    "The browser pre-authentication state is already bound to another request.",
                )

            pending.state = "CLAIMED"
            pending.preauth_digest = digest
            pending.origin = origin
            preauth.locator = locator
            return self._public_claim(locator, pending)

    def approve_pending(self, *, locator: str, caller_uid: int) -> PendingClaim:
        self._require_owner_uid(caller_uid)
        now = self._clock()
        with self._lock:
            pending = self._pending_for_use(locator, now)
            if not pending.primary_local:
                raise AuthError(
                    "REMOTE_APPROVAL_REQUIRES_PRIMARY_SESSION",
                    "Remote enrollment requires approval from an authenticated primary local session.",
                )
            if pending.state == "APPROVED":
                return self._public_claim(locator, pending)
            if pending.state == "OPEN":
                raise AuthError(
                    "PENDING_NOT_CLAIMED",
                    "The browser has not claimed this enrollment request yet.",
                    retryable=True,
                )
            if pending.state != "CLAIMED" or pending.origin is None or pending.preauth_digest is None:
                raise AuthError("PENDING_INVALID", "The enrollment request is not ready for approval.")
            epoch = self._auth_epoch
            request = NativeConfirmationRequest(
                kind="ENROLLMENT",
                verification_phrase=pending.verification_phrase,
                auth_epoch=epoch,
                caller_uid=caller_uid,
                locator=locator,
                source=pending.source,
                origin=pending.origin,
                device_label=pending.device_label,
            )

        approved = bool(self._native_confirmation.confirm(request))
        with self._lock:
            current = self._pending_for_use(locator, self._clock())
            if current.auth_epoch != epoch or self._auth_epoch != epoch or current.state != "CLAIMED":
                raise AuthError("PENDING_INVALID", "The enrollment request changed during confirmation.")
            if not approved:
                self._invalidate_pending(locator, current)
                raise AuthError("NATIVE_CONFIRMATION_DENIED", "The Owner denied the enrollment request.")
            current.state = "APPROVED"
            return self._public_claim(locator, current)

    def poll_pending(
        self,
        *,
        locator: str,
        origin: str,
        preauth_secret: str,
    ) -> SessionCredentials | None:
        origin = self._validate_origin(origin)
        digest = self._secret_digest(preauth_secret)
        now = self._clock()
        with self._lock:
            pending = self._pending_for_use(locator, now)
            if pending.preauth_digest != digest or pending.origin != origin:
                raise AuthError(
                    "PENDING_BINDING_MISMATCH",
                    "The enrollment request is bound to another browser or origin.",
                )
            preauth = self._preauth.get(digest)
            if (
                preauth is None
                or preauth.auth_epoch != self._auth_epoch
                or now >= preauth.expires_at
                or preauth.origin != origin
                or preauth.locator != locator
            ):
                raise AuthError("PREAUTH_INVALID", "The browser pre-authentication state is invalid.")
            if pending.state == "CLAIMED":
                return None
            if pending.state != "APPROVED":
                raise AuthError("PENDING_INVALID", "The enrollment request is no longer valid.")

            self._cleanup_expired_sessions(now)
            if len(self._sessions) >= MAX_LIVE_SESSIONS:
                raise AuthError(
                    "SESSION_LIMIT",
                    "The maximum number of live Owner sessions has been reached.",
                    retryable=True,
                )

            session_secret = self._new_secret()
            session_digest = self._secret_digest(session_secret)
            while session_digest in self._sessions:
                session_secret = self._new_secret()
                session_digest = self._secret_digest(session_secret)
            csrf_secret = self._new_secret()
            session = _SessionState(
                csrf_digest=self._secret_digest(csrf_secret),
                origin=origin,
                created_at=now,
                last_user_activity_at=now,
                absolute_expires_at=now + SESSION_ABSOLUTE_TTL_SECONDS,
                auth_epoch=self._auth_epoch,
                primary_local=pending.primary_local,
            )
            self._sessions[session_digest] = session
            self._pending.pop(locator, None)
            self._preauth.pop(digest, None)
            return SessionCredentials(
                session_secret=session_secret,
                csrf_secret=csrf_secret,
                expires_at=session.expires_at(),
                absolute_expires_at=session.absolute_expires_at,
                idle_remaining_seconds=max(0.0, session.expires_at() - now),
                absolute_remaining_seconds=max(0.0, session.absolute_expires_at - now),
                auth_epoch=session.auth_epoch,
                primary_local=session.primary_local,
            )

    def resume_session(
        self,
        *,
        session_secret: str,
        origin: str,
        csrf_secret: str,
    ) -> SessionResumeCredentials:
        origin = self._validate_origin(origin)
        digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            session = self._session_for_use(digest, origin, now)
            self._validate_csrf(session, csrf_secret, required=True)
            rotated_csrf = self._new_secret()
            session.csrf_digest = self._secret_digest(rotated_csrf)
            return SessionResumeCredentials(
                csrf_secret=rotated_csrf,
                idle_remaining_seconds=max(0.0, session.expires_at() - now),
                absolute_remaining_seconds=max(0.0, session.absolute_expires_at - now),
                auth_epoch=session.auth_epoch,
                primary_local=session.primary_local,
            )

    def validate_session(
        self,
        *,
        session_secret: str,
        origin: str,
        csrf_secret: str | None = None,
        require_csrf: bool = False,
        user_activity: bool = False,
    ) -> SessionContext:
        origin = self._validate_origin(origin)
        digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            session = self._session_for_use(digest, origin, now)
            self._validate_csrf(session, csrf_secret, required=require_csrf)
            if user_activity:
                session.last_user_activity_at = now
            return SessionContext(
                expires_at=session.expires_at(),
                absolute_expires_at=session.absolute_expires_at,
                auth_epoch=session.auth_epoch,
                primary_local=session.primary_local,
            )

    def remote_enrollment_preview(
        self,
        *,
        locator: str,
        session_secret: str,
        origin: str,
    ) -> str:
        origin = self._validate_origin(origin)
        session_digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            session = self._session_for_use(session_digest, origin, now)
            if not session.primary_local:
                raise AuthError(
                    "PRIMARY_LOCAL_SESSION_REQUIRED",
                    "Remote enrollment approval requires a primary local Owner session.",
                )
            pending = self._pending_for_use(locator, now)
            if pending.primary_local:
                raise AuthError("REMOTE_PENDING_REQUIRED", "The pending request is not a remote enrollment.")
            if pending.state != "CLAIMED":
                raise AuthError("PENDING_INVALID", "The remote enrollment is not awaiting approval.")
            session.last_user_activity_at = now
            return self._canonical_remote_enrollment_preview(locator, pending)

    def approve_remote_enrollment(
        self,
        *,
        locator: str,
        session_secret: str,
        origin: str,
        csrf_secret: str,
        grant_secret: str,
    ) -> PendingClaim:
        origin = self._validate_origin(origin)
        session_digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            session = self._session_for_use(session_digest, origin, now)
            self._validate_csrf(session, csrf_secret, required=True)
            if not session.primary_local:
                raise AuthError(
                    "PRIMARY_LOCAL_SESSION_REQUIRED",
                    "Remote enrollment approval requires a primary local Owner session.",
                )
            pending = self._pending_for_use(locator, now)
            if pending.primary_local:
                raise AuthError("REMOTE_PENDING_REQUIRED", "The pending request is not a remote enrollment.")
            if pending.state != "CLAIMED":
                raise AuthError("PENDING_INVALID", "The remote enrollment is not awaiting approval.")
            preview = self._canonical_remote_enrollment_preview(locator, pending)
            self._consume_sensitive_grant_locked(
                grant_secret=grant_secret,
                session_digest=session_digest,
                origin=origin,
                preview=preview,
                now=now,
            )
            session.last_user_activity_at = now
            pending.state = "APPROVED"
            return self._public_claim(locator, pending)

    def logout(self, session_secret: str) -> None:
        digest = self._secret_digest(session_secret)
        with self._lock:
            self._sessions.pop(digest, None)
            self._drop_session_sensitive_state(digest)

    def begin_sensitive_challenge(
        self,
        *,
        session_secret: str,
        origin: str,
        preview: str,
    ) -> SensitiveChallenge:
        origin = self._validate_origin(origin)
        preview = self._bounded_text(preview, "preview", 16_384)
        session_digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            session = self._session_for_use(session_digest, origin, now)
            session.last_user_activity_at = now
            preview_digest = self._preview_digest(preview)
            matching_id: str | None = None
            matching: _SensitiveChallengeState | None = None
            same_session_unapproved: list[str] = []
            for existing_id, existing in self._challenges.items():
                if (
                    existing.auth_epoch == self._auth_epoch
                    and existing.session_digest == session_digest
                    and existing.approved_at is None
                ):
                    same_session_unapproved.append(existing_id)
                    if (
                        matching is None
                        and existing.origin == origin
                        and existing.preview == preview
                        and hmac.compare_digest(existing.preview_digest, preview_digest)
                    ):
                        matching_id = existing_id
                        matching = existing

            if matching_id is not None and matching is not None:
                for existing_id in same_session_unapproved:
                    if existing_id != matching_id:
                        self._challenges.pop(existing_id, None)
                return SensitiveChallenge(
                    challenge_id=matching_id,
                    verification_phrase=matching.verification_phrase,
                    preview=matching.preview,
                    approved_at=None,
                )

            for existing_id in same_session_unapproved:
                self._challenges.pop(existing_id, None)
            challenge_id = self._new_locator()
            while challenge_id in self._challenges:
                challenge_id = self._new_locator()
            phrase = self._new_verification_phrase()
            self._challenges[challenge_id] = _SensitiveChallengeState(
                session_digest=session_digest,
                origin=origin,
                preview=preview,
                preview_digest=preview_digest,
                verification_phrase=phrase,
                auth_epoch=self._auth_epoch,
            )
            return SensitiveChallenge(
                challenge_id=challenge_id,
                verification_phrase=phrase,
                preview=preview,
                approved_at=None,
            )

    def approve_single_pending_sensitive_challenge(
        self,
        *,
        caller_uid: int,
    ) -> SensitiveChallenge:
        """Native-only approval for exactly one live browser-bound challenge.

        The caller deliberately supplies no challenge identifier. Selection is made
        while holding the auth-state lock, and the selected challenge is snapshotted
        before the native dialog is shown so a later challenge cannot retarget the
        approval.
        """

        self._require_owner_uid(caller_uid)
        now = self._clock()
        with self._lock:
            self._cleanup_expired_sessions(now)
            candidates: list[tuple[str, _SensitiveChallengeState]] = []
            for challenge_id, challenge in self._challenges.items():
                if challenge.auth_epoch != self._auth_epoch or challenge.approved_at is not None:
                    continue
                session = self._sessions.get(challenge.session_digest)
                if (
                    session is None
                    or session.auth_epoch != self._auth_epoch
                    or session.origin != challenge.origin
                    or now >= session.expires_at()
                ):
                    continue
                candidates.append((challenge_id, challenge))

            if not candidates:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_NONE",
                    "No live sensitive-action challenge is awaiting native approval.",
                )
            if len(candidates) != 1:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_AMBIGUOUS",
                    "More than one sensitive-action challenge is awaiting native approval.",
                )

            challenge_id, challenge = candidates[0]
            epoch = self._auth_epoch
            snapshot = (
                challenge.session_digest,
                challenge.origin,
                challenge.preview,
                challenge.preview_digest,
                challenge.verification_phrase,
                challenge.auth_epoch,
            )
            request = NativeConfirmationRequest(
                kind="SENSITIVE_ACTION",
                verification_phrase=challenge.verification_phrase,
                auth_epoch=epoch,
                caller_uid=caller_uid,
                origin=challenge.origin,
                preview=challenge.preview,
            )

        approved = bool(self._native_confirmation.confirm(request))
        approved_at = self._clock()
        with self._lock:
            current = self._challenges.get(challenge_id)
            if current is None:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_INVALID",
                    "The sensitive-action challenge changed during confirmation.",
                )
            current_snapshot = (
                current.session_digest,
                current.origin,
                current.preview,
                current.preview_digest,
                current.verification_phrase,
                current.auth_epoch,
            )
            if (
                current_snapshot != snapshot
                or current.auth_epoch != epoch
                or self._auth_epoch != epoch
                or current.approved_at is not None
            ):
                raise AuthError(
                    "SENSITIVE_CHALLENGE_INVALID",
                    "The sensitive-action challenge changed during confirmation.",
                )
            self._session_for_use(current.session_digest, current.origin, approved_at)
            if not approved:
                self._challenges.pop(challenge_id, None)
                raise AuthError(
                    "NATIVE_CONFIRMATION_DENIED",
                    "The Owner denied the sensitive action.",
                )
            current.approved_at = approved_at
            return SensitiveChallenge(
                challenge_id=challenge_id,
                verification_phrase=current.verification_phrase,
                preview=current.preview,
                approved_at=approved_at,
            )

    def approve_sensitive_challenge(
        self,
        *,
        challenge_id: str,
        caller_uid: int,
    ) -> SensitiveChallenge:
        self._require_owner_uid(caller_uid)
        now = self._clock()
        with self._lock:
            challenge = self._challenge_for_use(challenge_id)
            if challenge.approved_at is not None:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_ALREADY_APPROVED",
                    "The sensitive-action challenge is already approved.",
                )
            self._session_for_use(challenge.session_digest, challenge.origin, now)
            epoch = self._auth_epoch
            request = NativeConfirmationRequest(
                kind="SENSITIVE_ACTION",
                verification_phrase=challenge.verification_phrase,
                auth_epoch=epoch,
                caller_uid=caller_uid,
                origin=challenge.origin,
                preview=challenge.preview,
            )

        approved = bool(self._native_confirmation.confirm(request))
        approved_at = self._clock()
        with self._lock:
            current = self._challenge_for_use(challenge_id)
            if current.auth_epoch != epoch or self._auth_epoch != epoch or current.approved_at is not None:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_INVALID",
                    "The sensitive-action challenge changed during confirmation.",
                )
            self._session_for_use(current.session_digest, current.origin, approved_at)
            if not approved:
                self._challenges.pop(challenge_id, None)
                raise AuthError(
                    "NATIVE_CONFIRMATION_DENIED",
                    "The Owner denied the sensitive action.",
                )
            current.approved_at = approved_at
            return SensitiveChallenge(
                challenge_id=challenge_id,
                verification_phrase=current.verification_phrase,
                preview=current.preview,
                approved_at=approved_at,
            )

    def mint_sensitive_grant(
        self,
        *,
        challenge_id: str,
        session_secret: str,
        origin: str,
    ) -> SensitiveGrant:
        origin = self._validate_origin(origin)
        session_digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            challenge = self._challenge_for_use(challenge_id)
            if challenge.session_digest != session_digest or challenge.origin != origin:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_BINDING_MISMATCH",
                    "The challenge is bound to another Owner session or origin.",
                )
            session = self._session_for_use(session_digest, origin, now)
            session.last_user_activity_at = now
            if challenge.approved_at is None:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_UNAPPROVED",
                    "The sensitive-action challenge has not been approved.",
                )
            challenge_expires_at = challenge.approved_at + SENSITIVE_CHALLENGE_FRESH_SECONDS
            if now >= challenge_expires_at:
                self._challenges.pop(challenge_id, None)
                raise AuthError(
                    "SENSITIVE_CHALLENGE_EXPIRED",
                    "The sensitive-action confirmation is no longer fresh.",
                )
            if challenge.grant_minted:
                raise AuthError(
                    "SENSITIVE_CHALLENGE_CONSUMED",
                    "The sensitive-action challenge has already minted its grant.",
                )

            expires_at = min(challenge_expires_at, session.expires_at())
            grant_secret = self._new_secret()
            grant_digest = self._secret_digest(grant_secret)
            while grant_digest in self._grants:
                grant_secret = self._new_secret()
                grant_digest = self._secret_digest(grant_secret)
            self._grants[grant_digest] = _SensitiveGrantState(
                session_digest=session_digest,
                origin=origin,
                preview_digest=challenge.preview_digest,
                expires_at=expires_at,
                auth_epoch=self._auth_epoch,
            )
            challenge.grant_minted = True
            return SensitiveGrant(grant_secret=grant_secret, expires_at=expires_at)

    def consume_sensitive_grant(
        self,
        *,
        grant_secret: str,
        session_secret: str,
        origin: str,
        preview: str,
    ) -> None:
        origin = self._validate_origin(origin)
        preview = self._bounded_text(preview, "preview", 16_384)
        session_digest = self._secret_digest(session_secret)
        now = self._clock()
        with self._lock:
            self._consume_sensitive_grant_locked(
                grant_secret=grant_secret,
                session_digest=session_digest,
                origin=origin,
                preview=preview,
                now=now,
            )

    def recover_credentials(self, *, caller_uid: int) -> int:
        self._require_owner_uid(caller_uid)
        with self._lock:
            epoch = self._auth_epoch
            phrase = self._new_verification_phrase()
            request = NativeConfirmationRequest(
                kind="CREDENTIAL_RECOVERY",
                verification_phrase=phrase,
                auth_epoch=epoch,
                caller_uid=caller_uid,
            )

        approved = bool(self._native_confirmation.confirm(request))
        with self._lock:
            if not approved:
                raise AuthError(
                    "NATIVE_CONFIRMATION_DENIED",
                    "The Owner denied credential recovery.",
                )
            return self._invalidate_all_auth_state_locked()

    def invalidate_all_sessions_after_maintenance(self) -> int:
        """Trusted internal lifecycle invalidation after maintenance completion.

        This is intentionally not a browser or native-approval surface. The caller
        cannot choose an epoch; one invocation advances the current epoch exactly
        once and revokes every browser authentication artifact atomically.
        """

        with self._lock:
            return self._invalidate_all_auth_state_locked()

    def _invalidate_all_auth_state_locked(self) -> int:
        self._auth_epoch += 1
        self._preauth.clear()
        self._pending.clear()
        self._sessions.clear()
        self._challenges.clear()
        self._grants.clear()
        return self._auth_epoch

    def _pending_for_use(self, locator: str, now: float) -> _PendingState:
        pending = self._pending.get(locator)
        if pending is None or pending.auth_epoch != self._auth_epoch:
            raise AuthError("PENDING_INVALID", "The enrollment request is invalid.")
        if now >= pending.expires_at:
            self._invalidate_pending(locator, pending)
            raise AuthError("PENDING_EXPIRED", "The enrollment request has expired.")
        if pending.state == "INVALIDATED":
            raise AuthError("PENDING_INVALID", "The enrollment request is invalid.")
        return pending

    def _challenge_for_use(self, challenge_id: str) -> _SensitiveChallengeState:
        challenge = self._challenges.get(challenge_id)
        if challenge is None or challenge.auth_epoch != self._auth_epoch:
            raise AuthError(
                "SENSITIVE_CHALLENGE_INVALID",
                "The sensitive-action challenge is invalid.",
            )
        return challenge

    def _session_for_use(self, digest: bytes, origin: str, now: float) -> _SessionState:
        session = self._sessions.get(digest)
        if session is None or session.auth_epoch != self._auth_epoch:
            raise AuthError("SESSION_INVALID", "The Owner session is invalid.")
        if session.origin != origin:
            raise AuthError("SESSION_ORIGIN_MISMATCH", "The Owner session is bound to another origin.")
        if now >= session.expires_at():
            self._sessions.pop(digest, None)
            self._drop_session_sensitive_state(digest)
            raise AuthError("SESSION_EXPIRED", "The Owner session has expired.")
        return session

    def _consume_sensitive_grant_locked(
        self,
        *,
        grant_secret: str,
        session_digest: bytes,
        origin: str,
        preview: str,
        now: float,
    ) -> None:
        self._session_for_use(session_digest, origin, now)
        grant_digest = self._secret_digest(grant_secret)
        grant = self._grants.get(grant_digest)
        if grant is None or grant.auth_epoch != self._auth_epoch:
            raise AuthError("SENSITIVE_GRANT_INVALID", "The sensitive-action grant is invalid.")
        if grant.session_digest != session_digest or grant.origin != origin:
            raise AuthError(
                "SENSITIVE_GRANT_BINDING_MISMATCH",
                "The sensitive-action grant is bound to another Owner session or origin.",
            )
        if now >= grant.expires_at:
            self._grants.pop(grant_digest, None)
            raise AuthError(
                "SENSITIVE_GRANT_EXPIRED",
                "The sensitive-action grant has expired.",
            )
        if not hmac.compare_digest(grant.preview_digest, self._preview_digest(preview)):
            raise AuthError(
                "SENSITIVE_PREVIEW_MISMATCH",
                "The sensitive-action grant does not match the exact current preview.",
            )
        self._grants.pop(grant_digest, None)

    def _validate_csrf(
        self,
        session: _SessionState,
        csrf_secret: str | None,
        *,
        required: bool,
    ) -> None:
        if required and csrf_secret is None:
            raise AuthError("CSRF_INVALID", "CSRF protection is required for this request.")
        if csrf_secret is not None and not hmac.compare_digest(
            session.csrf_digest,
            self._secret_digest(csrf_secret),
        ):
            raise AuthError("CSRF_INVALID", "The CSRF secret is invalid.")

    def _drop_session_sensitive_state(self, session_digest: bytes) -> None:
        for challenge_id in [
            challenge_id
            for challenge_id, challenge in self._challenges.items()
            if challenge.session_digest == session_digest
        ]:
            self._challenges.pop(challenge_id, None)
        for grant_digest in [
            grant_digest
            for grant_digest, grant in self._grants.items()
            if grant.session_digest == session_digest
        ]:
            self._grants.pop(grant_digest, None)

    def _invalidate_pending(
        self,
        locator: str,
        pending: _PendingState,
        *,
        conflicting_digest: bytes | None = None,
    ) -> None:
        pending.state = "INVALIDATED"
        if pending.preauth_digest is not None:
            self._preauth.pop(pending.preauth_digest, None)
        if conflicting_digest is not None:
            self._preauth.pop(conflicting_digest, None)

    def _cleanup_expired_pending(self, now: float) -> None:
        for locator, pending in list(self._pending.items()):
            if (
                pending.auth_epoch != self._auth_epoch
                or pending.state == "INVALIDATED"
                or now >= pending.expires_at
            ):
                if pending.preauth_digest is not None:
                    self._preauth.pop(pending.preauth_digest, None)
                self._pending.pop(locator, None)
        self._cleanup_expired_preauth(now)

    def _cleanup_expired_preauth(self, now: float) -> None:
        for digest, preauth in list(self._preauth.items()):
            if preauth.auth_epoch != self._auth_epoch or now >= preauth.expires_at:
                self._preauth.pop(digest, None)

    def _cleanup_expired_sessions(self, now: float) -> None:
        for digest, session in list(self._sessions.items()):
            if session.auth_epoch != self._auth_epoch or now >= session.expires_at():
                self._sessions.pop(digest, None)
                self._drop_session_sensitive_state(digest)

    def _cleanup_rate_windows(self, now: float) -> None:
        for source, attempts in list(self._source_attempts.items()):
            while attempts and now - attempts[0] >= SOURCE_RATE_WINDOW_SECONDS:
                attempts.popleft()
            if not attempts:
                self._source_attempts.pop(source, None)

    def _reserve_enrollment_slot(self, source: str, now: float) -> None:
        self._cleanup_expired_pending(now)
        self._cleanup_rate_windows(now)
        attempts = self._source_attempts[source]
        if len(attempts) >= MAX_PENDING_PER_SOURCE_PER_MINUTE:
            raise AuthError(
                "SOURCE_RATE_LIMIT",
                "Too many enrollment attempts from this source.",
                retryable=True,
            )
        if self._live_pending_count(now) >= MAX_PENDING_TOTAL:
            raise AuthError(
                "PENDING_LIMIT",
                "Too many enrollment requests are pending.",
                retryable=True,
            )
        attempts.append(now)

    def _live_pending_count(self, now: float) -> int:
        return sum(
            1
            for pending in self._pending.values()
            if pending.auth_epoch == self._auth_epoch
            and pending.state in {"OPEN", "CLAIMED", "APPROVED"}
            and now < pending.expires_at
        )

    def _public_claim(self, locator: str, pending: _PendingState) -> PendingClaim:
        assert pending.origin is not None
        return PendingClaim(
            locator=locator,
            verification_phrase=pending.verification_phrase,
            device_label=pending.device_label,
            origin=pending.origin,
            expires_at=pending.expires_at,
        )

    @staticmethod
    def _canonical_remote_enrollment_preview(locator: str, pending: _PendingState) -> str:
        assert pending.origin is not None
        return json.dumps(
            {
                "device_label": pending.device_label,
                "locator": locator,
                "operation": "APPROVE_REMOTE_ENROLLMENT",
                "origin": pending.origin,
                "source": pending.source,
                "verification_phrase": pending.verification_phrase,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _require_owner_uid(self, caller_uid: int) -> None:
        if isinstance(caller_uid, bool) or not isinstance(caller_uid, int) or caller_uid != self._owner_uid:
            raise AuthError(
                "CALLER_UID_MISMATCH",
                "The native caller UID does not match the configured Owner UID.",
            )

    @staticmethod
    def _bounded_text(value: str, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"{field} must be a non-empty string of at most {maximum} characters")
        return value

    @classmethod
    def _validate_origin(cls, origin: str) -> str:
        origin = cls._bounded_text(origin, "origin", 2048)
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("origin must contain a valid port when a port is present") from exc
        common_invalid = (
            not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        )
        local_http = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and port is not None
            and 8765 <= port <= 8774
            and origin == f"http://127.0.0.1:{port}"
        )
        exact_https = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and origin
            == f"https://{parsed.hostname}" + (f":{port}" if port is not None else "")
        )
        if common_invalid or not (local_http or exact_https):
            raise ValueError(
                "origin must be exact HTTPS or the canonical FactLane loopback HTTP origin"
            )
        return origin

    @staticmethod
    def _new_secret() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _new_locator() -> str:
        return secrets.token_urlsafe(18)

    @classmethod
    def _new_verification_phrase(cls) -> str:
        return " ".join(secrets.choice(cls._PHRASE_WORDS) for _ in range(5))

    @staticmethod
    def _secret_digest(secret: str) -> bytes:
        if not isinstance(secret, str) or not secret:
            return hashlib.sha256(b"<invalid-empty-secret>").digest()
        return hashlib.sha256(secret.encode("utf-8")).digest()

    @staticmethod
    def _preview_digest(preview: str) -> bytes:
        return hashlib.sha256(preview.encode("utf-8")).digest()
