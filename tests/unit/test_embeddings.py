from __future__ import annotations

import http.server
import json
import socket
import threading
import urllib.request
from contextlib import contextmanager

import pytest

from factlane.adapter import MODEL_DIGESTS, PROFILE_DEFINITIONS
from factlane.contract import AdapterError
from factlane.embeddings import (
    OllamaLocalProvider,
    _context_length_from_model_info,
    _embedding_length_from_model_info,
)


NOMIC_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
EMBEDDINGGEMMA_DIGEST = "85462619ee721b466c5927d109d4cb765861907d5417b9109caebc4e614679f1"


def _production_provider(base_url: str) -> OllamaLocalProvider:
    return OllamaLocalProvider(
        model="embeddinggemma:300m",
        profile_id="embeddinggemma-300m-768",
        output_dimension=768,
        source_dimension=768,
        model_digest=EMBEDDINGGEMMA_DIGEST,
        document_prefix="title: none | text: ",
        query_prefix="task: search result | query: ",
        semantic_family="gemma3",
        minimum_context_window=2048,
        base_url=base_url,
        timeout=2.0,
    )


@contextmanager
def _json_server(*, ipv6: bool = False, responder=None):
    requests: list[tuple[str, str, bytes]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            requests.append((self.command, self.path, body))
            if responder is not None:
                status, headers, payload = responder(self.command, self.path, body)
            elif self.path == "/api/version":
                status, headers, payload = 200, {}, {"version": "0.22.1"}
            elif self.path == "/api/tags":
                status, headers, payload = 200, {}, {
                    "models": [
                        {
                            "name": "embeddinggemma:300m",
                            "digest": "b" * 64,
                            "details": {"family": "gemma3"},
                        }
                    ]
                }
            elif self.path == "/api/show":
                status, headers, payload = 200, {}, {
                    "model_info": {"gemma3.embedding_length": 768, "gemma3.context_length": 2048},
                    "capabilities": ["embedding"],
                }
            elif self.path == "/api/embed":
                request_payload = json.loads(body.decode("utf-8"))
                inputs = request_payload["input"]
                dimensions = request_payload["dimensions"]
                unit = [1.0] + [0.0] * (dimensions - 1)
                status, headers, payload = 200, {}, {"embeddings": [unit[:] for _ in inputs]}
            else:
                status, headers, payload = 404, {}, {"error": "not found"}
            encoded = json.dumps(payload).encode("utf-8") if isinstance(payload, dict) else payload
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _handle
        do_POST = _handle

    server_type = http.server.ThreadingHTTPServer
    host = "127.0.0.1"
    if ipv6:
        class IPv6Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_type = IPv6Server
        host = "::1"
    try:
        server = server_type((host, 0), Handler)
    except OSError:
        if ipv6:
            pytest.skip("IPv6 loopback binding is unavailable on this host")
        raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://[{host}]:{port}" if ipv6 else f"http://{host}:{port}"
        yield url, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_production_request_path_reaches_all_ollama_endpoints_on_ipv4_loopback() -> None:
    with _json_server() as (url, requests):
        provider = _production_provider(url)
        status = provider.provider_status()
        vectors = provider.embed_documents(["alpha"])
    assert status["ollama_version"] == "0.22.1"
    assert len(vectors) == 1 and len(vectors[0]) == 768
    assert [(method, path) for method, path, _ in requests] == [
        ("GET", "/api/version"),
        ("GET", "/api/tags"),
        ("POST", "/api/show"),
        ("POST", "/api/embed"),
    ]


def test_production_request_path_supports_bracketed_ipv6_loopback() -> None:
    with _json_server(ipv6=True) as (url, requests):
        provider = _production_provider(url)
        assert provider._request("/api/version")["version"] == "0.22.1"
    assert requests == [("GET", "/api/version", b"")]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("location_kind", ["relative", "alternate_loopback"])
def test_production_request_path_rejects_redirects_without_followup(
    code: int,
    method: str,
    location_kind: str,
) -> None:
    target_requests: list[tuple[str, str, bytes]] = []
    with _json_server() as (alternate_url, alternate_requests):
        target_requests = alternate_requests

        def responder(_method: str, path: str, _body: bytes):
            assert path == "/api/version" if method == "GET" else path == "/api/embed"
            location = "/redirect-target" if location_kind == "relative" else f"{alternate_url}/redirect-target"
            return code, {"Location": location}, {"redirect": True}

        with _json_server(responder=responder) as (url, source_requests):
            provider = _production_provider(url)
            with pytest.raises(AdapterError) as error:
                if method == "GET":
                    provider._request("/api/version")
                else:
                    provider._request("/api/embed", {"input": ["alpha"], "dimensions": 768})
            assert error.value.code == "EMBEDDING_UNAVAILABLE"
        assert len(source_requests) == 1
    assert target_requests == []


def test_provider_owned_opener_ignores_hostile_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    with _json_server() as (proxy_url, proxy_requests):
        with _json_server() as (origin_url, origin_requests):
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                monkeypatch.setenv(name, proxy_url)
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("no_proxy", "")
            provider = _production_provider(origin_url)
            assert provider._request("/api/version")["version"] == "0.22.1"
    assert [(method, path) for method, path, _ in origin_requests] == [("GET", "/api/version")]
    assert proxy_requests == []


@pytest.mark.parametrize("replace_before_construction", [False, True])
def test_provider_owned_opener_does_not_use_process_global_opener(
    monkeypatch: pytest.MonkeyPatch,
    replace_before_construction: bool,
) -> None:
    class FailingGlobalOpener:
        def open(self, *_args, **_kwargs):
            raise AssertionError("process-global opener must not be used")

    with _json_server() as (url, requests):
        if replace_before_construction:
            monkeypatch.setattr(urllib.request, "_opener", FailingGlobalOpener())
        provider = _production_provider(url)
        if not replace_before_construction:
            monkeypatch.setattr(urllib.request, "_opener", FailingGlobalOpener())
        assert provider._request("/api/version")["version"] == "0.22.1"
    assert requests == [("GET", "/api/version", b"")]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "http://127.0.0.2:11434",
        "http://[::2]:11434",
        "http://[::1%25lo]:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://[::1]junk:11434",
        "http://127.0.0.1/path",
        "http://127.0.0.1/;param",
        "http://127.0.0.1/;",
        "http://127.0.0.1/?query=value",
        "http://127.0.0.1/#fragment",
        "http://127.0.0.1/?",
        "http://127.0.0.1/#",
        " http://127.0.0.1:11434",
        "http://127.0.0.1:11434\n",
        "http://127.0.0.1\t:11434",
    ],
)
def test_invalid_provider_url_fails_before_opener_construction(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    opener_calls = 0

    def unexpected_build_opener(*_args, **_kwargs):
        nonlocal opener_calls
        opener_calls += 1
        raise AssertionError("invalid URL must fail before transport construction")

    monkeypatch.setattr(urllib.request, "build_opener", unexpected_build_opener)
    with pytest.raises(AdapterError) as error:
        _production_provider(base_url)
    assert error.value.code == "INVALID_ENVELOPE"
    assert opener_calls == 0


class RecordingProvider(OllamaLocalProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.requests: list[tuple[str, dict[str, object] | None]] = []

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.requests.append((path, payload))
        if path == "/api/embed":
            assert payload is not None
            dimensions = int(payload["dimensions"])
            inputs = payload["input"]
            assert isinstance(inputs, list)
            unit = [1.0] + [0.0] * (dimensions - 1)
            return {"embeddings": [unit[:] for _ in range(len(inputs))]}
        raise AssertionError(path)


def make_provider(provider_type=RecordingProvider):
    return provider_type(
        model="nomic-embed-text:latest",
        profile_id="nomic-256",
        output_dimension=256,
        source_dimension=768,
        model_digest=NOMIC_DIGEST,
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


def test_nomic_prefixes_are_applied_exactly_once() -> None:
    provider = make_provider()
    provider.embed_documents(["alpha"])
    provider.embed_query("beta")
    assert provider.requests[0][1]["input"] == ["search_document: alpha"]
    assert provider.requests[1][1]["input"] == ["search_query: beta"]


def test_embed_requests_disable_provider_truncation() -> None:
    provider = make_provider()
    provider.embed_documents(["alpha"])
    assert provider.requests[0][1]["truncate"] is False


def test_context_length_prefers_explicit_nomic_key() -> None:
    assert _context_length_from_model_info({"nomic-bert.context_length": 2048}) == 2048


def test_context_length_accepts_suffix_fallback() -> None:
    assert _context_length_from_model_info({"custom.encoder.max_position_embeddings": 4096}) == 4096


def test_context_length_missing_is_unknown() -> None:
    assert _context_length_from_model_info({"nomic-bert.embedding_length": 768}) is None


def test_embedding_length_single_architecture_key_is_supported() -> None:
    assert _embedding_length_from_model_info({"gemma3.embedding_length": 768}) == 768


def test_embedding_length_missing_is_unknown() -> None:
    assert _embedding_length_from_model_info({"gemma3.context_length": 2048}) is None


def test_embedding_length_conflict_is_fail_closed() -> None:
    assert _embedding_length_from_model_info({"gemma3.embedding_length": 768, "bert.embedding_length": 384}) is None


def test_embedding_length_legacy_nomic_and_bert_keys_are_unchanged() -> None:
    assert _embedding_length_from_model_info({"nomic-bert.embedding_length": 768}) == 768
    assert _embedding_length_from_model_info({"bert.embedding_length": 384}) == 384


def test_selected_embeddinggemma_profile_definition_is_exact() -> None:
    definition = PROFILE_DEFINITIONS["embeddinggemma-300m-768"]
    assert MODEL_DIGESTS["embeddinggemma:300m"] == EMBEDDINGGEMMA_DIGEST
    assert definition == {
        "model": "embeddinggemma:300m",
        "output_dimension": 768,
        "source_dimension": 768,
        "document_prefix": "title: none | text: ",
        "query_prefix": "task: search result | query: ",
        "semantic_family": "gemma3",
        "minimum_context_window": 2048,
    }


class StatusProvider(OllamaLocalProvider):
    def __init__(self, *, version: str = "0.34.2", digest: str = "a" * 64, family: str = "gemma3") -> None:
        super().__init__(
            model="embeddinggemma:300m",
            profile_id="embeddinggemma-300m-768",
            output_dimension=768,
            source_dimension=768,
            model_digest=EMBEDDINGGEMMA_DIGEST,
            document_prefix="title: none | text: ",
            query_prefix="task: search result | query: ",
            semantic_family="gemma3",
            minimum_context_window=2048,
        )
        self.version = version
        self.digest = digest
        self.family = family

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        if path == "/api/version":
            return {"version": self.version}
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": self.model,
                        "digest": self.digest,
                        "size": 123,
                        "details": {"family": self.family},
                    }
                ]
            }
        if path == "/api/show":
            return {
                "model_info": {"gemma3.embedding_length": 768, "gemma3.context_length": 2048},
                "capabilities": ["embedding"],
            }
        raise AssertionError(path)


