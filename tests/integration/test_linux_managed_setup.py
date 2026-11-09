from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

if sys.platform != "linux":
    pytest.skip("managed Linux setup qualification is Linux-only", allow_module_level=True)

from factlane.webui_install import (
    BrowserInstall,
    InstallEnvironment,
    InstallerStepStatus,
    LinuxSetupRecoveryRequired,
    ProductionLinuxInstallerBackend,
    SetupConsent,
    bootstrap_linux_caddy_root,
    ensure_linux_firefox_profile_network_policy,
    ensure_linux_firefox_profile_trust,
    linux_setup_journal_path,
    load_linux_provisional_installation_id,
    plan_webui_setup,
)
from factlane.webui_platform import LinuxFirefoxBrowserLauncher, build_default_setup_request


def _request(tmp_path: Path, *, lingering_required: bool = False):
    return build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "linuxsetup123",
        lingering_detector=lambda _user: lingering_required,
    )


def _environment() -> InstallEnvironment:
    return InstallEnvironment(
        system="Linux",
        systemd=True,
        caddy_path="/usr/bin/caddy",
        zenity_path="/usr/bin/zenity",
        browser=BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        euid=os.getuid(),
    )


def _consent(*, lingering: bool = False) -> SetupConsent:
    return SetupConsent(
        hostname_mapping=True,
        browser_profile_trust=True,
        user_lingering=lingering,
    )


def test_linux_plan_uses_profile_local_dns_and_ca_bootstrap_without_hosts(tmp_path: Path) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())

    assert receipt.status == "READY_TO_APPLY"
    kinds = [operation.kind for operation in receipt.operations]
    assert "ENSURE_HOSTNAME_MAPPING" not in kinds
    assert kinds[:4] == [
        "PERSIST_PROVISIONAL_SETUP",
        "BOOTSTRAP_CADDY_CA",
        "ENSURE_ISOLATED_PROFILE_NETWORK",
        "ENSURE_ISOLATED_PROFILE_TRUST",
    ]
    assert all(operation.target != "/etc/hosts" for operation in receipt.operations)


def test_linux_profile_network_policy_is_exact_private_and_foreign_state_fails_closed(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "browser"
    receipt = ensure_linux_firefox_profile_network_policy(
        profile_dir=profile,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
    )

    assert receipt.status == "CREATED"
    assert profile.stat().st_mode & 0o777 == 0o700
    user_js = profile / "user.js"
    assert user_js.stat().st_mode & 0o777 == 0o600
    assert user_js.read_text(encoding="utf-8") == (
        'user_pref("network.dns.localDomains", "factlane-linuxsetup123.test");\n'
        'user_pref("network.trr.mode", 5);\n'
        'user_pref("network.proxy.type", 0);\n'
    )

    again = ensure_linux_firefox_profile_network_policy(
        profile_dir=profile,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
    )
    assert again.status == "REUSED"

    user_js.write_text('user_pref("network.proxy.type", 1);\n', encoding="utf-8")
    user_js.chmod(0o600)
    with pytest.raises(RuntimeError, match="foreign"):
        ensure_linux_firefox_profile_network_policy(
            profile_dir=profile,
            hostname="factlane-linuxsetup123.test",
            owner_uid=os.getuid(),
        )


def test_failed_setup_reuses_owner_private_provisional_installation_identity(tmp_path: Path) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    assert journal_op.kind == "PERSIST_PROVISIONAL_SETUP"

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=lambda _argv: 0,
    )
    result = backend.apply_operation(journal_op, plan=receipt.plan)
    assert result.status is InstallerStepStatus.APPLIED
    journal = linux_setup_journal_path(request.state_dir)
    assert journal.stat().st_mode & 0o777 == 0o600
    assert load_linux_provisional_installation_id(
        request.state_dir,
        owner_uid=os.getuid(),
    ) == "linuxsetup123"

    reused = build_default_setup_request(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        home=str(tmp_path / "home"),
        owner_uid=os.getuid(),
        installing_user="alice",
        factlane_executable="/usr/bin/factlane",
        installation_id_factory=lambda: "must-not-be-used",
        lingering_detector=lambda _user: False,
    )
    assert reused.installation_id == "linuxsetup123"


