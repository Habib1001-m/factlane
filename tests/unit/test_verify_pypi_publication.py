from __future__ import annotations

import importlib.util
from pathlib import Path
import urllib.error

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_pypi_publication.py"
SPEC = importlib.util.spec_from_file_location("verify_pypi_publication", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

EXPECTED = {
    "factlane-0.1.6-py3-none-any.whl": {
        "sha256": "a" * 64,
        "size": 10,
        "packagetype": "bdist_wheel",
    },
    "factlane-0.1.6.tar.gz": {
        "sha256": "b" * 64,
        "size": 20,
        "packagetype": "sdist",
    },
}


def exact_json() -> dict:
    return {
        "info": {"name": "factlane", "version": "0.1.6"},
        "urls": [
            {
                "filename": filename,
                "digests": {"sha256": expected["sha256"]},
                "size": expected["size"],
                "packagetype": expected["packagetype"],
                "url": f"https://files.pythonhosted.org/{filename}",
            }
            for filename, expected in EXPECTED.items()
        ],
    }


def downloader(url: str, _timeout: float) -> tuple[str, int]:
    filename = url.rsplit("/", 1)[-1]
    expected = EXPECTED[filename]
    return str(expected["sha256"]), int(expected["size"])


def test_exact_metadata_and_downloaded_bytes_pass() -> None:
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=0,
        poll_interval_seconds=0,
        request_timeout_seconds=1,
        json_fetcher=lambda _url, _timeout: exact_json(),
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
    )
    assert receipt["status"] == "PASS"
    assert receipt["classification"] == "PUBLICATION_VERIFIED_EXACT_BYTES"
    assert receipt["doNotAutoRetry"] is True


def test_partial_visibility_retries_until_exact_file_set_is_visible() -> None:
    calls = 0

    def fetch(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        data = exact_json()
        if calls == 1:
            data["urls"] = data["urls"][:1]
        return data

    clock = iter([0.0, 0.0, 1.0, 1.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=10,
        poll_interval_seconds=1,
        request_timeout_seconds=1,
        json_fetcher=fetch,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert calls == 2
    assert receipt["status"] == "PASS"
    assert receipt["attempts"] == 2


@pytest.mark.parametrize("field", ["digests", "size", "packagetype", "url"])
def test_visible_filename_with_missing_metadata_retries_as_incomplete(field: str) -> None:
    calls = 0

    def fetch(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        data = exact_json()
        if calls == 1:
            del data["urls"][0][field]
        return data

    clock = iter([0.0, 0.0, 1.0, 1.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=10,
        poll_interval_seconds=1,
        request_timeout_seconds=1,
        json_fetcher=fetch,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert calls == 2
    assert receipt["status"] == "PASS"
    assert receipt["attempts"] == 2


@pytest.mark.parametrize("field", ["name", "version"])
def test_missing_info_field_retries_as_incomplete(field: str) -> None:
    calls = 0

    def fetch(_url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        data = exact_json()
        if calls == 1:
            del data["info"][field]
        return data

    clock = iter([0.0, 0.0, 1.0, 1.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=10,
        poll_interval_seconds=1,
        request_timeout_seconds=1,
        json_fetcher=fetch,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert calls == 2
    assert receipt["status"] == "PASS"
    assert receipt["attempts"] == 2


def test_partial_visibility_with_wrong_present_digest_is_immediate_contradiction() -> None:
    data = exact_json()
    data["urls"] = data["urls"][:1]
    data["urls"][0]["digests"]["sha256"] = "0" * 64
    clock = iter([0.0, 0.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=30,
        poll_interval_seconds=10,
        request_timeout_seconds=1,
        json_fetcher=lambda _url, _timeout: data,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["attempts"] == 1
    assert receipt["reconciliationTrigger"] == "PYPI_METADATA_CONTRADICTION"
    assert "SHA-256 metadata mismatch" in receipt["lastObservation"]


def test_transient_http_error_retries() -> None:
    calls = 0

    def fetch(url: str, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(url, 503, "temporary", {}, None)
        return exact_json()

    clock = iter([0.0, 0.0, 1.0, 1.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=10,
        poll_interval_seconds=1,
        request_timeout_seconds=1,
        json_fetcher=fetch,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert calls == 2
    assert receipt["status"] == "PASS"


def test_non_transient_http_error_requires_immediate_reconciliation() -> None:
    clock = iter([0.0, 0.0])

    def fetch(url: str, _timeout: float) -> dict:
        raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)

    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=600,
        poll_interval_seconds=10,
        request_timeout_seconds=1,
        json_fetcher=fetch,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["attempts"] == 1
    assert receipt["reconciliationTrigger"] == "NON_TRANSIENT_PYPI_READ_ERROR"
    assert "HTTP 403" in receipt["lastObservation"]


def test_timeout_requires_external_reconciliation_and_forbids_auto_retry() -> None:
    clock = iter([0.0, 0.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=0,
        poll_interval_seconds=0,
        request_timeout_seconds=1,
        json_fetcher=lambda url, _timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(url, 404, "not visible", {}, None)
        ),
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["classification"] == MODULE.RECONCILIATION_REQUIRED
    assert receipt["doNotAutoRetry"] is True
    assert "transient HTTP 404" in receipt["lastObservation"]


def test_metadata_size_mismatch_is_an_immediate_contradiction() -> None:
    data = exact_json()
    data["urls"][0]["size"] += 1
    clock = iter([0.0, 0.0])
    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=30,
        poll_interval_seconds=10,
        request_timeout_seconds=1,
        json_fetcher=lambda _url, _timeout: data,
        downloader=downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["classification"] == MODULE.RECONCILIATION_REQUIRED
    assert receipt["reconciliationTrigger"] == "PYPI_METADATA_CONTRADICTION"
    assert "size metadata mismatch" in receipt["lastObservation"]


def test_downloaded_byte_mismatch_is_an_immediate_contradiction() -> None:
    clock = iter([0.0, 0.0])

    def wrong_downloader(_url: str, _timeout: float) -> tuple[str, int]:
        return "0" * 64, 1

    receipt = MODULE.verify_until_deadline(
        project="factlane",
        version="0.1.6",
        expected_files=EXPECTED,
        timeout_seconds=30,
        poll_interval_seconds=10,
        request_timeout_seconds=1,
        json_fetcher=lambda _url, _timeout: exact_json(),
        downloader=wrong_downloader,
        sleeper=lambda _seconds: None,
        monotonic=lambda: next(clock),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["classification"] == MODULE.RECONCILIATION_REQUIRED
    assert receipt["reconciliationTrigger"] == "PYPI_BYTE_CONTRADICTION"
    assert "downloaded SHA-256 mismatch" in receipt["lastObservation"]