def test_provider_status_treats_digest_and_version_as_runtime_provenance() -> None:
    provider = StatusProvider(version="0.34.2", digest="b" * 64)
    configured_digest = provider.profile.model_digest
    status = provider.provider_status()
    assert status["ollama_version"] == "0.34.2"
    assert status["digest"] == "b" * 64
    assert status["semantic_family"] == "gemma3"
    assert provider.profile.model_digest == configured_digest


def test_provider_status_rejects_unsupported_ollama_version() -> None:
    provider = StatusProvider(version="0.22.0")
    with pytest.raises(AdapterError) as error:
        provider.provider_status()
    assert error.value.code == "EMBEDDING_UNAVAILABLE"


def test_provider_status_rejects_semantic_family_drift() -> None:
    provider = StatusProvider(family="different-family")
    with pytest.raises(AdapterError) as error:
        provider.provider_status()
    assert error.value.code == "SCHEMA_MISMATCH"


class RejectingProvider(OllamaLocalProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.payloads: list[dict[str, object]] = []

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        if path != "/api/embed":
            raise AssertionError(path)
        assert payload is not None
        self.payloads.append(payload)
        # Mirror the production _request contract after an Ollama error envelope.
        raise AdapterError("EMBEDDING_UNAVAILABLE", "local embedding provider rejected the request")


def test_provider_rejection_does_not_retry_with_truncation() -> None:
    provider = make_provider(RejectingProvider)
    with pytest.raises(AdapterError) as error:
        provider.embed_documents(["alpha"])
    assert error.value.code == "EMBEDDING_UNAVAILABLE"
    assert len(provider.payloads) == 1
    assert provider.payloads[0]["truncate"] is False