def test_linger_backend_uses_exact_loginctl_polkit_path_and_reprobes(tmp_path: Path) -> None:
    request = _request(tmp_path, lingering_required=True)
    receipt = plan_webui_setup(request, _environment(), _consent(lingering=True))
    assert receipt.plan is not None
    linger = next(op for op in receipt.operations if op.kind == "ENABLE_USER_LINGERING")
    calls: list[tuple[str, ...]] = []
    reads = iter([(0, "no\n"), (0, "yes\n")])
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        loginctl_path="/usr/bin/loginctl",
        owner_uid=os.getuid(),
        command_runner=lambda argv: calls.append(argv) or 0,
        read_command_runner=lambda _argv: next(reads),
    )

    result = backend.apply_operation(linger, plan=receipt.plan)

    assert result.status is InstallerStepStatus.APPLIED
    assert calls == [("/usr/bin/loginctl", "enable-linger", "--", "alice")]


def test_provisional_journal_foreign_binding_is_recovery_required(tmp_path: Path) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    journal = Path(journal_op.target)
    journal.parent.mkdir(parents=True, mode=0o700)
    journal.write_text(
        json.dumps({"version": 1, "installation_id": "foreign"}) + "\n",
        encoding="utf-8",
    )
    journal.chmod(0o600)
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=lambda _argv: 0,
    )

    result = backend.apply_operation(journal_op, plan=receipt.plan)

    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED


def _root_pem(tmp_path: Path) -> tuple[Path, str]:
    fixture = Path(__file__).parents[1] / "fixtures" / "windows_owner_http_test_identity.pem"
    content = fixture.read_text(encoding="ascii")
    end = content.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    pem = content[:end] + "\n"
    root = tmp_path / "root.crt"
    root.write_text(pem, encoding="ascii")
    root.chmod(0o600)
    return root, pem


def _journal_payload(request) -> dict[str, object]:
    path = linux_setup_journal_path(request.state_dir)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_journal(request, payload: dict[str, object]) -> None:
    path = linux_setup_journal_path(request.state_dir)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _install_test_root(plan, pem: str) -> Path:
    root = Path(plan.caddy_state_dir) / "pki" / "authorities" / "local" / "root.crt"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text(pem, encoding="ascii")
    root.chmod(0o600)
    return root


def _nss_read_runner(profile: Path, pem: str, state: dict[str, bool]):
    def read(argv: tuple[str, ...]) -> tuple[int, str]:
        if "is-enabled" in argv:
            return 1, "disabled\n"
        if "-L" not in argv:
            return 1, ""
        if "-n" in argv:
            return (0, pem) if state["imported"] else (1, "")
        listing = (
            "Certificate Nickname                                         Trust Attributes\n"
            "SSL,S/MIME,JAR/XPI\n\n"
        )
        if state["imported"]:
            listing += "FactLane Local CA                                           C,,\n"
        return 0, listing

    return read


