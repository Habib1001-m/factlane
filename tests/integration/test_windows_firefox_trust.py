from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import factlane.windows_firefox_trust as windows_firefox_trust
from factlane.windows_firefox_trust import (
    WindowsFirefoxTrustError,
    ensure_firefox_profile_network_policy,
    ensure_firefox_profile_trust,
)
from factlane.windows_tls import build_windows_server_ssl_context, ensure_windows_tls_material


NATIVE_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="native Windows Firefox trust qualification only")


def test_firefox_trust_imports_off_platform_and_fails_typed(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("off-platform import contract is exercised on non-Windows hosts")
    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=tmp_path / "firefox.exe",
            profile_dir=tmp_path / "profile",
            tls_material=object(),  # type: ignore[arg-type]
        )
    assert caught.value.code == "WINDOWS_FIREFOX_TRUST_UNAVAILABLE"


def _firefox_executable() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Mozilla Firefox" / "firefox.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Mozilla Firefox" / "firefox.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Mozilla Firefox" / "firefox.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("native Firefox installation unavailable")


def _private_profile(path: Path) -> Path:
    from factlane.windows_security import current_user_sid, ensure_owner_private_directory

    ensure_owner_private_directory(path, owner_sid=current_user_sid())
    return path


def _private_file(path: Path, data: bytes) -> None:
    from factlane.windows_security import current_user_sid, protect_owner_private_path

    path.write_bytes(data)
    protect_owner_private_path(path, owner_sid=current_user_sid(), directory=False)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trust_db_hashes(profile: Path) -> dict[str, str]:
    names = ("cert9.db", "key4.db", "pkcs11.txt", ".factlane-firefox-trust.json")
    return {name: _sha256(profile / name) for name in names}


@NATIVE_WINDOWS
def test_firefox_profile_imports_only_factlane_root_and_preserves_unrelated_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from factlane.windows_security import current_user_sid, validate_owner_private_path

    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    sentinel = profile / "owner-note.bin"
    _private_file(sentinel, b"unrelated-profile-state")
    sentinel_before = (sentinel.read_bytes(), sentinel.stat().st_mtime_ns)
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-import.test")
    os_roots_before = tuple(sorted(hashlib.sha256(cert).hexdigest() for cert, _encoding, _trust in ssl.enum_certificates("ROOT")))
    global_profiles = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox" / "profiles.ini"
    global_profiles_before = global_profiles.read_bytes() if global_profiles.is_file() else None
    original_load_nss = windows_firefox_trust._load_nss
    db_precreated_owner_private = False

    def validating_load_nss(path: Path):
        nonlocal db_precreated_owner_private
        sid = current_user_sid()
        for name in ("cert9.db", "key4.db", "pkcs11.txt"):
            db_path = profile / name
            validate_owner_private_path(db_path, owner_sid=sid, directory=False)
            assert db_path.stat().st_size == 0
        db_precreated_owner_private = True
        return original_load_nss(path)

    monkeypatch.setattr(windows_firefox_trust, "_load_nss", validating_load_nss)

    receipt = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=material,
    )

    assert receipt.status == "IMPORTED"
    assert receipt.db_initialized is True
    assert receipt.firefox_executable == firefox
    assert receipt.nss_dll_path == firefox.parent / "nss3.dll"
    assert receipt.root_fingerprint_sha256 == material.root_fingerprint_sha256
    assert receipt.ssl_trust_flags & 0x18 == 0x18
    assert receipt.ssl_trust_flags & ~0x38 == 0
    assert db_precreated_owner_private is True
    assert sentinel.read_bytes() == sentinel_before[0]
    assert sentinel.stat().st_mtime_ns == sentinel_before[1]
    os_roots_after = tuple(sorted(hashlib.sha256(cert).hexdigest() for cert, _encoding, _trust in ssl.enum_certificates("ROOT")))
    assert os_roots_after == os_roots_before
    assert material.root_fingerprint_sha256 not in os_roots_after
    assert (global_profiles.read_bytes() if global_profiles.is_file() else None) == global_profiles_before
    sid = current_user_sid()
    for name in ("cert9.db", "key4.db", "pkcs11.txt", ".factlane-firefox-trust.json"):
        validate_owner_private_path(profile / name, owner_sid=sid, directory=False)


