from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from factlane import windows_tls
from factlane.owner_auth import NativeConfirmationRequest, OwnerAuthState
from factlane.owner_http import WindowsOwnerHTTPServer
from factlane.windows_tls import (
    WindowsTLSError,
    build_windows_client_ssl_context,
    build_windows_server_ssl_context,
    ensure_windows_tls_material,
    load_windows_tls_material,
)


native_windows = pytest.mark.skipif(sys.platform != "win32", reason="native Windows TLS qualification only")
off_windows = pytest.mark.skipif(sys.platform == "win32", reason="off-platform typed failure only")


class _ApproveNativeConfirmation:
    def confirm(self, request: NativeConfirmationRequest) -> bool:
        del request
        return True


@off_windows
def test_windows_tls_imports_off_platform_and_fails_typed(tmp_path: Path) -> None:
    with pytest.raises(WindowsTLSError) as caught:
        ensure_windows_tls_material(tmp_path / "tls", "factlane-offplatform.test")
    assert caught.value.code == "WINDOWS_TLS_UNAVAILABLE"


def _attempt_parent_substitution(root: Path, alternate: Path, state: dict[str, bool]) -> None:
    if state["attempted"]:
        return
    state["attempted"] = True
    retired = root.with_name(root.name + "-retired")
    try:
        root.rename(retired)
    except OSError:
        state["rename_blocked"] = True
        return
    state["renamed"] = True
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(root), str(alternate)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"junction substitution setup failed: {completed.stderr or completed.stdout}")


@native_windows
def test_tls_material_is_owner_private_and_manifest_binds_valid_crypto(tmp_path: Path) -> None:
    from factlane.windows_security import current_user_sid, validate_owner_private_path

    root = tmp_path / "tls"
    material = ensure_windows_tls_material(root, "factlane-owner123.test")
    sid = current_user_sid()

    validate_owner_private_path(root, owner_sid=sid, directory=True)
    for path in (
        material.root_key_path,
        material.root_cert_path,
        material.leaf_key_path,
        material.leaf_cert_path,
        material.manifest_path,
    ):
        validate_owner_private_path(path, owner_sid=sid, directory=False)

    manifest = json.loads(material.manifest_path.read_text(encoding="utf-8"))
    assert manifest["hostname"] == material.hostname
    assert manifest["root_fingerprint_sha256"] == material.root_fingerprint_sha256
    assert manifest["leaf_fingerprint_sha256"] == material.leaf_fingerprint_sha256
    assert set(manifest["files"]) == {"root_key", "root_cert", "leaf_key", "leaf_cert"}
    for role, path in {
        "root_key": material.root_key_path,
        "root_cert": material.root_cert_path,
        "leaf_key": material.leaf_key_path,
        "leaf_cert": material.leaf_cert_path,
    }.items():
        assert manifest["files"][role]["name"] == path.name
        assert manifest["files"][role]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    reloaded = load_windows_tls_material(root, material.hostname)
    assert reloaded == material


@native_windows
def test_tls_rejects_reparse_ancestor_without_redirected_mutation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    junction = tmp_path / "junction"
    real.mkdir()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr or completed.stdout}")
    try:
        with pytest.raises(WindowsTLSError) as caught:
            ensure_windows_tls_material(junction / "tls", "factlane-reparse.test")
        assert caught.value.code == "WINDOWS_TLS_CUSTODY_UNSAFE"
        assert not (real / "tls").exists()
    finally:
        try:
            junction.rmdir()
        except OSError:
            pass


@native_windows
def test_tls_retained_parent_blocks_substitution_during_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tls"
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    state = {"attempted": False, "rename_blocked": False, "renamed": False}
    original = windows_tls._nt_create_relative_file

    def racing_create(guard, name: str, *, delete_access: bool = False):
        _attempt_parent_substitution(root, alternate, state)
        return original(guard, name, delete_access=delete_access)

    monkeypatch.setattr(windows_tls, "_nt_create_relative_file", racing_create)
    try:
        if state["renamed"]:
            pytest.fail("race injection state changed before issuance started")
        try:
            material = ensure_windows_tls_material(root, "factlane-create-race.test")
        except WindowsTLSError as exc:
            assert state["renamed"] is True
            assert exc.code == "WINDOWS_TLS_CUSTODY_UNSAFE"
        else:
            assert state["rename_blocked"] is True
            assert material.manifest_path.is_file()
        assert state["attempted"] is True
        assert list(alternate.iterdir()) == []
    finally:
        if state["renamed"]:
            try:
                os.rmdir(root)
            except OSError:
                pass


