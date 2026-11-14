from __future__ import annotations

import json

from factlane import webui_launcher


def test_gui_launcher_reuses_exact_cli_launch_flow_without_terminal_output(monkeypatch) -> None:
    calls: list[tuple[list[str], object]] = []

    def fake_main(argv, *, output):
        calls.append((list(argv), output))
        output.write(json.dumps({"status": "OK", "opened": True}))
        return 0

    monkeypatch.setattr(webui_launcher.cli, "main", fake_main)

    assert webui_launcher.main() == 0
    assert [item[0] for item in calls] == [["launch", "webui"]]


def test_gui_launcher_returns_failure_without_inventing_an_alternate_open_path(monkeypatch) -> None:
    messages: list[str] = []

    def fake_main(argv, *, output):
        assert argv == ["launch", "webui"]
        output.write(json.dumps({"status": "ERROR", "message": "setup required"}))
        return 1

    monkeypatch.setattr(webui_launcher.cli, "main", fake_main)
    monkeypatch.setattr(webui_launcher, "_show_windows_error", messages.append)

    assert webui_launcher.main() == 1
    assert messages == ["setup required"]
