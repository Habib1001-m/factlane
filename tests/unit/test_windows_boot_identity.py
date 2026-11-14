from __future__ import annotations

import pytest

from factlane.webui_windows import WindowsWebUIError, _format_windows_boot_counter


@pytest.mark.parametrize("counter", [0, 1, 42, 0xFFFFFFFF])
def test_registry_boot_counter_valid_dword(counter: int) -> None:
    assert _format_windows_boot_counter(counter, 4, dword_type=4) == f"windows:boot-id:{counter}"


@pytest.mark.parametrize(
    ("value", "kind"),
    [(True, 4), ("1", 4), (None, 4), (-1, 4), (0x100000000, 4), (5, 3), (5, True)],
)
def test_registry_boot_counter_rejects_ambiguous_or_invalid_values(value: object, kind: object) -> None:
    with pytest.raises(WindowsWebUIError) as info:
        _format_windows_boot_counter(value, kind, dword_type=4)
    assert info.value.code == "WINDOWS_BOOT_ID_UNAVAILABLE"
