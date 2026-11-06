"""Edited inputs are re-read, never re-asked as though the LLM omitted them.

The fixture stubs only ML scoring. MemoryEngine, the store, version checks and the
consolidation loop are real, against the existing isolated pg0 fixture.
"""

import hashlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.engine.consolidation import consolidator as C
from hindsight_api.engine.cross_encoder import CrossEncoderModel
from hindsight_api.engine.embeddings import Embeddings
from hindsight_api.engine.memories import get_memories
from hindsight_api.engine.schema import fq_table
from tests.test_observation_invalidation import _ensure_bank, _insert_document_with_memories


class _FixtureEmbeddings(Embeddings):
    @property
    def provider_name(self) -> str:
        return "fixture"

    @property
    def dimension(self) -> int:
        return 384

    async def initialize(self) -> None:
        pass

    async def encode(self, texts: list[str]) -> list[list[float]]:
        return [[(v + 1) / 256 for v in hashlib.sha512(text.encode()).digest()] * 6 for text in texts]


class _FixtureReranker(CrossEncoderModel):
    @property
    def provider_name(self) -> str:
        return "fixture"

    async def initialize(self) -> None:
        pass

    async def _predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.8] * len(pairs)


@pytest.fixture(scope="session")
def embeddings() -> Embeddings:
    return _FixtureEmbeddings()


@pytest.fixture(scope="session")
def cross_encoder() -> CrossEncoderModel:
    return _FixtureReranker()


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_a_retagged_batch_is_re_read_without_stale_omission_retries(
    memory: MemoryEngine, request_context: RequestContext
) -> None:
    bank_id = f"test-stale-batch-{uuid.uuid4().hex[:8]}"
    await _ensure_bank(memory, bank_id, request_context)
    pool = await memory._get_pool()
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    try:
        async with pool.acquire() as conn:
            ids = await _insert_document_with_memories(
                memory,
                conn,
                bank_id,
                doc_id,
                [("Alice loves hiking.", "experience"), ("Bob loves chess.", "experience")],
            )
            await get_memories().mark_consolidated(
                conn=conn, fq_table=fq_table, bank_id=bank_id, unit_ids=[str(mid) for mid in ids], when=None
            )
        calls = 0

        async def fake_llm(**_kwargs: object) -> C._BatchLLMResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                with patch.object(memory, "submit_async_consolidation", new=AsyncMock()):
                    await memory.update_document(doc_id, bank_id, tags=["new-tag"], request_context=request_context)
            return C._BatchLLMResult(
                creates=[C._CreateAction(text="A durable fixture fact.", source_fact_ids=[str(mid)]) for mid in ids]
            )

        with patch.object(C, "_consolidate_batch_with_llm", side_effect=fake_llm):
            await C.run_consolidation_job(memory_engine=memory, bank_id=bank_id, request_context=request_context)
        assert calls == 2, "reject the stale version once, then read the new version; do not bisect/re-ask the old copy"
        done = await memory.list_memory_units(bank_id, consolidation_state="done", request_context=request_context)
        assert {m["id"] for m in done["items"]} == {str(mid) for mid in ids}
        observations = await memory.list_memory_units(bank_id, fact_type="observation", request_context=request_context)
        assert all(o["tags"] == ["new-tag"] for o in observations["items"])
    finally:
        await memory.delete_bank(bank_id, request_context=request_context)
