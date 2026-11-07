"""Postgres-only transactional relocation of unchanged chunk references.

No schema change: facts keep their UUIDs while temporary chunk rows carry their
FKs across an index permutation. Callers hold the document lock and run staging,
normal obsolete-chunk invalidation, and finalization in ONE transaction.
"""

import uuid
from collections.abc import Callable
from typing import Any

from ...chunk_ids import build_chunk_id
from ...retain.stable_chunks import (
    MAX_STABLE_CHUNKS,
    ChunkSnapshot,
    StableChunkPlan,
    StagedChunkMove,
    StagedChunkReuse,
)


async def compatible_recovery_chunks(
    *,
    conn: Any,
    fq_table: Callable[[str], str],
    bank_id: str,
    document_id: str,
    content_hash: str,
    expected_hashes: dict[int, str],
    source_context: str | None,
    source_strategy: str | None,
) -> dict[int, str] | None:
    """Only resume committed positions under the same source/extraction contract.

    A body hash does not identify a chunk layout. In particular, a stable layout
    must never be overwritten by greedy recovery while keeping its old facts.
    Call again under the document lock before writing a recovered/later batch.
    """
    matches = await conn.fetchval(
        f"SELECT 1 FROM {fq_table('documents')} WHERE bank_id = $1 AND id = $2 AND content_hash = $3 "
        "AND NULLIF(retain_params->>'context', '') IS NOT DISTINCT FROM NULLIF($4::text, '') "
        "AND NULLIF(retain_params->>'strategy', '') IS NOT DISTINCT FROM NULLIF($5::text, '')",
        bank_id,
        document_id,
        content_hash,
        source_context,
        source_strategy,
    )
    if not matches:
        return None
    rows = await conn.fetch(
        f"SELECT chunk_index, content_hash FROM {fq_table('chunks')} "
        "WHERE bank_id = $1 AND document_id = $2 ORDER BY chunk_index LIMIT $3",
        bank_id,
        document_id,
        len(expected_hashes) + 1,
    )
    if any(
        row["chunk_index"] not in expected_hashes or expected_hashes[row["chunk_index"]] != row["content_hash"]
        for row in rows
    ):
        return None
    return {row["chunk_index"]: row["content_hash"] for row in rows}


