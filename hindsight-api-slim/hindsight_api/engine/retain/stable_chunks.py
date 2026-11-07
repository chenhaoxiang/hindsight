"""Bounded, source-ordered reuse of complete JSONL chunks.

The ordinary greedy splitter remains unchanged. A historical insertion shifts its
boundaries, however, so re-running it over the whole body needlessly replaces the
facts of the unchanged suffix. This planner anchors complete, unique old chunks
and cuts only the gaps. Persistence must relocate their references atomically.
"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from . import attachment_content

MAX_STABLE_SOURCE_CHARS = 2 * 1024 * 1024
MAX_STABLE_CHUNKS = 512


@dataclass(frozen=True)
class ChunkSnapshot:
    chunk_id: str
    chunk_index: int
    content_hash: str | None
    chunk_text: str
    created_at: datetime | None = None


@dataclass(frozen=True)
class PlannedChunk:
    chunk_index: int
    text: str
    reuse_from: str | None = None


@dataclass(frozen=True)
class ChunkReuseStats:
    old_chunks: int
    new_chunks: int
    reused_chunks: int
    moved_chunks: int
    retired_chunks: int
    extraction_chunks: int
    extraction_chars: int
    moved_fact_refs: int | None = None


@dataclass(frozen=True)
class StableChunkPlan:
    old_chunks: list[ChunkSnapshot]
    chunks: list[PlannedChunk]
    source_context: str | None = None
    source_strategy: str | None = None

    @property
    def texts(self) -> dict[int, str]:
        return {chunk.chunk_index: chunk.text for chunk in self.chunks}

    @property
    def reused_ids(self) -> set[str]:
        return {chunk.reuse_from for chunk in self.chunks if chunk.reuse_from is not None}

    @property
    def obsolete_ids(self) -> list[str]:
        reused = self.reused_ids
        return [chunk.chunk_id for chunk in self.old_chunks if chunk.chunk_id not in reused]

    @property
    def extraction_indices(self) -> list[int]:
        return [chunk.chunk_index for chunk in self.chunks if chunk.reuse_from is None]

    @property
    def stats(self) -> ChunkReuseStats:
        return ChunkReuseStats(
            old_chunks=len(self.old_chunks),
            new_chunks=len(self.chunks),
            reused_chunks=len(self.reused_ids),
            moved_chunks=self.moved_count,
            retired_chunks=len(self.obsolete_ids),
            extraction_chunks=len(self.extraction_indices),
            extraction_chars=sum(len(chunk.text) for chunk in self.chunks if chunk.reuse_from is None),
        )

    @property
    def moved_count(self) -> int:
        indices = {chunk.chunk_id: chunk.chunk_index for chunk in self.old_chunks}
        return sum(
            chunk.reuse_from is not None and indices[chunk.reuse_from] != chunk.chunk_index for chunk in self.chunks
        )


@dataclass(frozen=True)
class StagedChunkMove:
    original: ChunkSnapshot
    temporary_id: str
    temporary_index: int
    target_id: str
    target_index: int


@dataclass(frozen=True)
class StagedChunkReuse:
    moves: list[StagedChunkMove]


def is_jsonl_source(text: str, minimum_lines: int = 2) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < minimum_lines:
        return False
    try:
        return all(isinstance(json.loads(line), dict) for line in lines)
    except (ValueError, TypeError):
        return False


def fits_stable_chunk_budget(text: str, max_chars: int, structured_chunk_size: int | None) -> bool:
    """Stop at the first over-budget chunk before bypassing the outer splitter."""
    from . import fact_extraction

    if len(text) > MAX_STABLE_SOURCE_CHARS:
        return False
    for index, _chunk in enumerate(
        fact_extraction.iter_chunks(text, max_chars, structured_chunk_size=structured_chunk_size)
    ):
        if index >= MAX_STABLE_CHUNKS:
            return False
    return True


def _at_line_boundaries(body: str, start: int, end: int) -> bool:
    return (start == 0 or body[start - 1] == "\n") and (end == len(body) or body[end] == "\n")


def plan_stable_chunks(
    old_body: str | None,
    new_body: str,
    old_chunks: list[ChunkSnapshot],
    *,
    max_chars: int,
    structured_chunk_size: int | None,
    source_context: str | None = None,
    source_strategy: str | None = None,
) -> StableChunkPlan | None:
    """Plan exact anchors, or decline to the existing path without changing data.

    Repeated old chunks and ambiguous new occurrences are not interchangeable:
    their facts have distinct provenance. Partial JSON turns and attachments also
    keep the ordinary path rather than crossing extraction/vision boundaries.
    Work is bounded and the caller runs it outside the serving event loop.
    """
    from . import fact_extraction

    if (
        not old_body
        or max(len(old_body), len(new_body)) > MAX_STABLE_SOURCE_CHARS
        or not old_chunks
        or len(old_chunks) > MAX_STABLE_CHUNKS
        or max_chars <= 0
        or not is_jsonl_source(old_body)
        or not is_jsonl_source(new_body)
        or attachment_content.contains_attachment(old_body)
        or attachment_content.contains_attachment(new_body)
    ):
        return None
    old_chunks = sorted(old_chunks, key=lambda chunk: chunk.chunk_index)
    if [chunk.chunk_index for chunk in old_chunks] != list(range(len(old_chunks))):
        return None
    # A completed exact-body retry already has occurrence identity and complete
    # coverage. Keep even tiny tails/repeated chunks in place; anchor heuristics
    # must not make it look like an interrupted write. A partial recovery's chunk
    # prefix does not cover the stored full body and cannot take this shortcut.
    if old_body == new_body and "\n".join(chunk.chunk_text for chunk in old_chunks) == old_body:
        try:
            verified = all(
                chunk.content_hash == hashlib.sha256(chunk.chunk_text.encode()).hexdigest() for chunk in old_chunks
            )
        except UnicodeEncodeError:
            return None
        if verified:
            return StableChunkPlan(
                old_chunks=old_chunks,
                source_context=source_context,
                source_strategy=source_strategy,
                chunks=[
                    PlannedChunk(chunk_index=chunk.chunk_index, text=chunk.chunk_text, reuse_from=chunk.chunk_id)
                    for chunk in old_chunks
                ],
            )
    frequencies = Counter(chunk.content_hash for chunk in old_chunks)
    plan: list[PlannedChunk] = []
    old_cursor = 0
    new_cursor = 0

    def append_gap(start: int, end: int) -> None:
        text = new_body[start:end].strip("\n")
        if not text.strip():
            return
        for chunk in fact_extraction.iter_chunks(text, max_chars, structured_chunk_size=structured_chunk_size):
            if chunk:
                plan.append(PlannedChunk(chunk_index=len(plan), text=chunk))

    for old in old_chunks:
        text = old.chunk_text
        try:
            actual_hash = hashlib.sha256(text.encode()).hexdigest()
        except UnicodeEncodeError:
            return None
        if not old.content_hash or actual_hash != old.content_hash:
            return None
        if not text or frequencies[old.content_hash] != 1:
            continue
        if fact_extraction.chunk_text(text, max_chars, structured_chunk_size=structured_chunk_size) != [text]:
            continue
        old_start = old_body.find(text)
        if old_start < old_cursor or old_body.find(text, old_start + 1) >= 0:
            continue
        old_end = old_start + len(text)
        old_cursor = old_end
        if not _at_line_boundaries(old_body, old_start, old_end):
            continue
        start = new_body.find(text)
        if start < new_cursor or new_body.find(text, start + 1) >= 0:
            continue
        end = start + len(text)
        if not _at_line_boundaries(new_body, start, end):
            continue
        # Repack a growing tail so repeated tiny appends do not each add a chunk.
        if old.chunk_index == len(old_chunks) - 1 and not old_body[old_end:].strip() and new_body[end:].strip():
            continue
        append_gap(new_cursor, start)
        plan.append(PlannedChunk(chunk_index=len(plan), text=text, reuse_from=old.chunk_id))
        new_cursor = end
    append_gap(new_cursor, len(new_body))
    result = StableChunkPlan(
        old_chunks=old_chunks, chunks=plan, source_context=source_context, source_strategy=source_strategy
    )
    if not result.reused_ids or len(plan) > MAX_STABLE_CHUNKS:
        return None
    return result