@native_windows
def test_tls_manifest_publication_uses_bound_source_handle_not_replaceable_temp_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tls"
    attacker = tmp_path / "attacker-manifest.json"
    attacker_payload = b'{"attacker":true}\n'
    attacker.write_bytes(attacker_payload)
    state = {"attempted": False, "blocked": False, "replaced": False}
    original_publish = windows_tls._rename_handle_manifest_replace

    def racing_publish(source_handle, guard, target_name: str):
        state["attempted"] = True
        source_path = Path(windows_tls._handle_identity(source_handle).final_path)
        try:
            os.replace(attacker, source_path)
        except OSError:
            state["blocked"] = True
        else:
            state["replaced"] = True
        return original_publish(source_handle, guard, target_name)

    monkeypatch.setattr(windows_tls, "_rename_handle_manifest_replace", racing_publish)
    material = ensure_windows_tls_material(root, "factlane-source-binding.test")

    assert state["attempted"] is True
    assert state["blocked"] or state["replaced"]
    assert material.manifest_path.read_bytes() != attacker_payload
    manifest = json.loads(material.manifest_path.read_text(encoding="utf-8"))
    assert manifest["hostname"] == material.hostname
    assert load_windows_tls_material(root, material.hostname) == material


@native_windows
def test_tls_retained_parent_blocks_substitution_during_manifest_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tls"
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    state = {"attempted": False, "rename_blocked": False, "renamed": False}
    original_publish = windows_tls._rename_handle_manifest_replace

    def racing_publish(source_handle, guard, target_name: str):
        _attempt_parent_substitution(root, alternate, state)
        return original_publish(source_handle, guard, target_name)

    monkeypatch.setattr(windows_tls, "_rename_handle_manifest_replace", racing_publish)
    try:
        try:
            material = ensure_windows_tls_material(root, "factlane-publish-race.test")
        except WindowsTLSError as exc:
            assert state["renamed"] is True
            assert exc.code == "WINDOWS_TLS_CUSTODY_UNSAFE"
        else:
            assert state["rename_blocked"] is True
            assert material.manifest_path.is_file()
        assert state["attempted"] is True
        assert list(alternate.iterdir()) == []
    finally:
        if state["renamed"]:
            try:
                os.rmdir(root)
            except OSError:
                pass


@native_windows
def test_tls_corruption_rotates_to_new_generation_without_deleting_ambiguous_files(tmp_path: Path) -> None:
    root = tmp_path / "tls"
    hostname = "factlane-corruption.test"
    first = ensure_windows_tls_material(root, hostname)
    old_leaf = first.leaf_cert_path
    old_root = first.root_cert_path
    old_leaf.write_bytes(b"corrupted-leaf")

    second = ensure_windows_tls_material(root, hostname)

    assert second.leaf_cert_path != old_leaf
    assert second.root_cert_path != old_root
    assert old_leaf.read_bytes() == b"corrupted-leaf"
    assert old_root.is_file()
    assert load_windows_tls_material(root, hostname) == second


@native_windows
def test_tls_wrong_host_fails_closed_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "tls"
    ensure_windows_tls_material(root, "factlane-host-one.test")
    before = sorted(path.name for path in root.iterdir())

    with pytest.raises(WindowsTLSError) as caught:
        ensure_windows_tls_material(root, "factlane-host-two.test")

    assert caught.value.code == "WINDOWS_TLS_HOST_MISMATCH"
    assert sorted(path.name for path in root.iterdir()) == before


@native_windows
def test_tls_key_mismatch_is_rejected_even_when_manifest_file_hash_is_rebound(tmp_path: Path) -> None:
    root = tmp_path / "tls"
    hostname = "factlane-key-mismatch.test"
    material = ensure_windows_tls_material(root, hostname)
    manifest = json.loads(material.manifest_path.read_text(encoding="utf-8"))
    mismatched_key = material.root_key_path.read_bytes()
    material.leaf_key_path.write_bytes(mismatched_key)
    manifest["files"]["leaf_key"]["sha256"] = hashlib.sha256(mismatched_key).hexdigest()
    material.manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WindowsTLSError) as caught:
        load_windows_tls_material(root, hostname)
    assert caught.value.code == "WINDOWS_TLS_MATERIAL_INVALID"


