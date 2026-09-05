from __future__ import annotations

from dataclasses import dataclass

from .contract import (
    INTENT_CLASSES,
    RETRIEVAL_MODES,
    AdapterError,
    ScopeContext,
    supported_values,
)


@dataclass(frozen=True)
class RouteDecision:
    status: str
    intent_class: str
    operation: str
    scope: ScopeContext | None
    retrieval_mode: str
    reason: str


class TruthRouter:
    """Small decision boundary; storage and authority remain in the adapter."""

    def decide(
        self,
        *,
        intent_class: str,
        operation: str,
        scope: ScopeContext | None,
        direct_truth_available: bool = False,
        user_supplied: bool = False,
        retrieval_mode: str = "CURRENT",
    ) -> RouteDecision:
        if intent_class not in INTENT_CLASSES:
            raise AdapterError(
                "UNKNOWN_INTENT",
                f"intent class is not supported; choose one of: {supported_values(INTENT_CLASSES)}",
            )
        if operation not in {"memory_search", "memory_get", "memory_store", "memory_update", "memory_status"}:
            raise AdapterError(
                "INVALID_ENVELOPE",
                "operation is not part of the five-operation surface; use one of: "
                "memory_search, memory_get, memory_store, memory_update, memory_status",
            )
        if retrieval_mode not in RETRIEVAL_MODES:
            raise AdapterError(
                "INVALID_ENUM",
                f"retrieval mode is invalid; choose one of: {supported_values(RETRIEVAL_MODES)}",
            )
        if intent_class == "GENERAL_TASK_NO_MEMORY_REQUIRED":
            return RouteDecision("NO_MEMORY_NEEDED", intent_class, operation, scope, retrieval_mode, "self-contained task")
        if direct_truth_available:
            return RouteDecision("NO_MEMORY_NEEDED", intent_class, operation, scope, retrieval_mode, "direct current source answered")
        if user_supplied and operation == "memory_search":
            return RouteDecision("NO_MEMORY_NEEDED", intent_class, operation, scope, retrieval_mode, "current request supplied the needed context")
        if scope is None:
            raise AdapterError("UNKNOWN_SCOPE", "exact scope is required before memory access")
        if intent_class == "HISTORICAL_QUESTION" and retrieval_mode != "REVIEW_HISTORY":
            raise AdapterError("INVALID_ENVELOPE", "historical questions require REVIEW_HISTORY")
        return RouteDecision("ROUTE_MEMORY", intent_class, operation, scope, retrieval_mode, "bounded memory lookup")
