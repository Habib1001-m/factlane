#!/usr/bin/env python3
"""Verify an already-written PyPI release without ever authorizing a retry.

This command is intentionally post-write only. A non-zero result means the
publication outcome is uncertain or mismatched and requires external
reconciliation. It never means that re-publishing the same version is safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


RECONCILIATION_REQUIRED = "PUBLICATION_OUTCOME_REQUIRES_EXTERNAL_RECONCILIATION"
DO_NOT_AUTO_RETRY = "DO NOT AUTO-RETRY"
TRANSIENT_HTTP_CODES = {404, 408, 425, 429, 500, 502, 503, 504}
USER_AGENT = "FactLane-PyPI-PostWrite-Verifier/1"
EXACT = "EXACT"
INCOMPLETE = "INCOMPLETE"
CONTRADICTION = "CONTRADICTION"


def fetch_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status} for {url}")
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError("PyPI JSON response is not an object")
    return payload


def download_digest_and_size(url: str, timeout: float) -> tuple[str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status} for {url}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def expected_files_from_args(args: argparse.Namespace) -> dict[str, dict[str, object]]:
    return {
        args.wheel_name: {
            "sha256": args.wheel_sha256,
            "size": args.wheel_size,
            "packagetype": "bdist_wheel",
        },
        args.sdist_name: {
            "sha256": args.sdist_sha256,
            "size": args.sdist_size,
            "packagetype": "sdist",
        },
    }


def validate_metadata(
    data: dict,
    *,
    project: str,
    version: str,
    expected_files: dict[str, dict[str, object]],
) -> tuple[str, str, dict[str, dict]]:
    try:
        info = data["info"]
        urls = data["urls"]
        if not isinstance(info, dict) or not isinstance(urls, list):
            return INCOMPLETE, "PyPI JSON has incomplete info/urls structure", {}
        project_name = info.get("name")
        if project_name is None:
            return INCOMPLETE, "project name not visible yet", {}
        if str(project_name).lower() != project.lower():
            return CONTRADICTION, f"project name mismatch: {project_name!r}", {}
        observed_version = info.get("version")
        if observed_version is None:
            return INCOMPLETE, "version not visible yet", {}
        if observed_version != version:
            return CONTRADICTION, f"version mismatch: {observed_version!r}", {}

        filenames = [entry.get("filename") for entry in urls if isinstance(entry, dict)]
        if len(filenames) != len(urls) or len(set(filenames)) != len(filenames):
            return CONTRADICTION, "PyPI file list contains malformed or duplicate filenames", {}
        actual_names = set(filenames)
        expected_names = set(expected_files)
        unexpected = actual_names - expected_names
        if unexpected:
            return (
                CONTRADICTION,
                f"unexpected PyPI filenames: {sorted(unexpected)}",
                {},
            )

        actual = {entry["filename"]: entry for entry in urls}
        for filename in actual_names:
            expected = expected_files[filename]
            entry = actual[filename]
            digests = entry.get("digests")
            if not isinstance(digests, dict):
                return INCOMPLETE, f"digests not visible yet for {filename}", {}
            sha256 = digests.get("sha256")
            if sha256 is None:
                return INCOMPLETE, f"SHA-256 not visible yet for {filename}", {}
            if sha256 != expected["sha256"]:
                return CONTRADICTION, f"SHA-256 metadata mismatch for {filename}", {}
            size = entry.get("size")
            if size is None:
                return INCOMPLETE, f"size not visible yet for {filename}", {}
            if size != expected["size"]:
                return CONTRADICTION, f"size metadata mismatch for {filename}", {}
            package_type = entry.get("packagetype")
            if package_type is None:
                return INCOMPLETE, f"package type not visible yet for {filename}", {}
            if package_type != expected["packagetype"]:
                return CONTRADICTION, f"package type mismatch for {filename}", {}
            file_url = entry.get("url")
            if file_url is None:
                return INCOMPLETE, f"file URL not visible yet for {filename}", {}
            if not isinstance(file_url, str) or not file_url.startswith("https://"):
                return CONTRADICTION, f"invalid or non-HTTPS file URL for {filename}", {}
        if actual_names != expected_names:
            missing = expected_names - actual_names
            return INCOMPLETE, f"expected PyPI files not visible yet: {sorted(missing)}", {}
        return EXACT, "exact PyPI metadata observed", actual
    except (KeyError, TypeError, ValueError) as exc:
        return INCOMPLETE, f"incomplete PyPI JSON: {exc}", {}


def verify_downloaded_bytes(
    actual: dict[str, dict],
    expected_files: dict[str, dict[str, object]],
    *,
    request_timeout: float,
    downloader: Callable[[str, float], tuple[str, int]] = download_digest_and_size,
) -> tuple[bool, str]:
    for filename, expected in expected_files.items():
        digest, size = downloader(actual[filename]["url"], request_timeout)
        if digest != expected["sha256"]:
            return False, f"downloaded SHA-256 mismatch for {filename}"
        if size != expected["size"]:
            return False, f"downloaded size mismatch for {filename}"
    return True, "exact PyPI bytes observed"


def is_transient_http_error(exc: urllib.error.HTTPError) -> bool:
    return exc.code in TRANSIENT_HTTP_CODES


def hold_receipt(
    *,
    project: str,
    version: str,
    expected_files: dict[str, dict[str, object]],
    attempts: int,
    elapsed_seconds: float,
    observation: str,
    trigger: str,
) -> dict:
    return {
        "status": "HOLD",
        "classification": RECONCILIATION_REQUIRED,
        "doNotAutoRetry": True,
        "project": project,
        "version": version,
        "attempts": attempts,
        "elapsedSeconds": round(elapsed_seconds, 3),
        "reconciliationTrigger": trigger,
        "lastObservation": observation,
        "files": expected_files,
    }


def verify_until_deadline(
    *,
    project: str,
    version: str,
    expected_files: dict[str, dict[str, object]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    request_timeout_seconds: float,
    json_fetcher: Callable[[str, float], dict] = fetch_json,
    downloader: Callable[[str, float], tuple[str, int]] = download_digest_and_size,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    if timeout_seconds < 0 or poll_interval_seconds < 0 or request_timeout_seconds <= 0:
        raise ValueError("timeouts must be non-negative and request timeout must be positive")

    url = f"https://pypi.org/pypi/{project}/{version}/json"
    started = monotonic()
    deadline = started + timeout_seconds
    attempts = 0
    last_observation = "verification did not run"

    while True:
        attempts += 1
        try:
            data = json_fetcher(url, request_timeout_seconds)
            metadata_state, observation, actual = validate_metadata(
                data,
                project=project,
                version=version,
                expected_files=expected_files,
            )
            last_observation = observation
            if metadata_state == CONTRADICTION:
                return hold_receipt(
                    project=project,
                    version=version,
                    expected_files=expected_files,
                    attempts=attempts,
                    elapsed_seconds=monotonic() - started,
                    observation=last_observation,
                    trigger="PYPI_METADATA_CONTRADICTION",
                )
            if metadata_state == EXACT:
                try:
                    bytes_ok, observation = verify_downloaded_bytes(
                        actual,
                        expected_files,
                        request_timeout=request_timeout_seconds,
                        downloader=downloader,
                    )
                    last_observation = observation
                    if not bytes_ok:
                        return hold_receipt(
                            project=project,
                            version=version,
                            expected_files=expected_files,
                            attempts=attempts,
                            elapsed_seconds=monotonic() - started,
                            observation=last_observation,
                            trigger="PYPI_BYTE_CONTRADICTION",
                        )
                    return {
                        "status": "PASS",
                        "classification": "PUBLICATION_VERIFIED_EXACT_BYTES",
                        "doNotAutoRetry": True,
                        "project": project,
                        "version": version,
                        "attempts": attempts,
                        "elapsedSeconds": round(monotonic() - started, 3),
                        "files": expected_files,
                    }
                except urllib.error.HTTPError as exc:
                    if is_transient_http_error(exc):
                        last_observation = f"transient HTTP {exc.code} while downloading PyPI bytes"
                    else:
                        return hold_receipt(
                            project=project,
                            version=version,
                            expected_files=expected_files,
                            attempts=attempts,
                            elapsed_seconds=monotonic() - started,
                            observation=f"HTTP {exc.code} while downloading PyPI bytes",
                            trigger="NON_TRANSIENT_PYPI_READ_ERROR",
                        )
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_observation = f"transient byte-download error: {type(exc).__name__}: {exc}"
                except Exception as exc:
                    return hold_receipt(
                        project=project,
                        version=version,
                        expected_files=expected_files,
                        attempts=attempts,
                        elapsed_seconds=monotonic() - started,
                        observation=f"byte verification error: {type(exc).__name__}: {exc}",
                        trigger="POST_WRITE_VERIFIER_ERROR",
                    )
        except urllib.error.HTTPError as exc:
            if is_transient_http_error(exc):
                last_observation = f"transient HTTP {exc.code} while reading PyPI"
            else:
                return hold_receipt(
                    project=project,
                    version=version,
                    expected_files=expected_files,
                    attempts=attempts,
                    elapsed_seconds=monotonic() - started,
                    observation=f"HTTP {exc.code} while reading PyPI",
                    trigger="NON_TRANSIENT_PYPI_READ_ERROR",
                )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_observation = f"transient read error: {type(exc).__name__}: {exc}"
        except Exception as exc:
            return hold_receipt(
                project=project,
                version=version,
                expected_files=expected_files,
                attempts=attempts,
                elapsed_seconds=monotonic() - started,
                observation=f"verification error: {type(exc).__name__}: {exc}",
                trigger="POST_WRITE_VERIFIER_ERROR",
            )

        now = monotonic()
        if now >= deadline:
            return hold_receipt(
                project=project,
                version=version,
                expected_files=expected_files,
                attempts=attempts,
                elapsed_seconds=now - started,
                observation=last_observation,
                trigger="BOUNDED_VISIBILITY_TIMEOUT",
            )
        sleeper(min(poll_interval_seconds, max(0.0, deadline - now)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel-name", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--wheel-size", required=True, type=int)
    parser.add_argument("--sdist-name", required=True)
    parser.add_argument("--sdist-sha256", required=True)
    parser.add_argument("--sdist-size", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=10.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--receipt", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expected_files = expected_files_from_args(args)
    receipt_path = Path(args.receipt)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        receipt = verify_until_deadline(
            project=args.project,
            version=args.version,
            expected_files=expected_files,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
        )
    except Exception as exc:
        receipt = {
            "status": "HOLD",
            "classification": RECONCILIATION_REQUIRED,
            "doNotAutoRetry": True,
            "project": args.project,
            "version": args.version,
            "lastObservation": f"verifier failed before completing: {type(exc).__name__}: {exc}",
            "files": expected_files,
        }

    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    receipt_path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    if receipt["status"] == "PASS":
        return 0

    sys.stderr.write(f"{RECONCILIATION_REQUIRED} — {DO_NOT_AUTO_RETRY}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