@NATIVE_WINDOWS
def test_firefox_profile_same_root_reuse_is_idempotent(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-reuse.test")

    first = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=material,
    )
    before = _trust_db_hashes(profile)
    second = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=material,
    )
    after = _trust_db_hashes(profile)

    assert first.status == "IMPORTED"
    assert second.status == "REUSED"
    assert second.db_initialized is False
    assert second.root_fingerprint_sha256 == first.root_fingerprint_sha256
    assert after == before


@NATIVE_WINDOWS
def test_firefox_profile_rebinds_only_manifest_owned_root_on_managed_ca_rotation(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    first_material = ensure_windows_tls_material(tmp_path / "tls-a", "factlane-firefox-root.test")
    second_material = ensure_windows_tls_material(tmp_path / "tls-b", "factlane-firefox-root.test")
    assert first_material.root_fingerprint_sha256 != second_material.root_fingerprint_sha256

    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=first_material,
    )
    before = _trust_db_hashes(profile)
    rebound = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=second_material,
    )
    after = _trust_db_hashes(profile)
    manifest = json.loads((profile / ".factlane-firefox-trust.json").read_text(encoding="utf-8"))

    assert rebound.status == "REBOUND"
    assert rebound.root_fingerprint_sha256 == second_material.root_fingerprint_sha256
    assert manifest["root_fingerprint_sha256"] == second_material.root_fingerprint_sha256
    assert after != before
    reused = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=second_material,
    )
    assert reused.status == "REUSED"
    assert _trust_db_hashes(profile) == after


@NATIVE_WINDOWS
def test_firefox_profile_root_rebind_refuses_unproven_manifest_ownership_without_db_mutation(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    first_material = ensure_windows_tls_material(tmp_path / "tls-a", "factlane-firefox-owned-root.test")
    second_material = ensure_windows_tls_material(tmp_path / "tls-b", "factlane-firefox-owned-root.test")
    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=first_material,
    )
    manifest_path = profile / ".factlane-firefox-trust.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["root_fingerprint_sha256"] = "0" * 64
    _private_file(
        manifest_path,
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    db_before = {name: _sha256(profile / name) for name in ("cert9.db", "key4.db", "pkcs11.txt")}

    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=profile,
            tls_material=second_material,
        )

    assert caught.value.code == "WINDOWS_FIREFOX_TRUST_ROOT_MISMATCH"
    assert {name: _sha256(profile / name) for name in db_before} == db_before


@NATIVE_WINDOWS
def test_firefox_profile_root_rebind_rolls_back_old_trust_if_manifest_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    first_material = ensure_windows_tls_material(tmp_path / "tls-a", "factlane-firefox-rebind-rollback.test")
    second_material = ensure_windows_tls_material(tmp_path / "tls-b", "factlane-firefox-rebind-rollback.test")
    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=first_material,
    )
    original_write_manifest = windows_firefox_trust._write_manifest_atomic
    writes = 0

    def fail_first_rebind_commit(guard, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise WindowsFirefoxTrustError(
                "INJECTED_REBIND_COMMIT_FAILURE",
                "injected managed-root manifest commit failure",
            )
        original_write_manifest(guard, data)

    monkeypatch.setattr(windows_firefox_trust, "_write_manifest_atomic", fail_first_rebind_commit)
    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=profile,
            tls_material=second_material,
        )

    assert caught.value.code == "INJECTED_REBIND_COMMIT_FAILURE"
    assert writes == 2
    manifest = json.loads((profile / ".factlane-firefox-trust.json").read_text(encoding="utf-8"))
    assert manifest["root_fingerprint_sha256"] == first_material.root_fingerprint_sha256
    monkeypatch.setattr(windows_firefox_trust, "_write_manifest_atomic", original_write_manifest)
    restored = ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=first_material,
    )
    assert restored.status == "REUSED"


