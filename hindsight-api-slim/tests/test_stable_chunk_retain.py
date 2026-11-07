"""Real isolated-store lifecycle: reuse preserves fact IDs and observation evidence."""

import asyncio
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from hindsight_api.config import _get_raw_config
from hindsight_api.engine.chunk_ids import parse_chunk_id
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.memories.pg import stable_chunks as pg_stable
from hindsight_api.engine.retain.stable_chunks import plan_stable_chunks
from tests.test_consolidation_stale_batch import cross_encoder, embeddings  # noqa: F401 (fixtures)
from tests.test_observation_invalidation import _insert_observation


def _lines(count: int = 120) -> list[str]:
    return [
        json.dumps({"role": "user", "content": f"Fixture {i:03d}: " + "durable source evidence. " * (5 + i % 3)})
        for i in range(count)
    ]


@pytest.fixture
async def bank(memory, request_context, monkeypatch):
    name = "stable-retain-" + uuid.uuid4().hex[:12]
    await memory.ensure_bank_profile(name, request_context=request_context)
    config = replace(
        _get_raw_config(),
        retain_stable_chunks=True,
        retain_chunk_size=600,
        retain_structured_chunk_size=600,
        retain_extraction_mode="chunks",
        enable_observations=False,
    )
    # A deliberately tiny splitter threshold exercises the bounded whole-document
    # seam. The old greedy later slices must not overwrite the anchored result.
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 500)
    with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
        yield name
    await memory.delete_bank(name, request_context=request_context)


async def _facts(memory, bank, document, context):
    page = await memory.list_memory_units(
        bank, document_id=document, fact_type="world", limit=1000, request_context=context
    )
    assert page["total"] == len(page["items"])
    return {item["id"]: item for item in page["items"]}


@dataclass(frozen=True)
class _IntrinsicState:
    created_at: datetime | None
    updated_at: datetime | None
    consolidated_at: datetime | None
    embedding: str | None


async def _intrinsics(memory, bank, identities):
    # The public curation views intentionally omit source creation/version times
    # and embeddings. Use the typed store seam only for these unexposed invariants.
    from hindsight_api.engine.schema import fq_table

    store = get_memories()
    async with (await memory._get_pool()).acquire() as conn:
        rows = await store.get_memories(conn=conn, fq_table=fq_table, bank_id=bank, unit_ids=list(identities))
        vectors = await store.unit_embeddings(conn=conn, fq_table=fq_table, bank_id=bank, unit_ids=list(identities))
    embeddings = {row["id"]: row["embedding"] for row in vectors}
    return {
        row.unit_id: _IntrinsicState(row.created_at, row.updated_at, row.consolidated_at, embeddings.get(row.unit_id))
        for row in rows
    }


