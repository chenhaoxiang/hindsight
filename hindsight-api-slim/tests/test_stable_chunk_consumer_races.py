"""Every streaming writer must keep source CAS and typed append-conflict recovery."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.retain import orchestrator
from hindsight_api.engine.retain.types import ConcurrentAppendConflict
from hindsight_api.engine.schema import fq_table
from hindsight_api.worker.backpressure import is_append_conflict
from tests.test_stable_chunk_retain import _chunks, _facts, bank, cross_encoder, embeddings  # noqa: F401


def _turns(letters: str) -> list[str]:
    return [json.dumps({"role": "user", "content": letter + "x" * 20}) for letter in letters]


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_zero_fact_recovery_cannot_write_an_old_body_over_a_concurrent_winner(
    memory, request_context, bank, monkeypatch
):
    config = replace(
        await memory._config_resolver.resolve_full_config(bank, request_context),
        retain_chunk_size=110,
        retain_structured_chunk_size=110,
    )
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 20)
    document = "zero-fact-race"
    lines = _turns("ABCDEFGH")
    with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
        await memory.retain_async(
            bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
        )
        lines.insert(3, _turns("X")[0])
        body = "\n".join(lines)
        await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
        chunks = await _chunks(memory, bank, document, request_context)
        async with (await memory._get_pool()).acquire() as conn:
            await get_memories().delete_chunks(
                conn=conn,
                fq_table=fq_table,
                bank_id=bank,
                chunk_ids=[row["chunk_id"] for row in chunks if row["chunk_index"] >= 3],
            )
        winner = "\n".join(json.dumps({"role": "user", "content": f"WINNER_SOURCE {index}"}) for index in range(2))
        real_extract = orchestrator._extract_and_embed
        raced = False
        committed = asyncio.Event()

        async def zero_facts_after_winner(*args, **kwargs):
            nonlocal raced
            incoming = args[0]
            result = await real_extract(*args, **kwargs)
            if any("WINNER_SOURCE" in item.content for item in incoming):
                return result
            if not raced:
                raced = True
                await memory.retain_async(
                    bank_id=bank, document_id=document, content=winner, request_context=request_context
                )
                committed.set()
            await asyncio.wait_for(committed.wait(), 3)
            return replace(
                result,
                extracted_facts=[],
                processed_facts=[],
                chunks=[replace(chunk, fact_count=0) for chunk in result.chunks],
            )

        monkeypatch.setattr(orchestrator, "_extract_and_embed", zero_facts_after_winner)
        with pytest.raises(RuntimeError, match="layout or extraction contract"):
            await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    assert raced
    assert (await memory.get_document(document, bank, request_context=request_context))["original_text"] == winner
    final_chunks = await _chunks(memory, bank, document, request_context)
    sources = {row["chunk_id"]: row["chunk_text"] for row in final_chunks}
    assert all(
        row["text"] == sources[row["chunk_id"]]
        for row in (await _facts(memory, bank, document, request_context)).values()
    )


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_later_streaming_append_conflict_retries_against_the_winning_source(
    memory, request_context, bank, monkeypatch
):
    config = replace(
        await memory._config_resolver.resolve_full_config(bank, request_context),
        retain_chunk_size=110,
        retain_structured_chunk_size=110,
        retain_chunk_batch_size=1,
    )
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 10000)
    document = "later-append-race"
    tail = "\n".join(_turns("CDEF"))
    winner = "\n".join(_turns("YZ"))
    with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
        await memory.retain_async(
            bank_id=bank, document_id=document, content="\n".join(_turns("AB")), request_context=request_context
        )
        store = get_memories()
        real_lock = store.lock_document_for_write
        calls = 0
        raced = False
        conflicts = []

        class ObservedAppendConflict(ConcurrentAppendConflict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                conflicts.append(self)

        async def advance_before_the_second_lock(**kwargs):
            nonlocal calls, raced
            if kwargs["document_id"] == document and not raced:
                calls += 1
                if calls == 2:
                    raced = True
                    await memory.retain_async(
                        bank_id=bank,
                        document_id=document,
                        content=winner,
                        context="different extraction context",
                        request_context=request_context,
                    )
            return await real_lock(**kwargs)

        monkeypatch.setattr(store, "lock_document_for_write", advance_before_the_second_lock)
        monkeypatch.setattr(orchestrator, "ConcurrentAppendConflict", ObservedAppendConflict)
        await memory.retain_batch_async(
            bank_id=bank,
            contents=[
                {
                    "content": tail,
                    "document_id": document,
                    "update_mode": "append",
                    "context": "different extraction context",
                }
            ],
            request_context=request_context,
        )
    assert raced and conflicts
    assert any("streaming write" in str(error) for error in conflicts)
    assert all(is_append_conflict(error) for error in conflicts)
    assert (await memory.get_document(document, bank, request_context=request_context))[
        "original_text"
    ] == winner + "\n" + tail
    chunks = await _chunks(memory, bank, document, request_context)
    sources = {row["chunk_id"]: row["chunk_text"] for row in chunks}
    assert all(
        row["text"] == sources[row["chunk_id"]]
        for row in (await _facts(memory, bank, document, request_context)).values()
    )