@NATIVE_WINDOWS
def test_firefox_profile_network_policy_is_owner_private_exact_and_idempotent(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, validate_owner_private_path

    profile = _private_profile(tmp_path / "profile")
    hostname = "factlane-firefox-network.test"
    policy = ensure_firefox_profile_network_policy(profile_dir=profile, hostname=hostname)
    expected = (
        "// FactLane managed Firefox profile network policy v1\n"
        f'user_pref("network.dns.localDomains", "{hostname}");\n'
        'user_pref("network.proxy.type", 0);\n'
        'user_pref("network.trr.mode", 5);\n'
    ).encode("utf-8")
    before = (policy.read_bytes(), policy.stat().st_mtime_ns)

    assert before[0] == expected
    validate_owner_private_path(policy, owner_sid=current_user_sid(), directory=False)
    assert ensure_firefox_profile_network_policy(profile_dir=profile, hostname=hostname) == policy
    assert (policy.read_bytes(), policy.stat().st_mtime_ns) == before


@NATIVE_WINDOWS
def test_firefox_profile_network_policy_rejects_conflicting_unmanaged_user_js(tmp_path: Path) -> None:
    profile = _private_profile(tmp_path / "profile")
    policy = profile / "user.js"
    original = b'user_pref("network.proxy.type", 1);\n'
    _private_file(policy, original)

    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_network_policy(
            profile_dir=profile,
            hostname="factlane-firefox-conflict.test",
        )

    assert caught.value.code == "WINDOWS_FIREFOX_PROFILE_PREFS_CONFLICT"
    assert policy.read_bytes() == original


@NATIVE_WINDOWS
def test_firefox_profile_manifest_rejects_profile_relocation(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    original = _private_profile(tmp_path / "profile-a")
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-profile.test")
    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=original,
        tls_material=material,
    )
    relocated = tmp_path / "profile-b"
    original.rename(relocated)
    before = _trust_db_hashes(relocated)

    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=relocated,
            tls_material=material,
        )

    assert caught.value.code == "WINDOWS_FIREFOX_TRUST_PROFILE_MISMATCH"
    assert _trust_db_hashes(relocated) == before


@NATIVE_WINDOWS
def test_firefox_profile_manifest_rejects_executable_identity_drift_before_nss_mutation(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-executable.test")
    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=material,
    )
    manifest_path = profile / ".factlane-firefox-trust.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["firefox_sha256"] = "0" * 64
    _private_file(
        manifest_path,
        (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
    )
    db_before = {name: _sha256(profile / name) for name in ("cert9.db", "key4.db", "pkcs11.txt")}

    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=profile,
            tls_material=material,
        )

    assert caught.value.code == "WINDOWS_FIREFOX_TRUST_EXECUTABLE_MISMATCH"
    assert {name: _sha256(profile / name) for name in db_before} == db_before


@NATIVE_WINDOWS
def test_firefox_profile_corrupt_sql_db_fails_closed_before_nss_mutation(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    for name, content in (
        ("cert9.db", b"corrupt cert database"),
        ("key4.db", b"corrupt key database"),
        ("pkcs11.txt", b"corrupt module config"),
    ):
        _private_file(profile / name, content)
    before = {path.name: path.read_bytes() for path in profile.iterdir()}
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-corrupt.test")

    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=profile,
            tls_material=material,
        )

    assert caught.value.code == "WINDOWS_FIREFOX_PROFILE_DB_INVALID"
    assert not (profile / ".factlane-firefox-trust.json").exists()
    assert {path.name: path.read_bytes() for path in profile.iterdir()} == before


@NATIVE_WINDOWS
def test_firefox_fresh_profile_rolls_back_owned_db_files_if_nss_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    sentinel = profile / "owner-sentinel.bin"
    _private_file(sentinel, b"preserve-me")
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-rollback.test")

    def injected_nss_failure(_path: Path):
        raise WindowsFirefoxTrustError("INJECTED_NSS_FAILURE", "injected NSS load failure")

    monkeypatch.setattr(windows_firefox_trust, "_load_nss", injected_nss_failure)
    with pytest.raises(WindowsFirefoxTrustError) as caught:
        ensure_firefox_profile_trust(
            firefox_executable=firefox,
            profile_dir=profile,
            tls_material=material,
        )

    assert caught.value.code == "INJECTED_NSS_FAILURE"
    assert sentinel.read_bytes() == b"preserve-me"
    assert sorted(path.name for path in profile.iterdir()) == [sentinel.name]