def test_linux_firefox_nss_trust_imports_exact_root_with_manifest_and_reuses(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "browser"
    ensure_linux_firefox_profile_network_policy(
        profile_dir=profile,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
    )
    root, pem = _root_pem(tmp_path)
    state = {"imported": False}
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> int:
        commands.append(argv)
        if "-N" in argv:
            for name in ("cert9.db", "key4.db", "pkcs11.txt"):
                path = profile / name
                path.write_text("managed", encoding="utf-8")
                path.chmod(0o600)
            return 0
        if "-A" in argv:
            state["imported"] = True
            return 0
        if "-D" in argv:
            state["imported"] = False
            return 0
        return 1

    def read(argv: tuple[str, ...]) -> tuple[int, str]:
        if "-L" not in argv:
            return 1, ""
        if "-n" in argv:
            return (0, pem) if state["imported"] else (1, "")
        listing = "Certificate Nickname                                         Trust Attributes\nSSL,S/MIME,JAR/XPI\n\n"
        if state["imported"]:
            listing += "FactLane Local CA                                           C,,\n"
        return 0, listing

    first = ensure_linux_firefox_profile_trust(
        certutil_path="/usr/bin/certutil",
        firefox_executable="/usr/bin/firefox",
        profile_dir=profile,
        root_cert_path=root,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
        command_runner=run,
        read_command_runner=read,
    )
    second = ensure_linux_firefox_profile_trust(
        certutil_path="/usr/bin/certutil",
        firefox_executable="/usr/bin/firefox",
        profile_dir=profile,
        root_cert_path=root,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
        command_runner=run,
        read_command_runner=read,
    )

    assert first.status == "IMPORTED"
    assert first.certificate_imported is True
    assert first.manifest_created is True
    assert second.status == "REUSED"
    assert second.certificate_imported is False
    manifest = profile / ".factlane-trust.json"
    assert manifest.stat().st_mode & 0o777 == 0o600
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["root_fingerprint_sha256"] == first.root_fingerprint_sha256
    assert ("/usr/bin/certutil", "-A", "-d", f"sql:{profile}", "-n", "FactLane Local CA", "-t", "C,,", "-i", str(root)) in commands


def test_linux_firefox_nss_foreign_factlane_nickname_without_manifest_fails_closed(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "browser"
    ensure_linux_firefox_profile_network_policy(
        profile_dir=profile,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
    )
    for name in ("cert9.db", "key4.db", "pkcs11.txt"):
        path = profile / name
        path.write_text("foreign", encoding="utf-8")
        path.chmod(0o600)
    root, pem = _root_pem(tmp_path)

    def read(argv: tuple[str, ...]) -> tuple[int, str]:
        if "-n" in argv:
            return 0, pem
        return 0, (
            "Certificate Nickname                                         Trust Attributes\n"
            "SSL,S/MIME,JAR/XPI\n\n"
            "FactLane Local CA                                           C,,\n"
        )

    with pytest.raises(LinuxSetupRecoveryRequired, match="without its ownership manifest"):
        ensure_linux_firefox_profile_trust(
            certutil_path="/usr/bin/certutil",
            firefox_executable="/usr/bin/firefox",
            profile_dir=profile,
            root_cert_path=root,
            hostname="factlane-linuxsetup123.test",
            owner_uid=os.getuid(),
            command_runner=lambda _argv: pytest.fail("foreign trust must not be mutated"),
            read_command_runner=read,
        )


def test_caddy_ca_bootstrap_is_unclaimed_and_stops_before_return(tmp_path: Path) -> None:
    state = tmp_path / "caddy"
    root, pem = _root_pem(tmp_path)
    argv_seen: list[tuple[str, ...]] = []

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout: float):
            assert timeout == 2.0
            return 0

        def kill(self):
            self.stopped = True

    process = Process()

    def factory(argv: tuple[str, ...]):
        argv_seen.append(argv)
        target = state / "pki" / "authorities" / "local" / "root.crt"
        target.parent.mkdir(parents=True)
        target.write_text(pem, encoding="ascii")
        target.chmod(0o600)
        return process

    receipt = bootstrap_linux_caddy_root(
        caddy_path="/usr/bin/caddy",
        caddy_state_dir=state,
        hostname="factlane-linuxsetup123.test",
        owner_uid=os.getuid(),
        process_factory=factory,
    )

    assert receipt.status == "CREATED"
    assert receipt.caddy_state_created is True
    assert process.stopped is True
    assert argv_seen == [
        ("/usr/bin/caddy", "run", "--config", str(state / ".factlane-ca-bootstrap.Caddyfile"), "--adapter", "caddyfile")
    ]
    assert not (state / ".factlane-ca-bootstrap.Caddyfile").exists()
    assert root.read_text(encoding="ascii") == pem


def test_linux_firefox_launcher_is_shell_free_and_uses_only_managed_profile(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def spawn(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return object()

    launcher = LinuxFirefoxBrowserLauncher(
        BrowserInstall(family="firefox", executable="/usr/bin/firefox"),
        str(tmp_path / "browser"),
        process_factory=spawn,
    )

    assert launcher("https://factlane-linuxsetup123.test:8765#connect=abc%2Fdef") is True
    assert calls[0][0] == (
        "/usr/bin/firefox",
        "-no-remote",
        "-profile",
        str(tmp_path / "browser"),
        "https://factlane-linuxsetup123.test:8765#connect=abc%2Fdef",
    )
    assert calls[0][1]["shell"] is False
    assert launcher("https://factlane-linuxsetup123.test:8765?connect=leak") is False


def test_file_rollback_restores_exact_preexisting_bytes_and_foreign_change_holds(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=lambda _argv: 0,
    )
    journal = receipt.operations[0]
    assert backend.apply_operation(journal, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    target = tmp_path / "config" / "factlane" / "webui.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original\n", encoding="utf-8")
    target.chmod(0o600)
    config = next(op for op in receipt.operations if op.kind == "WRITE_SERVICE_CONFIG")
    config = type(config)(
        kind=config.kind,
        target=str(target),
        desired_state=config.desired_state,
        preserve_unrelated_state=config.preserve_unrelated_state,
        consent_key=config.consent_key,
        write_contract=config.write_contract,
    )
    assert backend.apply_operation(config, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert target.read_text(encoding="utf-8") == config.desired_state
    rolled = backend.rollback_setup(plan=receipt.plan, completed_operations=(config.kind,))
    assert rolled.status is InstallerStepStatus.APPLIED
    assert target.read_text(encoding="utf-8") == "original\n"

    backend2 = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=lambda _argv: 0,
    )
    assert backend2.apply_operation(journal, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert backend2.apply_operation(config, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    target.write_text("foreign-change\n", encoding="utf-8")
    target.chmod(0o600)
    held = backend2.rollback_setup(plan=receipt.plan, completed_operations=(config.kind,))
    assert held.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert target.read_text(encoding="utf-8") == "foreign-change\n"


def test_service_enable_nonzero_partial_success_is_durable_and_rollback_survives_retry(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    register = next(op for op in receipt.operations if op.kind == "REGISTER_USER_SERVICE")
    state = {"enabled": False}

    def read_service(_argv: tuple[str, ...]) -> tuple[int, str]:
        return (0, "enabled\n") if state["enabled"] else (1, "disabled\n")

    def first_run(argv: tuple[str, ...]) -> int:
        if argv[-2:] == ("enable", "factlane-webui.service"):
            state["enabled"] = True
            return 1
        return 0

    first = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=first_run,
        read_command_runner=read_service,
    )
    assert first.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    failed = first.apply_operation(register, plan=receipt.plan)
    assert failed.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert state["enabled"] is True
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    assert ownership["service_enabled_preexisting"] is False
    assert ownership["service_enable_intent"] is True
    assert ownership["service_enable_changed"] is True

    calls: list[tuple[str, ...]] = []

    def retry_run(argv: tuple[str, ...]) -> int:
        calls.append(argv)
        if argv[-2:] == ("disable", "factlane-webui.service"):
            state["enabled"] = False
        return 0

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=retry_run,
        read_command_runner=read_service,
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    rolled = retry.rollback_setup(plan=receipt.plan, completed_operations=())
    assert rolled.status is InstallerStepStatus.APPLIED
    assert state["enabled"] is False
    assert ("/usr/bin/systemctl", "--user", "disable", "factlane-webui.service") in calls
    reset = _journal_payload(request)["ownership"]
    assert isinstance(reset, dict)
    assert reset["service_enable_intent"] is False
    assert reset["service_enable_changed"] is False


def test_service_enable_retry_infers_crash_after_enable_without_repeating_mutation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    register = next(op for op in receipt.operations if op.kind == "REGISTER_USER_SERVICE")
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership.update(
        {
            "service_enabled_preexisting": False,
            "service_enable_intent": True,
            "service_enable_changed": False,
        }
    )
    _write_journal(request, payload)
    commands: list[tuple[str, ...]] = []

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
        command_runner=lambda argv: commands.append(argv) or 0,
        read_command_runner=lambda _argv: (0, "enabled\n"),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.apply_operation(register, plan=receipt.plan)
    assert result.status is InstallerStepStatus.APPLIED
    assert not any(argv[-2:] == ("enable", "factlane-webui.service") for argv in commands)
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert after["service_enable_changed"] is True


def test_caddy_create_intent_recovers_empty_unreceipted_directory_and_durable_rollback(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    caddy_op = next(op for op in receipt.operations if op.kind == "BOOTSTRAP_CADDY_CA")
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership["caddy_state_create_intent"] = True
    _write_journal(request, payload)
    caddy_state = Path(receipt.plan.caddy_state_dir)
    caddy_state.mkdir(mode=0o700)
    caddy_state.chmod(0o700)
    _, pem = _root_pem(tmp_path)

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout: float):
            assert timeout == 2.0
            return 0

        def kill(self):
            self.stopped = True

    process = Process()

    def factory(_argv: tuple[str, ...]):
        target = caddy_state / "pki" / "authorities" / "local" / "root.crt"
        target.parent.mkdir(parents=True)
        target.write_text(pem, encoding="ascii")
        target.chmod(0o600)
        return process

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        caddy_path="/usr/bin/caddy",
        owner_uid=os.getuid(),
        caddy_bootstrap_process_factory=factory,
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    completed = retry.apply_operation(caddy_op, plan=receipt.plan)
    assert completed.status is InstallerStepStatus.APPLIED
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert after["caddy_state_create_intent"] is True
    assert after["caddy_state_created"] is True

    rollback = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert rollback.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = rollback.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.APPLIED
    assert not caddy_state.exists()


def test_caddy_rollback_preserves_unrelated_state_and_requires_recovery(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    caddy_op = next(op for op in receipt.operations if op.kind == "BOOTSTRAP_CADDY_CA")
    _, pem = _root_pem(tmp_path)
    caddy_state = Path(receipt.plan.caddy_state_dir)

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout: float):
            assert timeout == 2.0
            return 0

        def kill(self):
            self.stopped = True

    def factory(_argv: tuple[str, ...]):
        target = caddy_state / "pki" / "authorities" / "local" / "root.crt"
        target.parent.mkdir(parents=True)
        target.write_text(pem, encoding="ascii")
        target.chmod(0o600)
        return Process()

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        caddy_path="/usr/bin/caddy",
        owner_uid=os.getuid(),
        caddy_bootstrap_process_factory=factory,
    )
    assert backend.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert backend.apply_operation(caddy_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    foreign = caddy_state / "foreign.txt"
    foreign.write_text("preserve", encoding="utf-8")
    foreign.chmod(0o600)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert _journal_payload(request)["phase"] == "RECOVERY_REQUIRED"


def test_caddy_rollback_removes_exact_runtime_certificate_state_and_empty_locks(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    caddy_op = next(op for op in receipt.operations if op.kind == "BOOTSTRAP_CADDY_CA")
    _, pem = _root_pem(tmp_path)
    caddy_state = Path(receipt.plan.caddy_state_dir)

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout: float):
            assert timeout == 2.0
            return 0

        def kill(self):
            self.stopped = True

    def factory(_argv: tuple[str, ...]):
        target = caddy_state / "pki" / "authorities" / "local" / "root.crt"
        target.parent.mkdir(parents=True)
        target.write_text(pem, encoding="ascii")
        target.chmod(0o600)
        return Process()

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        caddy_path="/usr/bin/caddy",
        owner_uid=os.getuid(),
        caddy_bootstrap_process_factory=factory,
    )
    assert backend.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert backend.apply_operation(caddy_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED

    hostname = receipt.plan.hostname
    leaf = caddy_state / "certificates" / "local" / hostname
    leaf.mkdir(parents=True, mode=0o700)
    for directory in (caddy_state / "certificates", caddy_state / "certificates" / "local", leaf):
        directory.chmod(0o700)
    for suffix in ("crt", "key", "json"):
        target = leaf / f"{hostname}.{suffix}"
        target.write_text(f"managed-{suffix}", encoding="utf-8")
        target.chmod(0o600)
    locks = caddy_state / "locks"
    locks.mkdir(mode=0o700)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.APPLIED
    assert not caddy_state.exists()


def test_caddy_rollback_rejects_foreign_runtime_certificate_state(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    caddy_op = next(op for op in receipt.operations if op.kind == "BOOTSTRAP_CADDY_CA")
    _, pem = _root_pem(tmp_path)
    caddy_state = Path(receipt.plan.caddy_state_dir)

    class Process:
        stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout: float):
            assert timeout == 2.0
            return 0

        def kill(self):
            self.stopped = True

    def factory(_argv: tuple[str, ...]):
        target = caddy_state / "pki" / "authorities" / "local" / "root.crt"
        target.parent.mkdir(parents=True)
        target.write_text(pem, encoding="ascii")
        target.chmod(0o600)
        return Process()

    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        caddy_path="/usr/bin/caddy",
        owner_uid=os.getuid(),
        caddy_bootstrap_process_factory=factory,
    )
    assert backend.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert backend.apply_operation(caddy_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED

    leaf = caddy_state / "certificates" / "local" / receipt.plan.hostname
    leaf.mkdir(parents=True, mode=0o700)
    for directory in (caddy_state / "certificates", caddy_state / "certificates" / "local", leaf):
        directory.chmod(0o700)
    foreign = leaf / "foreign.pem"
    foreign.write_text("preserve", encoding="utf-8")
    foreign.chmod(0o600)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert foreign.read_text(encoding="utf-8") == "preserve"
    assert _journal_payload(request)["phase"] == "RECOVERY_REQUIRED"


def test_profile_and_user_js_intents_recover_exact_interrupted_state_and_rollback(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    network_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_NETWORK"
    )
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    profile = Path(network_op.target)
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    info = profile.stat()
    user_js = profile / "user.js"
    user_js.write_text(
        'user_pref("network.dns.localDomains", "factlane-linuxsetup123.test");\n'
        'user_pref("network.trr.mode", 5);\n'
        'user_pref("network.proxy.type", 0);\n',
        encoding="utf-8",
    )
    user_js.chmod(0o600)
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership.update(
        {
            "profile_create_intent": True,
            "profile_created": True,
            "profile_device": int(info.st_dev),
            "profile_inode": int(info.st_ino),
            "user_js_create_intent": True,
        }
    )
    _write_journal(request, payload)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    recovered = retry.apply_operation(network_op, plan=receipt.plan)
    assert recovered.status is InstallerStepStatus.APPLIED
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert "user.js" in after["profile_created_files"]

    rollback = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert rollback.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = rollback.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.APPLIED
    assert not profile.exists()


def test_profile_create_intent_recovers_empty_directory_created_before_receipt(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    network_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_NETWORK"
    )
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership["profile_create_intent"] = True
    _write_journal(request, payload)
    profile = Path(network_op.target)
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.apply_operation(network_op, plan=receipt.plan)
    assert result.status is InstallerStepStatus.APPLIED
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert after["profile_create_intent"] is True
    assert after["profile_created"] is True
    assert "user.js" in after["profile_created_files"]


def test_partial_nss_creation_retries_from_durable_intent_and_new_backend_rollback_uses_journal(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    network_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_NETWORK"
    )
    trust_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_TRUST"
    )
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert initial.apply_operation(network_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    _, pem = _root_pem(tmp_path)
    root = _install_test_root(receipt.plan, pem)
    assert root.is_file()
    profile = Path(trust_op.target)
    partial = profile / "cert9.db"
    partial.write_text("partial", encoding="utf-8")
    partial.chmod(0o600)
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership["nss_create_intent"] = True
    _write_journal(request, payload)
    state = {"imported": False}
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> int:
        commands.append(argv)
        if "-N" in argv:
            for name in ("cert9.db", "key4.db", "pkcs11.txt"):
                path = profile / name
                path.write_text("managed", encoding="utf-8")
                path.chmod(0o600)
            return 0
        if "-A" in argv:
            state["imported"] = True
            return 0
        if "-D" in argv:
            state["imported"] = False
            return 0
        return 0

    read = _nss_read_runner(profile, pem, state)
    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        certutil_path="/usr/bin/certutil",
        owner_uid=os.getuid(),
        command_runner=run,
        read_command_runner=read,
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    recovered = retry.apply_operation(trust_op, plan=receipt.plan)
    assert recovered.status is InstallerStepStatus.APPLIED
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert after["nss_create_intent"] is True
    assert after["trust_import_intent"] is True
    assert after["trust_manifest_intent"] is True
    assert after["trust_imported"] is True
    assert after["trust_manifest_created"] is True
    assert {"cert9.db", "key4.db", "pkcs11.txt", ".factlane-trust.json"}.issubset(
        set(after["profile_created_files"])
    )
    assert ("/usr/bin/certutil", "-N", "--empty-password", "-d", f"sql:{profile}") in commands

    rollback = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        certutil_path="/usr/bin/certutil",
        owner_uid=os.getuid(),
        command_runner=run,
        read_command_runner=read,
    )
    assert rollback.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = rollback.rollback_setup(plan=receipt.plan, completed_operations=())
    assert result.status is InstallerStepStatus.APPLIED
    assert not profile.exists()
    assert root.is_file(), "unowned Caddy state must be preserved"


def test_trust_import_without_manifest_recovers_only_with_prior_durable_intent(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    network_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_NETWORK"
    )
    trust_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_TRUST"
    )
    initial = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        owner_uid=os.getuid(),
    )
    assert initial.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert initial.apply_operation(network_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    _, pem = _root_pem(tmp_path)
    _install_test_root(receipt.plan, pem)
    profile = Path(trust_op.target)
    for name in ("cert9.db", "key4.db", "pkcs11.txt"):
        path = profile / name
        path.write_text("managed", encoding="utf-8")
        path.chmod(0o600)
    payload = _journal_payload(request)
    ownership = payload["ownership"]
    assert isinstance(ownership, dict)
    ownership["nss_create_intent"] = True
    ownership["trust_import_intent"] = True
    _write_journal(request, payload)
    state = {"imported": True}
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> int:
        commands.append(argv)
        if "-D" in argv:
            state["imported"] = False
        return 0

    read = _nss_read_runner(profile, pem, state)
    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        certutil_path="/usr/bin/certutil",
        owner_uid=os.getuid(),
        command_runner=run,
        read_command_runner=read,
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    recovered = retry.apply_operation(trust_op, plan=receipt.plan)
    assert recovered.status is InstallerStepStatus.APPLIED
    assert not any("-A" in argv for argv in commands)
    manifest = profile / ".factlane-trust.json"
    assert manifest.is_file()
    after = _journal_payload(request)["ownership"]
    assert isinstance(after, dict)
    assert after["trust_import_intent"] is True
    assert after["trust_manifest_intent"] is True
    assert after["trust_imported"] is True
    assert after["trust_manifest_created"] is True


def test_interrupted_nss_state_without_prior_intent_is_foreign_and_preserved(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    receipt = plan_webui_setup(request, _environment(), _consent())
    assert receipt.plan is not None
    journal_op = receipt.operations[0]
    network_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_NETWORK"
    )
    trust_op = next(
        op for op in receipt.operations if op.kind == "ENSURE_ISOLATED_PROFILE_TRUST"
    )
    backend = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        certutil_path="/usr/bin/certutil",
        owner_uid=os.getuid(),
    )
    assert backend.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    assert backend.apply_operation(network_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    _, pem = _root_pem(tmp_path)
    _install_test_root(receipt.plan, pem)
    profile = Path(trust_op.target)
    foreign = profile / "cert9.db"
    foreign.write_text("foreign-partial", encoding="utf-8")
    foreign.chmod(0o600)

    retry = ProductionLinuxInstallerBackend(
        systemctl_path="/usr/bin/systemctl",
        certutil_path="/usr/bin/certutil",
        owner_uid=os.getuid(),
        command_runner=lambda _argv: pytest.fail("foreign NSS state must not be mutated"),
        read_command_runner=lambda _argv: pytest.fail("foreign partial NSS must fail before probing"),
    )
    assert retry.apply_operation(journal_op, plan=receipt.plan).status is InstallerStepStatus.APPLIED
    result = retry.apply_operation(trust_op, plan=receipt.plan)
    assert result.status is InstallerStepStatus.RECOVERY_REQUIRED
    assert foreign.read_text(encoding="utf-8") == "foreign-partial"
