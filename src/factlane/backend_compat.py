from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, distribution
from typing import Any

from .contract import AdapterError


PINNED_BACKEND_DISTRIBUTION = "mcp-memory-service"
PINNED_BACKEND_VERSION = "11.10.0"
PINNED_BACKEND_URL = "https://github.com/doobidoo/mcp-memory-service.git"
PINNED_BACKEND_COMMIT = "e5155b937051db4fa99a384018c5ebd621d8c5ef"


def _compatibility_error(message: str) -> AdapterError:
    return AdapterError("BACKEND_COMPATIBILITY_MISMATCH", message)


def assert_pinned_backend_identity() -> None:
    """Fail closed unless the installed backend is the exact reviewed VCS pin."""
    try:
        backend = distribution(PINNED_BACKEND_DISTRIBUTION)
    except PackageNotFoundError as exc:
        raise _compatibility_error("pinned backend distribution is unavailable") from exc

    if backend.version != PINNED_BACKEND_VERSION:
        raise _compatibility_error("pinned backend version does not match the reviewed contract")

    raw_direct_url = backend.read_text("direct_url.json")
    if not raw_direct_url:
        raise _compatibility_error("pinned backend VCS provenance is unavailable")
    try:
        direct_url = json.loads(raw_direct_url)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _compatibility_error("pinned backend VCS provenance is invalid") from exc
    if not isinstance(direct_url, dict):
        raise _compatibility_error("pinned backend VCS provenance is invalid")

    vcs_info = direct_url.get("vcs_info")
    if not isinstance(vcs_info, dict):
        raise _compatibility_error("pinned backend VCS provenance is incomplete")
    if direct_url.get("url") != PINNED_BACKEND_URL:
        raise _compatibility_error("pinned backend repository identity changed")
    if vcs_info.get("vcs") != "git":
        raise _compatibility_error("pinned backend VCS kind changed")
    if vcs_info.get("commit_id") != PINNED_BACKEND_COMMIT:
        raise _compatibility_error("pinned backend commit identity changed")
    if vcs_info.get("requested_revision") != PINNED_BACKEND_COMMIT:
        raise _compatibility_error("pinned backend requested revision changed")


def assert_backend_class_contract(storage_class: type[Any]) -> None:
    """Bind the two reviewed private primitives to their exact pinned signatures."""
    initialize = getattr(storage_class, "_initialize_embedding_model", None)
    retry = getattr(storage_class, "_execute_with_retry", None)
    if not inspect.iscoroutinefunction(initialize):
        raise _compatibility_error("pinned backend embedding initialization boundary changed")
    if not inspect.iscoroutinefunction(retry):
        raise _compatibility_error("pinned backend SQLite retry boundary changed")

    initialize_signature = inspect.signature(initialize)
    if tuple(initialize_signature.parameters) != ("self",):
        raise _compatibility_error("pinned backend embedding initialization signature changed")
    initialize_self = initialize_signature.parameters["self"]
    if initialize_self.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
        raise _compatibility_error("pinned backend embedding initialization signature changed")
    if initialize_self.default is not inspect.Parameter.empty:
        raise _compatibility_error("pinned backend embedding initialization signature changed")

    retry_signature = inspect.signature(retry)
    if tuple(retry_signature.parameters) != ("self", "operation", "max_retries", "initial_delay"):
        raise _compatibility_error("pinned backend SQLite retry signature changed")
    for name in ("self", "operation", "max_retries", "initial_delay"):
        if retry_signature.parameters[name].kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
            raise _compatibility_error("pinned backend SQLite retry signature changed")
    if retry_signature.parameters["self"].default is not inspect.Parameter.empty:
        raise _compatibility_error("pinned backend SQLite retry signature changed")
    if retry_signature.parameters["operation"].default is not inspect.Parameter.empty:
        raise _compatibility_error("pinned backend SQLite retry signature changed")
    if retry_signature.parameters["max_retries"].default != 5:
        raise _compatibility_error("pinned backend SQLite retry count changed")
    if retry_signature.parameters["initial_delay"].default != 0.2:
        raise _compatibility_error("pinned backend SQLite retry delay changed")


def load_pinned_sqlite_vec_storage() -> type[Any]:
    assert_pinned_backend_identity()
    try:
        from mcp_memory_service.storage.sqlite_vec import SqliteVecMemoryStorage
    except SystemExit as exc:
        raise _compatibility_error("pinned backend configuration terminated during import") from exc
    except Exception as exc:
        raise _compatibility_error("pinned backend SQLite storage class is unavailable") from exc
    assert_backend_class_contract(SqliteVecMemoryStorage)
    return SqliteVecMemoryStorage


def bind_deferred_embedding_initializer(
    storage: Any,
    initializer: Callable[[], Awaitable[None]],
) -> None:
    current = getattr(storage, "_initialize_embedding_model", None)
    if not inspect.iscoroutinefunction(current):
        raise _compatibility_error("pinned backend embedding initialization boundary changed")
    if inspect.signature(current).parameters:
        raise _compatibility_error("pinned backend embedding initialization signature changed")
    storage._initialize_embedding_model = initializer


async def execute_with_backend_retry(storage: Any, operation: Callable[[], Any]) -> Any:
    retry = getattr(storage, "_execute_with_retry", None)
    if not inspect.iscoroutinefunction(retry):
        raise _compatibility_error("pinned backend SQLite retry boundary changed")
    retry_signature = inspect.signature(retry)
    if tuple(retry_signature.parameters) != ("operation", "max_retries", "initial_delay"):
        raise _compatibility_error("pinned backend SQLite retry signature changed")
    for name in ("operation", "max_retries", "initial_delay"):
        if retry_signature.parameters[name].kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD:
            raise _compatibility_error("pinned backend SQLite retry signature changed")
    if retry_signature.parameters["operation"].default is not inspect.Parameter.empty:
        raise _compatibility_error("pinned backend SQLite retry signature changed")
    if retry_signature.parameters["max_retries"].default != 5:
        raise _compatibility_error("pinned backend SQLite retry count changed")
    if retry_signature.parameters["initial_delay"].default != 0.2:
        raise _compatibility_error("pinned backend SQLite retry delay changed")
    return await retry(operation)