def _profile_lock_is_held(profile: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    lock_path = profile / "parent.lock"
    if not lock_path.exists():
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(str(lock_path), 0x80000000, 0, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if int(handle) == invalid:
        return ctypes.get_last_error() in {5, 32, 33}
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)
    return False


@NATIVE_WINDOWS
def test_firefox_profile_live_lock_fails_closed_without_trust_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    sentinel = profile / "owner-sentinel.bin"
    _private_file(sentinel, b"must-remain-unchanged")
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-firefox-locked.test")
    process = subprocess.Popen(
        [str(firefox), "-headless", "-no-remote", "-profile", str(profile), "about:blank"],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not _profile_lock_is_held(profile):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        nss_called = False

        def unexpected_nss_load(_path: Path):
            nonlocal nss_called
            nss_called = True
            raise AssertionError("NSS must not load while Firefox holds the profile lock")

        monkeypatch.setattr(windows_firefox_trust, "_load_nss", unexpected_nss_load)

        with pytest.raises(WindowsFirefoxTrustError) as caught:
            ensure_firefox_profile_trust(
                firefox_executable=firefox,
                profile_dir=profile,
                tls_material=material,
            )

        assert caught.value.code == "WINDOWS_FIREFOX_PROFILE_LOCKED"
        assert nss_called is False
        assert not (profile / ".factlane-firefox-trust.json").exists()
        assert sentinel.read_bytes() == b"must-remain-unchanged"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _serve_browser_tls(
    listener: socket.socket,
    context: ssl.SSLContext,
    received: threading.Event,
    errors: list[BaseException],
) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not received.is_set():
        try:
            connection, _address = listener.accept()
        except socket.timeout:
            continue
        except OSError as exc:
            if not received.is_set():
                errors.append(exc)
            return
        try:
            with connection:
                connection.settimeout(3)
                with context.wrap_socket(connection, server_side=True) as tls_socket:
                    request = tls_socket.recv(8192)
                    if request.startswith(b"GET "):
                        body = b"factlane-firefox-trust-ok"
                        tls_socket.sendall(
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/plain\r\n"
                            + f"Content-Length: {len(body)}\r\n".encode("ascii")
                            + b"Connection: close\r\n\r\n"
                            + body
                        )
                        received.set()
        except (TimeoutError, socket.timeout):
            continue
        except (OSError, ssl.SSLError) as exc:
            errors.append(exc)


@NATIVE_WINDOWS
def test_firefox_headless_uses_profile_root_for_real_https_without_bypass_flags(tmp_path: Path) -> None:
    firefox = _firefox_executable()
    profile = _private_profile(tmp_path / "profile")
    hostname = "factlane-firefox-browser.test"
    material = ensure_windows_tls_material(tmp_path / "tls", hostname)
    ensure_firefox_profile_trust(
        firefox_executable=firefox,
        profile_dir=profile,
        tls_material=material,
    )
    ensure_firefox_profile_network_policy(profile_dir=profile, hostname=hostname)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    port = int(listener.getsockname()[1])
    received = threading.Event()
    errors: list[BaseException] = []
    thread = threading.Thread(
        target=_serve_browser_tls,
        args=(listener, build_windows_server_ssl_context(material), received, errors),
        daemon=True,
    )
    thread.start()
    screenshot = tmp_path / "firefox-proof.png"
    argv = [
        str(firefox),
        "-headless",
        "-no-remote",
        "-profile",
        str(profile),
        "--screenshot",
        str(screenshot),
        f"https://{hostname}:{port}/proof",
    ]
    lowered = " ".join(argv).casefold()
    assert "accept-insecure" not in lowered
    assert "ignore-certificate" not in lowered
    assert "insecure-cert" not in lowered
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            timeout=25,
            check=False,
        )
    finally:
        thread.join(timeout=3)
        listener.close()

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert received.is_set(), (
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
        errors,
    )
    assert errors == []
    assert screenshot.is_file()
