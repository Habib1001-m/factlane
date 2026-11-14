from __future__ import annotations

import io
import json
import os
from typing import Any

from . import cli


def _safe_message(payload: str) -> str:
    try:
        decoded: Any = json.loads(payload)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        message = decoded.get("message") or decoded.get("error_code")
        if isinstance(message, str) and message:
            return message[:1000]
    return "FactLane Owner Panel could not be opened. Run FactLane setup or review its local service status."


def _show_windows_error(message: str) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "FactLane Owner Panel", 0x10)
    except Exception:
        return


def main() -> int:
    output = io.StringIO()
    try:
        result = cli.main(["launch", "webui"], output=output)
    except Exception:
        _show_windows_error("FactLane Owner Panel could not be opened safely.")
        return 1
    code = int(result) if isinstance(result, int) else 0
    if code != 0:
        _show_windows_error(_safe_message(output.getvalue()))
    return code


if __name__ == "__main__":  # pragma: no cover - exercised through installed launcher
    raise SystemExit(main())