async def _chunks(memory, bank, document, context):
    page = await memory.list_document_chunks(bank, document, limit=1000, request_context=context)
    assert page is not None and page["total"] == len(page["items"])
    return page["items"]


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("change", ["insert", "delete", "insert_append"])
async def test_reused_suffix_keeps_fact_ids_and_valid_observations(memory, request_context, bank, change):
    document = "fixture-source"
    lines = _lines()
    before_body = "\n".join(lines)
    await memory.retain_async(bank_id=bank, document_id=document, content=before_body, request_context=request_context)
    initial = await _facts(memory, bank, document, request_context)
    from hindsight_api.engine.schema import fq_table

    async with (await memory._get_pool()).acquire() as conn:
        await get_memories().mark_consolidated(
            conn=conn, fq_table=fq_table, bank_id=bank, unit_ids=list(initial), when=datetime.now(timezone.utc)
        )
    before = await _facts(memory, bank, document, request_context)
    assert len(before) > 20
    intrinsic_before = await _intrinsics(memory, bank, before)
    assert all(
        state.embedding is not None and state.created_at is not None and state.consolidated_at is not None
        for state in intrinsic_before.values()
    )
    # Seed lineage the public retain API cannot explicitly assign. Assertions below
    # still use engine reads, not SQL proxies for the observed memory state.
    suffix = next(item for item in before.values() if "Fixture 110" in item["text"])
    async with (await memory._get_pool()).acquire() as conn:
        proof = await _insert_observation(
            memory, conn, bank, "The unchanged suffix remains supported.", [uuid.UUID(suffix["id"])]
        )
    edited = list(lines)
    if change == "delete":
        del edited[40]
    else:
        edited.insert(40, json.dumps({"role": "user", "content": "A newly inserted, genuinely different fact."}))
    if change == "insert_append":
        edited.extend(_lines(8)[-4:])
    after_body = "\n".join(edited)
    created = await memory.retain_async(
        bank_id=bank, document_id=document, content=after_body, request_context=request_context
    )
    after = await _facts(memory, bank, document, request_context)
    kept = before.keys() & after.keys()
    intrinsic_after = await _intrinsics(memory, bank, kept)
    assert all(intrinsic_before[identity] == intrinsic_after[identity] for identity in kept)
    assert suffix["id"] in kept
    assert len(kept) >= len(before) - 2
    assert len(set(created)) == len(created)
    assert set(created) == after.keys() - before.keys(), "caller result must include every new chunk's facts"
    for identity in kept:
        # The listing intentionally omits storage creation/version timestamps.
        # Compare all observable intrinsic fields; only the source location may move.
        assert {k: v for k, v in before[identity].items() if k != "chunk_id"} == {
            k: v for k, v in after[identity].items() if k != "chunk_id"
        }
    assert await memory.get_memory_unit(bank, str(proof), request_context) is not None
    document_after = await memory.get_document(document, bank, request_context=request_context)
    assert document_after["original_text"] == after_body
    new_chunks = await _chunks(memory, bank, document, request_context)
    assert [c["chunk_index"] for c in new_chunks] == list(range(len(new_chunks)))
    assert "\n".join(c["chunk_text"] for c in new_chunks) == after_body
    for item in after.values():
        ref = parse_chunk_id(item["chunk_id"])
        assert ref is not None and new_chunks[ref.chunk_index]["chunk_id"] == item["chunk_id"]
        assert item["text"] == new_chunks[ref.chunk_index]["chunk_text"]
    # A normal append after a relocated replace must not restore greedy boundaries
    # and destroy the preserved suffix all over again.
    if change == "insert":
        tail = json.dumps({"role": "assistant", "content": "A final appended fixture result."})
        await memory.retain_batch_async(
            bank_id=bank,
            contents=[{"content": tail, "document_id": document, "update_mode": "append"}],
            request_context=request_context,
        )
        appended = await _facts(memory, bank, document, request_context)
        assert suffix["id"] in appended
        assert len(appended.keys() & after.keys()) >= len(after) - 1
        stored = await memory.get_document(document, bank, request_context=request_context)
        assert stored["original_text"] == after_body + "\n" + tail
    # Exact same-body retry is a no-op, not a move/re-extract loop.
    current = await memory.get_document(document, bank, request_context=request_context)
    final_before = await _facts(memory, bank, document, request_context)
    again = await memory.retain_async(
        bank_id=bank, document_id=document, content=current["original_text"], request_context=request_context
    )
    assert again == []
    assert (await _facts(memory, bank, document, request_context)).keys() == final_before.keys()


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_relocation_failure_rolls_back_source_facts_chunks_and_observations(
    memory, request_context, bank, monkeypatch
):
    document = "rollback-source"
    lines = _lines()
    body = "\n".join(lines)
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    before = await _facts(memory, bank, document, request_context)
    chunks = await _chunks(memory, bank, document, request_context)
    anchor = next(iter(before))
    async with (await memory._get_pool()).acquire() as conn:
        proof = await _insert_observation(memory, conn, bank, "Rollback keeps valid evidence.", [uuid.UUID(anchor)])

    async def fail_after_staging(**kwargs):
        assert kwargs["staged"].moves
        raise RuntimeError("fixture finalization failure")

    monkeypatch.setattr(pg_stable, "finish_reuse", fail_after_staging)
    lines.insert(40, json.dumps({"role": "user", "content": "A new insertion."}))
    with pytest.raises(RuntimeError, match="fixture finalization failure"):
        await memory.retain_async(
            bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
        )
    stored = await memory.get_document(document, bank, request_context=request_context)
    assert stored["original_text"] == body
    assert (await _facts(memory, bank, document, request_context)) == before
    assert await _chunks(memory, bank, document, request_context) == chunks
    assert await memory.get_memory_unit(bank, str(proof), request_context) is not None


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_actual_edited_and_deleted_source_still_invalidates_its_observation(memory, request_context, bank):
    document = "edited-source"
    lines = _lines()
    await memory.retain_async(
        bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
    )
    before = await _facts(memory, bank, document, request_context)
    changed = next(item for item in before.values() if "Fixture 040" in item["text"])
    async with (await memory._get_pool()).acquire() as conn:
        stale = await _insert_observation(
            memory, conn, bank, "This claim depends on the edited source.", [uuid.UUID(changed["id"])]
        )
    lines[40] = lines[40].replace("durable source evidence", "the corrected different source")
    await memory.retain_async(
        bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
    )
    assert await memory.get_memory_unit(bank, changed["id"], request_context) is None
    assert await memory.get_memory_unit(bank, str(stale), request_context) is None
    stored = await memory.get_document(document, bank, request_context=request_context)
    assert stored["original_text"] == "\n".join(lines)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_cancelled_relocation_is_invisible_and_rolls_back(memory, request_context, bank, monkeypatch):
    document = "cancelled-source"
    lines = _lines()
    body = "\n".join(lines)
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    before = await _facts(memory, bank, document, request_context)
    old_chunks = await _chunks(memory, bank, document, request_context)
    reached = asyncio.Event()
    hold = asyncio.Event()

    async def held_finish(**kwargs):
        assert kwargs["staged"].moves
        reached.set()
        await hold.wait()

    monkeypatch.setattr(pg_stable, "finish_reuse", held_finish)
    lines.insert(40, json.dumps({"role": "user", "content": "An inserted fixture."}))
    pending = asyncio.create_task(
        memory.retain_async(
            bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 10)
        # Other connections see the old committed version, never temporary FKs.
        assert (await memory.get_document(document, bank, request_context=request_context))["original_text"] == body
        assert await _chunks(memory, bank, document, request_context) == old_chunks
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert await _facts(memory, bank, document, request_context) == before
    assert await _chunks(memory, bank, document, request_context) == old_chunks


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_document_in_another_bank_is_not_reused_or_moved(memory, request_context, bank):
    other = bank + "_other"
    document = "shared_document_name"
    body = "\n".join(_lines(30))
    try:
        await memory.ensure_bank_profile(other, request_context=request_context)
        for target in (bank, other):
            await memory.retain_async(
                bank_id=target, document_id=document, content=body, request_context=request_context
            )
        foreign_facts = await _facts(memory, other, document, request_context)
        foreign_chunks = await _chunks(memory, other, document, request_context)
        lines = _lines(30)
        lines.insert(10, json.dumps({"role": "user", "content": "Only this bank changed."}))
        await memory.retain_async(
            bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
        )
        assert await _facts(memory, other, document, request_context) == foreign_facts
        assert await _chunks(memory, other, document, request_context) == foreign_chunks
        assert (await memory.get_document(document, other, request_context=request_context))["original_text"] == body
    finally:
        await memory.delete_bank(other, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_stale_same_hash_chunk_snapshot_is_rejected_without_a_write(memory, request_context, bank):
    document = "snapshot-source"
    lines = _lines(30)
    body = "\n".join(lines)
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    store = get_memories()
    backend = await memory._get_backend()
    from hindsight_api.engine.schema import fq_table

    snapshots = await store.load_stable_chunks(backend=backend, fq_table=fq_table, bank_id=bank, document_id=document)
    lines.insert(10, json.dumps({"role": "user", "content": "New fixture."}))
    plan = plan_stable_chunks(body, "\n".join(lines), snapshots, max_chars=600, structured_chunk_size=600)
    assert plan is not None and plan.moved_count
    stale = replace(
        plan, old_chunks=[replace(snapshots[0], content_hash="a different recovery snapshot"), *snapshots[1:]]
    )
    before = await _facts(memory, bank, document, request_context)
    chunks = await _chunks(memory, bank, document, request_context)
    async with (await memory._get_pool()).acquire() as conn:
        async with conn.transaction():
            await store.lock_document_hash(conn=conn, fq_table=fq_table, bank_id=bank, document_id=document)
            assert (
                await store.stage_stable_chunk_reuse(
                    conn=conn, fq_table=fq_table, bank_id=bank, document_id=document, plan=stale
                )
                is None
            )
    assert await _facts(memory, bank, document, request_context) == before
    assert await _chunks(memory, bank, document, request_context) == chunks


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("contract", ["source_context", "source_strategy"])
async def test_changed_extraction_contract_declines_reuse_without_a_write(memory, request_context, bank, contract):
    from hindsight_api.engine.schema import fq_table

    document = "contract-source"
    lines = _lines(30)
    body = "\n".join(lines)
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    store = get_memories()
    backend = await memory._get_backend()
    arguments = dict(backend=backend, fq_table=fq_table, bank_id=bank, document_id=document)
    snapshots = await store.load_stable_chunks(**arguments)
    assert snapshots
    assert await store.load_stable_chunks(**arguments, **{contract: "a different extraction contract"}) == []
    lines.insert(10, json.dumps({"role": "user", "content": "New fixture."}))
    plan = plan_stable_chunks(body, "\n".join(lines), snapshots, max_chars=600, structured_chunk_size=600)
    assert plan is not None and plan.moved_count
    before = await _facts(memory, bank, document, request_context)
    chunks = await _chunks(memory, bank, document, request_context)
    async with (await memory._get_pool()).acquire() as conn:
        async with conn.transaction():
            await store.lock_document_hash(conn=conn, fq_table=fq_table, bank_id=bank, document_id=document)
            assert (
                await store.stage_stable_chunk_reuse(
                    conn=conn,
                    fq_table=fq_table,
                    bank_id=bank,
                    document_id=document,
                    plan=replace(plan, **{contract: "a different extraction contract"}),
                )
                is None
            )
    assert await _facts(memory, bank, document, request_context) == before
    assert await _chunks(memory, bank, document, request_context) == chunks
    assert (await memory.get_document(document, bank, request_context=request_context))["original_text"] == body


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_explicit_reprocess_still_reextracts_unchanged_sources(memory, request_context, bank):
    document = "force-source"
    body = "\n".join(_lines(20))
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    before = await _facts(memory, bank, document, request_context)
    await memory.reprocess_document(bank, document, request_context=request_context)
    after = await _facts(memory, bank, document, request_context)
    assert not before.keys() & after.keys()
    assert sorted(item["text"] for item in before.values()) == sorted(item["text"] for item in after.values())
    assert (await memory.get_document(document, bank, request_context=request_context))["original_text"] == body


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_same_body_partial_recovery_does_not_skip_the_unwritten_tail(memory, request_context, bank):
    document = "partial-source"
    body = "\n".join(_lines(60))
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    complete = await _facts(memory, bank, document, request_context)
    chunks = await _chunks(memory, bank, document, request_context)
    cutoff = len(chunks) // 2
    # Force a same-hash interrupted-write state the public API cannot express:
    # full source metadata is durable, but only a prefix of chunk/fact writes is.
    from hindsight_api.engine.schema import fq_table

    async with (await memory._get_pool()).acquire() as conn:
        await get_memories().delete_chunks(
            conn=conn,
            fq_table=fq_table,
            bank_id=bank,
            chunk_ids=[chunk["chunk_id"] for chunk in chunks if chunk["chunk_index"] >= cutoff],
        )
    prefix = await _facts(memory, bank, document, request_context)
    assert 0 < len(prefix) < len(complete)
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    recovered = await _facts(memory, bank, document, request_context)
    assert prefix.keys() <= recovered.keys()
    assert sorted(x["text"] for x in recovered.values()) == sorted(x["text"] for x in complete.values())
    assert "\n".join(x["chunk_text"] for x in await _chunks(memory, bank, document, request_context)) == body


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_concurrent_append_replans_on_the_committed_source_without_losing_turns(
    memory, request_context, bank, monkeypatch
):
    from hindsight_api.engine.retain import orchestrator

    document = "append-race-source"
    body = "\n".join(_lines(30))
    await memory.retain_async(bank_id=bank, document_id=document, content=body, request_context=request_context)
    tail_a = json.dumps({"role": "user", "content": "First caller's new source turn."})
    tail_b = json.dumps({"role": "user", "content": "Concurrent caller's committed source turn."})
    real_extract = orchestrator._extract_and_embed
    raced = False

    async def extract_then_race(*args, **kwargs):
        nonlocal raced
        result = await real_extract(*args, **kwargs)
        if not raced:
            raced = True
            await memory.retain_batch_async(
                bank_id=bank,
                contents=[{"content": tail_b, "document_id": document, "update_mode": "append"}],
                request_context=request_context,
            )
        return result

    monkeypatch.setattr(orchestrator, "_extract_and_embed", extract_then_race)
    await memory.retain_batch_async(
        bank_id=bank,
        contents=[{"content": tail_a, "document_id": document, "update_mode": "append"}],
        request_context=request_context,
    )
    stored = await memory.get_document(document, bank, request_context=request_context)
    assert raced
    assert stored["original_text"] == body + "\n" + tail_b + "\n" + tail_a
    chunks = await _chunks(memory, bank, document, request_context)
    assert "\n".join(x["chunk_text"] for x in chunks) == stored["original_text"]