@native_windows
def test_tls_unsafe_manifest_dacl_fails_closed_without_new_generation(tmp_path: Path) -> None:
    import win32security

    from factlane.windows_security import current_user_sid

    root = tmp_path / "tls"
    hostname = "factlane-unsafe-dacl.test"
    material = ensure_windows_tls_material(root, hostname)
    sid = current_user_sid()
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid}D:P(A;;GA;;;{sid})(A;;GR;;;WD)",
        win32security.SDDL_REVISION_1,
    )
    win32security.SetNamedSecurityInfo(
        str(material.manifest_path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | getattr(win32security, "PROTECTED_DACL_SECURITY_INFORMATION", 0x80000000),
        descriptor.GetSecurityDescriptorOwner(),
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )
    before = sorted(path.name for path in root.iterdir())

    with pytest.raises(WindowsTLSError) as caught:
        ensure_windows_tls_material(root, hostname)

    assert caught.value.code == "WINDOWS_TLS_CUSTODY_UNSAFE"
    assert sorted(path.name for path in root.iterdir()) == before


@native_windows
def test_tls_filename_collision_is_abandoned_without_deleting_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from factlane.windows_security import (
        current_user_sid,
        ensure_owner_private_directory,
        protect_owner_private_path,
    )

    root = tmp_path / "tls"
    sid = current_user_sid()
    ensure_owner_private_directory(root, owner_sid=sid)
    collision_token = "1" * 32
    collision = root / f"ca-{collision_token}.key.pem"
    collision.write_bytes(b"preexisting-ambiguous-file")
    protect_owner_private_path(collision, owner_sid=sid, directory=False)
    tokens = iter([collision_token, "2" * 32, "3" * 32, "4" * 32, "5" * 32])
    monkeypatch.setattr(windows_tls, "_new_generation_token", lambda: next(tokens))

    material = ensure_windows_tls_material(root, "factlane-collision.test")

    assert material.root_key_path != collision
    assert collision.read_bytes() == b"preexisting-ambiguous-file"
    assert load_windows_tls_material(root, material.hostname) == material


@native_windows
def test_tls_leaf_renews_with_same_private_root_before_expiry(tmp_path: Path) -> None:
    root = tmp_path / "tls"
    hostname = "factlane-renewal.test"
    now = datetime.now(timezone.utc)
    first = ensure_windows_tls_material(root, hostname, now=now)

    second = ensure_windows_tls_material(
        root,
        hostname,
        now=first.leaf_not_after_utc - timedelta(days=13),
    )

    assert second.root_cert_path == first.root_cert_path
    assert second.root_fingerprint_sha256 == first.root_fingerprint_sha256
    assert second.leaf_cert_path != first.leaf_cert_path
    assert second.leaf_fingerprint_sha256 != first.leaf_fingerprint_sha256


def _serve_one(listener: socket.socket, context: ssl.SSLContext, errors: list[BaseException]) -> None:
    try:
        connection, _address = listener.accept()
        with connection:
            with context.wrap_socket(connection, server_side=True) as tls_socket:
                assert tls_socket.recv(4) == b"ping"
                tls_socket.sendall(b"pong")
    except BaseException as exc:  # test helper must surface server-side TLS failures
        errors.append(exc)


@native_windows
def test_tls_contexts_complete_verified_exact_hostname_handshake(tmp_path: Path) -> None:
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-handshake.test")
    server_context = build_windows_server_ssl_context(material)
    client_context = build_windows_client_ssl_context(material)

    assert server_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert client_context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert client_context.verify_mode == ssl.CERT_REQUIRED
    assert client_context.check_hostname is True
    assert len(client_context.get_ca_certs(binary_form=True)) == 1

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    errors: list[BaseException] = []
    thread = threading.Thread(target=_serve_one, args=(listener, server_context, errors))
    thread.start()
    try:
        with socket.create_connection(listener.getsockname(), timeout=5) as raw:
            with client_context.wrap_socket(raw, server_hostname=material.hostname) as tls_socket:
                tls_socket.sendall(b"ping")
                assert tls_socket.recv(4) == b"pong"
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    errors = []
    thread = threading.Thread(target=_serve_one, args=(listener, server_context, errors))
    thread.start()
    try:
        with socket.create_connection(listener.getsockname(), timeout=5) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                client_context.wrap_socket(raw, server_hostname="factlane-wrong-host.test")
    finally:
        listener.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], ssl.SSLError)


@native_windows
def test_tls_contexts_drive_windows_owner_http_readiness_probe(tmp_path: Path) -> None:
    material = ensure_windows_tls_material(tmp_path / "tls", "factlane-readiness.test")
    server_context = build_windows_server_ssl_context(material)
    client_context = build_windows_client_ssl_context(material)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    server = WindowsOwnerHTTPServer(
        origin=f"https://{material.hostname}:{port}",
        tls_context=server_context,
        auth_state=OwnerAuthState(owner_uid=1000, native_confirmation=_ApproveNativeConfirmation()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.probe_ready(trust_context=client_context, timeout_seconds=1.0) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
