from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from factlane.contract import AdapterError
from factlane.embeddings import EmbeddingProfile
from factlane.storage import SQLiteVecEngine, _SENSITIVE_RECOVERY_INTERLOCK_KEY


def profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        profile_id="test-256",
        provider_kind="OLLAMA_LOCAL",
        base_model_identity="nomic-embed-text:latest",
        model_digest="0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
        source_dimension=768,
        output_dimension=256,
        normalization_policy="OLLAMA_API_NORMALIZED_AFTER_DIMENSION_PROJECTION",
        distance_metric="cosine",
        projection_version="ollama-dimensions-v1",
        document_prefix="search_document: ",
        query_prefix="search_query: ",
    )


_SPECIAL_PATHS = ["plain", "a#b", "a?b", "a%2Fb", "a%20b", "a b", "مرحبا_日本_Strasse"]


@pytest.mark.parametrize("dirname", _SPECIAL_PATHS)
def test_recovery_interlock_enforced_for_special_character_paths(tmp_path, dirname) -> None:
    db = tmp_path / dirname / "memory.db"

    async def scenario() -> None:
        engine = SQLiteVecEngine(str(db), profile())
        await engine.open()
        await engine.close()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO adapter_meta(key, value) VALUES (?, ?)",
            (_SENSITIVE_RECOVERY_INTERLOCK_KEY, "op-1"),
        )
        conn.commit()
        conn.close()
        before = sorted(os.listdir(tmp_path))
        with pytest.raises(AdapterError) as excinfo:
            await SQLiteVecEngine(str(db), profile()).open()
        assert excinfo.value.code == "MAINTENANCE_IN_PROGRESS"
        assert sorted(os.listdir(tmp_path)) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("dirname", _SPECIAL_PATHS[1:])
def test_existing_database_reopens_for_special_character_paths_without_interlock(tmp_path, dirname) -> None:
    db = tmp_path / dirname / "memory.db"

    async def scenario() -> None:
        engine = SQLiteVecEngine(str(db), profile())
        await engine.open()
        await engine.close()

        reopened = SQLiteVecEngine(str(db), profile())
        await reopened.open()
        await reopened.close()

    asyncio.run(scenario())
