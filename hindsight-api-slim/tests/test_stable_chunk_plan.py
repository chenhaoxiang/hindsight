"""Exact source anchors avoid the greedy suffix cascade without conflating copies."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hindsight_api.engine.memories.base import MemoriesExtension
from hindsight_api.engine.memories.postgres import PostgresMemories
from hindsight_api.engine.retain.fact_extraction import chunk_text
from hindsight_api.engine.retain.stable_chunks import MAX_STABLE_SOURCE_CHARS, ChunkSnapshot, plan_stable_chunks


def _line(index: int) -> str:
    return json.dumps(
        {"role": "user", "content": f"Fixture {index}: " + "source evidence unchanged. " * (4 + index % 5)}
    )


def _snapshot(body: str, size: int) -> list[ChunkSnapshot]:
    return [
        ChunkSnapshot(f"bank_doc_{index}", index, hashlib.sha256(text.encode()).hexdigest(), text)
        for index, text in enumerate(chunk_text(body, size, structured_chunk_size=size))
    ]


@pytest.mark.parametrize("size", [3000, 12000])
@pytest.mark.parametrize("change", ["unchanged", "append", "edit", "insert", "delete", "insert_append"])
def test_exact_anchors_keep_the_unchanged_suffix(size: int, change: str) -> None:
    before = [_line(i) for i in range(2400)]
    after = list(before)
    if change == "edit":
        after[900] = after[900].replace("unchanged", "corrected", 1)
    elif change in {"insert", "insert_append"}:
        after.insert(900, _line(9000))
    elif change == "delete":
        del after[900]
    if change in {"append", "insert_append"}:
        after.extend(_line(i) for i in range(2400, 2450))
    old_body, new_body = "\n".join(before), "\n".join(after)
    old = _snapshot(old_body, size)
    plan = plan_stable_chunks(old_body, new_body, old, max_chars=size, structured_chunk_size=size)
    assert plan is not None
    assert "\n".join(chunk.text for chunk in plan.chunks) == new_body
    assert len(plan.reused_ids) >= len(old) - 2
    assert len(plan.obsolete_ids) <= 2
    assert len(plan.reused_ids) == sum(chunk.reuse_from is not None for chunk in plan.chunks)
    assert all(chunk_text(chunk.text, size, structured_chunk_size=size) == [chunk.text] for chunk in plan.chunks)
    if change in {"insert", "insert_append"}:
        assert plan.moved_count > 0
    if change == "unchanged":
        assert plan.extraction_indices == [] and plan.obsolete_ids == [] and plan.moved_count == 0


def test_repeated_old_chunks_are_never_matched_as_a_hash_set() -> None:
    texts = [_line(1), _line(2), _line(2), _line(3)]
    body = "\n".join(texts)
    old = [
        ChunkSnapshot(f"bank_doc_{i}", i, hashlib.sha256(text.encode()).hexdigest(), text)
        for i, text in enumerate(texts)
    ]
    new_body = "\n".join([texts[0], _line(9), *texts[1:]])
    plan = plan_stable_chunks(body, new_body, old, max_chars=400, structured_chunk_size=400)
    assert plan is not None
    assert plan.reused_ids == {old[0].chunk_id, old[3].chunk_id}
    assert old[1].chunk_id in plan.obsolete_ids and old[2].chunk_id in plan.obsolete_ids
    assert "\n".join(chunk.text for chunk in plan.chunks) == new_body


def test_completed_same_body_with_a_short_tail_is_not_partial_recovery() -> None:
    body = "\n".join([_line(1), json.dumps({"role": "user", "content": "short"})])
    old = _snapshot(body, 200)
    assert len(old[-1].chunk_text) < 128
    plan = plan_stable_chunks(body, body, old, max_chars=200, structured_chunk_size=200)
    assert plan is not None and plan.extraction_indices == [] and plan.obsolete_ids == []


@pytest.mark.parametrize("kind", ["plain", "array", "missing_body", "corrupt_hash", "oversize", "gapped_indices"])
def test_unsupported_or_unverified_inputs_keep_the_old_path(kind: str) -> None:
    body = "\n".join(_line(i) for i in range(20))
    old = _snapshot(body, 400)
    before, after = body, body
    if kind == "plain":
        before = after = "plain prose " * 100
    elif kind == "array":
        before = after = json.dumps([{"content": "a"}, {"content": "b"}])
    elif kind == "missing_body":
        before = None
    elif kind == "corrupt_hash":
        old[0] = replace(old[0], content_hash="unverified")
    elif kind == "oversize":
        after = body + "x" * MAX_STABLE_SOURCE_CHARS
    else:
        old[1] = replace(old[1], chunk_index=100)
    assert plan_stable_chunks(before, after, old, max_chars=400, structured_chunk_size=400) is None


def test_capability_is_explicit_and_postgres_only() -> None:
    # Base implementation is intentionally safe for all external store variants.
    assert MemoriesExtension.supports_stable_chunk_reuse(object(), SimpleNamespace(backend_type="postgresql")) is False
    store = PostgresMemories.__new__(PostgresMemories)
    assert store.supports_stable_chunk_reuse(SimpleNamespace(backend_type="postgresql")) is True
    assert store.supports_stable_chunk_reuse(SimpleNamespace(backend_type="oracle")) is False
