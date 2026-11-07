"""Stable layouts must survive recovery or retire incompatible sources safely."""

import json
import uuid
from dataclasses import replace
from unittest.mock import patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.schema import fq_table
from tests.test_observation_invalidation import _insert_observation
from tests.test_stable_chunk_retain import _chunks, _facts, bank, cross_encoder, embeddings  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("fallback", ["context", "strategy", "disabled", "partial"])
async def test_same_body_fallback_never_overwrites_a_surviving_facts_source(
    memory, request_context, bank, monkeypatch, fallback
):
    config = await memory._config_resolver.resolve_full_config(bank, request_context)
    config = replace(config, retain_chunk_size=110, retain_structured_chunk_size=110)
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 20)
    document = "layout-fallback"
    lines = [json.dumps({"role": "user", "content": letter + "x" * 20}) for letter in "ABCDEFGH"]
    body = "\n".join(lines)
    with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
        await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
        lines.insert(3, json.dumps({"role": "user", "content": "X" + "x" * 20}))
        body = "\n".join(lines)
        await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
        before = await _facts(memory, bank, document, request_context)
        chunks = await _chunks(memory, bank, document, request_context)
        assert len(chunks) == 5
        kept = next(row for row in before.values() if row["text"] == lines[4])
        async with (await memory._get_pool()).acquire() as conn:
            proof = await _insert_observation(
                memory, conn, bank, "The unchanged D turn is still supported.", [uuid.UUID(kept["id"])]
            )
        arguments = {}
        if fallback == "context":
            arguments["context"] = "a genuinely different extraction context"
        elif fallback == "strategy":
            arguments["strategy"] = "fixture-strategy"
        elif fallback == "disabled":
            config = replace(config, retain_stable_chunks=False)
        else:
            async with (await memory._get_pool()).acquire() as conn:
                await get_memories().delete_chunks(
                    conn=conn,
                    fq_table=fq_table,
                    bank_id=bank,
                    chunk_ids=[row["chunk_id"] for row in chunks if row["chunk_index"] >= 3],
                )
        with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
            await memory.retain_batch_async(
                bank_id=bank,
                contents=[dict(content=body, document_id=document, **arguments)],
                strategy=arguments.get("strategy"),
                request_context=request_context,
            )
    after = await _facts(memory, bank, document, request_context)
    final_chunks = await _chunks(memory, bank, document, request_context)
    by_id = {row["chunk_id"]: row["chunk_text"] for row in final_chunks}
    assert "\n".join(row["chunk_text"] for row in final_chunks) == body
    assert len(after) == len(final_chunks), "chunks extraction has exactly one fact per committed source chunk"
    assert all(row["text"] == by_id[row["chunk_id"]] for row in after.values())
    if fallback == "partial":
        assert kept["id"] in after
        assert await memory.get_memory_unit(bank, str(proof), request_context) is not None
    else:
        assert await memory.get_memory_unit(bank, str(proof), request_context) is None


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_partial_recovery_skips_positions_not_every_occurrence_of_a_hash(
    memory, request_context, bank, monkeypatch
):
    config = replace(
        await memory._config_resolver.resolve_full_config(bank, request_context),
        retain_chunk_size=110,
        retain_structured_chunk_size=110,
    )
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 20)
    lines = [json.dumps({"role": "user", "content": letter + "x" * 20}) for letter in "ABABCD"]
    body = "\n".join(lines)
    document = "duplicate-recovery"
    with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
        await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    before = await _facts(memory, bank, document, request_context)
    chunks = await _chunks(memory, bank, document, request_context)
    assert len(chunks) == 3 and chunks[0]["chunk_text"] == chunks[1]["chunk_text"]
    async with (await memory._get_pool()).acquire() as conn:
        await get_memories().delete_chunks(
            conn=conn, fq_table=fq_table, bank_id=bank, chunk_ids=[chunks[1]["chunk_id"]]
        )
    surviving = await _facts(memory, bank, document, request_context)
    with patch.object(
        memory._config_resolver, "resolve_full_config", return_value=replace(config, retain_stable_chunks=False)
    ):
        await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    after = await _facts(memory, bank, document, request_context)
    assert len(after) == 3 and surviving.keys() <= after.keys()
    assert len(after.keys() - before.keys()) == 1
    assert sorted(row["text"] for row in after.values()) == sorted(row["chunk_text"] for row in chunks)