async def load_chunks(
    *,
    conn: Any,
    fq_table: Callable[[str], str],
    bank_id: str,
    document_id: str,
    source_context: str | None = None,
    source_strategy: str | None = None,
) -> list[ChunkSnapshot]:
    rows = await conn.fetch(
        f"SELECT c.chunk_id, c.chunk_index, c.content_hash, c.chunk_text, c.created_at FROM {fq_table('chunks')} c "
        f"JOIN {fq_table('documents')} d ON d.bank_id = c.bank_id AND d.id = c.document_id "
        "WHERE c.bank_id = $1 AND c.document_id = $2 "
        "AND NULLIF(d.retain_params->>'context', '') IS NOT DISTINCT FROM NULLIF($4::text, '') "
        "AND NULLIF(d.retain_params->>'strategy', '') IS NOT DISTINCT FROM NULLIF($5::text, '') "
        "ORDER BY c.chunk_index LIMIT $3",
        bank_id,
        document_id,
        MAX_STABLE_CHUNKS + 1,
        source_context,
        source_strategy,
    )
    return [
        ChunkSnapshot(
            chunk_id=row["chunk_id"],
            chunk_index=row["chunk_index"],
            content_hash=row["content_hash"],
            chunk_text=row["chunk_text"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def _lock_reuse_write_set(
    *, conn: Any, fq_table: Callable[[str], str], bank_id: str, document_id: str, plan: StableChunkPlan
) -> bool:
    """Lock the UNION, not two individually sorted writer sets.

    Include all document facts because metadata/tag synchronization can write
    stationary anchors too. Only obsolete sources select observations to retire;
    moving sources are lock-only and must not invalidate their valid evidence.
    """
    facts = await conn.fetch(
        f"SELECT id, chunk_id FROM {fq_table('memory_units')} WHERE bank_id = $1 AND document_id = $2",
        bank_id,
        document_id,
    )
    retired = set(plan.obsolete_ids)
    outgoing = [uuid.UUID(str(row["id"])) for row in facts if row["chunk_id"] in retired]

    async def sweep_write_ids() -> set[uuid.UUID]:
        if not outgoing:
            return set()
        rows = await conn.fetch(
            f"SELECT id, source_memory_ids FROM {fq_table('memory_units')} "
            "WHERE bank_id = $1 AND fact_type = 'observation' AND source_memory_ids && $2::uuid[]",
            bank_id,
            outgoing,
        )
        identities = {uuid.UUID(str(row["id"])) for row in rows}
        for row in rows:
            identities.update(uuid.UUID(str(value)) for value in (row["source_memory_ids"] or []))
        return identities

    write_ids = {uuid.UUID(str(row["id"])) for row in facts} | await sweep_write_ids()
    if write_ids:
        await conn.fetch(
            f"SELECT id FROM {fq_table('memory_units')} WHERE bank_id = $1 AND id = ANY($2::uuid[]) "
            "ORDER BY id FOR NO KEY UPDATE",
            bank_id,
            sorted(write_ids),
        )
    # Observation creation takes SHARE on its sources. A publication that won
    # while we waited may expand the set: retry without writes, never acquire an
    # extra lower UUID after holding the original set.
    return await sweep_write_ids() <= write_ids


async def stage_reuse(
    *,
    conn: Any,
    fq_table: Callable[[str], str],
    bank_id: str,
    document_id: str,
    plan: StableChunkPlan,
) -> StagedChunkReuse | None:
    """Validate the whole read snapshot before making any write; None means stale.

    A sliced recovery can change chunks while keeping the document body's hash,
    so the ordinary document CAS alone is not enough for a relocation plan.
    """
    contract = await conn.fetchrow(
        f"SELECT retain_params->>'context' AS context, retain_params->>'strategy' AS strategy "
        f"FROM {fq_table('documents')} WHERE bank_id = $1 AND id = $2",
        bank_id,
        document_id,
    )
    if (
        contract is None
        or (contract["context"] or None) != (plan.source_context or None)
        or (contract["strategy"] or None) != (plan.source_strategy or None)
    ):
        return None
    rows = await conn.fetch(
        f"SELECT chunk_id, chunk_index, content_hash, chunk_text FROM {fq_table('chunks')} "
        "WHERE bank_id = $1 AND document_id = $2 ORDER BY chunk_id FOR UPDATE",
        bank_id,
        document_id,
    )
    current = {row["chunk_id"]: row for row in rows}
    if len(current) != len(plan.old_chunks) or any(
        old.chunk_id not in current
        or current[old.chunk_id]["chunk_index"] != old.chunk_index
        or current[old.chunk_id]["content_hash"] != old.content_hash
        or current[old.chunk_id]["chunk_text"] != old.chunk_text
        for old in plan.old_chunks
    ):
        return None
    old_by_id = {old.chunk_id: old for old in plan.old_chunks}
    moving = [
        chunk
        for chunk in plan.chunks
        if chunk.reuse_from is not None and old_by_id[chunk.reuse_from].chunk_index != chunk.chunk_index
    ]
    temporary_base = max(len(plan.old_chunks), len(plan.chunks))
    moves = [
        StagedChunkMove(
            original=old_by_id[chunk.reuse_from],
            temporary_id=build_chunk_id(bank_id, document_id, temporary_base + offset),
            temporary_index=temporary_base + offset,
            target_id=build_chunk_id(bank_id, document_id, chunk.chunk_index),
            target_index=chunk.chunk_index,
        )
        for offset, chunk in enumerate(moving)
        if chunk.reuse_from is not None
    ]
    if not await _lock_reuse_write_set(
        conn=conn, fq_table=fq_table, bank_id=bank_id, document_id=document_id, plan=plan
    ):
        return None
    # No source writes yet. The complete fact/sweep write union is now held in
    # UUID order; the normal sweep and relocation can reuse those locks safely.
    return StagedChunkReuse(moves=moves)


async def _move_to_temporary(
    *, conn: Any, fq_table: Callable[[str], str], bank_id: str, document_id: str, moves: list[StagedChunkMove]
) -> int:
    old_ids = [move.original.chunk_id for move in moves]
    temp_ids = [move.temporary_id for move in moves]
    # Staging acquired the entire document/sweep write union in one UUID order.
    # The normal obsolete sweep has run; these writer locks are already owned.
    units = await conn.fetch(
        f"SELECT id FROM {fq_table('memory_units')} WHERE bank_id = $1 AND document_id = $2 "
        "AND chunk_id = ANY($3::text[]) ORDER BY id FOR NO KEY UPDATE",
        bank_id,
        document_id,
        old_ids,
    )
    await conn.execute(
        f"""INSERT INTO {fq_table("chunks")}
            (chunk_id, document_id, bank_id, chunk_index, chunk_text, created_at, content_hash)
        SELECT m.temporary_id, c.document_id, c.bank_id, m.temporary_index,
               c.chunk_text, c.created_at, c.content_hash
        FROM {fq_table("chunks")} c
        JOIN unnest($3::text[], $4::text[], $5::int[]) AS m(old_id, temporary_id, temporary_index)
          ON c.chunk_id = m.old_id
        WHERE c.bank_id = $1 AND c.document_id = $2""",
        bank_id,
        document_id,
        old_ids,
        temp_ids,
        [move.temporary_index for move in moves],
    )
    # Only a source LOCATION changed. Do not turn this bookkeeping into a content
    # edit: UUID/text/embedding/times/scopes/consolidation stamps remain untouched.
    await conn.execute(
        f"""UPDATE {fq_table("memory_units")} u SET chunk_id = m.temporary_id
        FROM unnest($3::text[], $4::text[]) AS m(old_id, temporary_id)
        WHERE u.bank_id = $1 AND u.document_id = $2 AND u.chunk_id = m.old_id""",
        bank_id,
        document_id,
        old_ids,
        temp_ids,
    )
    await conn.execute(
        f"DELETE FROM {fq_table('chunks')} WHERE bank_id = $1 AND document_id = $2 AND chunk_id = ANY($3::text[])",
        bank_id,
        document_id,
        old_ids,
    )
    return len(units)


async def finish_reuse(
    *,
    conn: Any,
    fq_table: Callable[[str], str],
    bank_id: str,
    document_id: str,
    staged: StagedChunkReuse,
) -> int:
    """Move references only AFTER the standard obsolete-source/observation sweep."""
    if not staged.moves:
        return 0
    moved_count = await _move_to_temporary(
        conn=conn, fq_table=fq_table, bank_id=bank_id, document_id=document_id, moves=staged.moves
    )
    temp_ids = [move.temporary_id for move in staged.moves]
    target_ids = [move.target_id for move in staged.moves]
    await conn.execute(
        f"""INSERT INTO {fq_table("chunks")}
            (chunk_id, document_id, bank_id, chunk_index, chunk_text, created_at, content_hash)
        SELECT m.target_id, c.document_id, c.bank_id, m.target_index,
               c.chunk_text, c.created_at, c.content_hash
        FROM {fq_table("chunks")} c
        JOIN unnest($3::text[], $4::text[], $5::int[]) AS m(temporary_id, target_id, target_index)
          ON c.chunk_id = m.temporary_id
        WHERE c.bank_id = $1 AND c.document_id = $2""",
        bank_id,
        document_id,
        temp_ids,
        target_ids,
        [move.target_index for move in staged.moves],
    )
    await conn.execute(
        f"""UPDATE {fq_table("memory_units")} u SET chunk_id = m.target_id
        FROM unnest($3::text[], $4::text[]) AS m(temporary_id, target_id)
        WHERE u.bank_id = $1 AND u.document_id = $2 AND u.chunk_id = m.temporary_id""",
        bank_id,
        document_id,
        temp_ids,
        target_ids,
    )
    await conn.execute(
        f"DELETE FROM {fq_table('chunks')} WHERE bank_id = $1 AND document_id = $2 AND chunk_id = ANY($3::text[])",
        bank_id,
        document_id,
        temp_ids,
    )
    return moved_count
