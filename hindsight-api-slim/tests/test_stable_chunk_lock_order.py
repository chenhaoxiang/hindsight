"""Relocation must not hold moving facts ahead of a shared-observation sweep."""

import asyncio
import json
import uuid

import pytest

from hindsight_api.engine.memories.pg import stable_chunks as pg_stable
from hindsight_api.engine.memories.pg import writes as pg_writes
from tests.test_observation_invalidation import _insert_observation
from tests.test_stable_chunk_retain import _facts, _lines, bank, cross_encoder, embeddings  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_relocation_and_foreign_source_delete_share_the_sweep_lock_order(
    memory, request_context, bank, monkeypatch
):
    document = "moving-document"
    lines = _lines(60)
    await memory.retain_async(
        bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
    )
    before = await _facts(memory, bank, document, request_context)
    retired = next(row for row in before.values() if "Fixture 020" in row["text"])
    moving = next(row for row in before.values() if "Fixture 050" in row["text"])
    await memory.retain_async(
        bank_id=bank, document_id="foreign-document", content="A foreign source.", request_context=request_context
    )
    # Seed only the otherwise-unexposed ordering condition. A tiny random UUID
    # sorts ahead of ordinary v4 source UUIDs without sharing a global fixture ID.
    foreign = uuid.UUID(int=uuid.uuid4().int & ((1 << 64) - 1))
    assert foreign < uuid.UUID(retired["id"]) and foreign < uuid.UUID(moving["id"])
    pool = await memory._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO memory_units (id, bank_id, text, fact_type, document_id, event_date) "
            "VALUES ($1, $2, 'A foreign ordered source.', 'world', 'foreign-document', NOW())",
            foreign,
            bank,
        )
        proof = await _insert_observation(
            memory, conn, bank, "A shared claim.", [foreign, uuid.UUID(retired["id"]), uuid.UUID(moving["id"])]
        )
    entered = asyncio.Event()
    foreign_pid = None
    foreign_delete = None
    real_sweep = pg_writes.delete_stale_observations
    real_stage = pg_stable.stage_reuse

    async def observed_sweep(**kwargs):
        nonlocal foreign_pid
        if str(foreign) in {str(identity) for identity in kwargs["fact_ids"]}:
            foreign_pid = await kwargs["conn"].fetchval("SELECT pg_backend_pid()")
            entered.set()
        return await real_sweep(**kwargs)

    async def stage_then_delete_foreign(**kwargs):
        nonlocal foreign_delete
        staged = await real_stage(**kwargs)
        assert staged is not None and staged.moves
        assert any(move.original.chunk_id == moving["chunk_id"] for move in staged.moves)
        async with pool.acquire() as probe:
            async with probe.transaction():
                # Validation must not already own a writer lock on M. This fails
                # immediately on the pre-sweep UPDATE/lock from the old staging.
                assert await probe.fetchval(
                    "SELECT id FROM memory_units WHERE bank_id = $1 AND id = $2 FOR NO KEY UPDATE NOWAIT",
                    bank,
                    uuid.UUID(moving["id"]),
                ) == uuid.UUID(moving["id"])
        foreign_delete = asyncio.create_task(
            memory.delete_memory_unit(str(foreign), bank_id=bank, request_context=request_context)
        )
        await asyncio.wait_for(entered.wait(), 3)
        # In the broken implementation the foreign sweep holds F then waits for
        # moving M. Releasing T1 here forms T1->F / T2->M. In the fixed ordering
        # T1 owns no fact yet, so the ordinary sweep can finish instead.
        async with pool.acquire() as probe:
            deadline = asyncio.get_running_loop().time() + 3
            while not foreign_delete.done():
                if await probe.fetchval("SELECT cardinality(pg_blocking_pids($1)) > 0", foreign_pid):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("foreign sweep neither completed nor reached an observable lock wait")
                await asyncio.sleep(0.01)
        return staged

    monkeypatch.setattr(pg_writes, "delete_stale_observations", observed_sweep)
    monkeypatch.setattr(pg_stable, "stage_reuse", stage_then_delete_foreign)
    lines[20] = lines[20].replace("durable source evidence", "genuinely corrected evidence")
    lines.insert(20, json.dumps({"role": "user", "content": "A new historical source turn."}))
    try:
        await asyncio.wait_for(
            memory.retain_async(
                bank_id=bank, document_id=document, content="\n".join(lines), request_context=request_context
            ),
            8,
        )
        assert foreign_delete is not None
        await asyncio.wait_for(foreign_delete, 3)
    finally:
        if foreign_delete is not None and not foreign_delete.done():
            foreign_delete.cancel()
            await asyncio.gather(foreign_delete, return_exceptions=True)
    after = await _facts(memory, bank, document, request_context)
    assert moving["id"] in after and retired["id"] not in after
    assert await memory.get_memory_unit(bank, str(foreign), request_context) is None
    assert await memory.get_memory_unit(bank, str(proof), request_context) is None
