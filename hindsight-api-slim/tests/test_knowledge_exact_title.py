"""A full page title is an identity lookup, still confined by bank and tag scope."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.engine.memories.base import KnowledgePageMatch
from hindsight_api.engine.memory_engine import KnowledgeTagFilter
from hindsight_api.engine.retain import embedding_utils
from hindsight_api.engine.search.tags import TagGroupLeaf
from hindsight_api.extensions import BankReadOperation, OperationValidationError
from tests.test_consolidation_stale_batch import cross_encoder, embeddings  # noqa: F401 (fixtures)
from tests.test_knowledge_base import _kb_validator
from tests.test_tag_scope import _ScopeByApiKey


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Fixture maintenance plan", "中文维护计划与分层检索", "???"])
async def test_exact_title_works_without_either_hybrid_arm(
    memory: MemoryEngine, request_context: RequestContext, monkeypatch, title: str
) -> None:
    bank = "title-exact-" + uuid.uuid4().hex[:10]
    try:
        await memory.ensure_bank_profile(bank, request_context=request_context)
        page = await memory.create_knowledge_page(
            bank_id=bank,
            name=title,
            source_query="Fixture scope?",
            content="Fixture body.",
            request_context=request_context,
        )
        original_config = memory._config_resolver.get_bank_config

        async def no_text_search(bank_id, context=None):
            config = await original_config(bank_id, context)
            return {**config, "enable_text_search": False}

        monkeypatch.setattr(memory._config_resolver, "get_bank_config", no_text_search)
        embed = AsyncMock(return_value=[None])
        monkeypatch.setattr(embedding_utils, "generate_embeddings_batch", embed)
        hits = await memory.search_knowledge_pages(bank, f"  {title.upper()}  ", request_context=request_context)
        assert len(hits) == 1
        assert hits[0]["id"] == page["id"]
        assert hits[0]["name"] == title
        assert hits[0]["mental_model_id"] == page["mental_model_id"]
        assert hits[0]["source_query"] == "Fixture scope?"
        assert hits[0]["snippet"] == "Fixture body."
        assert hits[0]["score"] == 1.0
        # With a one-result budget an identity hit does not call embeddings at all.
        embed.side_effect = AssertionError("exact-only search must not embed")
        assert await memory.search_knowledge_pages(bank, title, limit=1, request_context=request_context) == hits
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
async def test_exact_candidates_obey_bank_tag_filter_forced_scope_and_denial(
    memory: MemoryEngine, request_context: RequestContext, monkeypatch
) -> None:
    banks = ["exact-scope-" + uuid.uuid4().hex[:8] for _ in range(2)]
    original_validator = memory._operation_validator
    title = "Shared exact title"
    try:
        for bank in banks:
            await memory.ensure_bank_profile(bank, request_context=request_context)
        allowed_folder = await memory.create_knowledge_folder(banks[0], name="Allowed", request_context=request_context)
        hidden_folder = await memory.create_knowledge_folder(banks[0], name="Hidden", request_context=request_context)
        await memory.create_knowledge_folder(banks[0], name=title, request_context=request_context)
        allowed = await memory.create_knowledge_page(
            bank_id=banks[0],
            name=title,
            parent_id=allowed_folder["id"],
            source_query="Allowed?",
            content="Visible fixture.",
            tags=["reader:allowed"],
            request_context=request_context,
        )
        hidden = await memory.create_knowledge_page(
            bank_id=banks[0],
            name=title,
            parent_id=hidden_folder["id"],
            source_query="Hidden?",
            content="Hidden fixture.",
            tags=["reader:hidden"],
            request_context=request_context,
        )
        foreign = await memory.create_knowledge_page(
            bank_id=banks[1],
            name=title,
            source_query="Foreign?",
            content="Other-bank fixture.",
            tags=["reader:allowed"],
            request_context=request_context,
        )
        embed = AsyncMock(return_value=[None])
        monkeypatch.setattr(embedding_utils, "generate_embeddings_batch", embed)
        filtered = await memory.search_knowledge_pages(
            banks[0],
            title,
            tag_filter=KnowledgeTagFilter(tags=["reader:allowed"], tags_match="all_strict"),
            request_context=request_context,
        )
        assert [hit["id"] for hit in filtered] == [allowed["id"]]
        assert hidden["id"] not in {hit["id"] for hit in filtered}
        assert foreign["id"] not in {hit["id"] for hit in filtered}
        memory._operation_validator = _ScopeByApiKey(
            {"reader": [TagGroupLeaf(tags=["reader:allowed"], match="all_strict")]}
        )
        scoped = await memory.search_knowledge_pages(banks[0], title, request_context=RequestContext(api_key="reader"))
        assert [hit["id"] for hit in scoped] == [allowed["id"]]
        denied = _kb_validator(reject_read=BankReadOperation.SEARCH_KNOWLEDGE_BASE)
        memory._operation_validator = denied
        with pytest.raises(OperationValidationError):
            await memory.search_knowledge_pages(banks[0], title, limit=1, request_context=request_context)
        assert denied.read_ops == [BankReadOperation.SEARCH_KNOWLEDGE_BASE]
    finally:
        memory._operation_validator = original_validator
        for bank in banks:
            await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_index", [True, False])
async def test_store_owned_candidate_cut_cannot_hide_an_exact_title(
    memory: MemoryEngine, request_context: RequestContext, monkeypatch, empty_index: bool
) -> None:
    bank = "exact-store-" + uuid.uuid4().hex[:8]
    try:
        await memory.ensure_bank_profile(bank, request_context=request_context)
        exact = await memory.create_knowledge_page(
            bank_id=bank,
            name="Exact store title",
            source_query="Exact?",
            content="Fixture identity.",
            request_context=request_context,
        )
        other = await memory.create_knowledge_page(
            bank_id=bank,
            name="Other",
            source_query="Other?",
            content="Related fixture.",
            request_context=request_context,
        )
        ranked = [] if empty_index else [KnowledgePageMatch(page_id=other["mental_model_id"], score=0.9)]
        store = SimpleNamespace(
            store_owned_for=lambda bank_id: bank_id == bank, search_knowledge_pages=AsyncMock(return_value=ranked)
        )
        with monkeypatch.context() as scoped:
            scoped.setattr("hindsight_api.engine.memories.get_memories", lambda: store)
            hits = await memory.search_knowledge_pages(
                bank, "Exact store title", limit=2, request_context=request_context
            )
        assert [hit["id"] for hit in hits] == ([exact["id"]] if empty_index else [exact["id"], other["id"]])
        assert hits[0]["score"] == 1.0
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
async def test_exact_hits_are_deduplicated_before_unchanged_hybrid_order(
    memory: MemoryEngine, request_context: RequestContext
) -> None:
    bank = "exact-order-" + uuid.uuid4().hex[:8]
    try:
        await memory.ensure_bank_profile(bank, request_context=request_context)
        exact = await memory.create_knowledge_page(
            bank_id=bank,
            name="Fixture title",
            source_query="Exact?",
            content="Unrelated text.",
            request_context=request_context,
        )
        other = await memory.create_knowledge_page(
            bank_id=bank,
            name="Notes",
            source_query="Notes?",
            content="Fixture title details.",
            request_context=request_context,
        )
        hits = await memory.search_knowledge_pages(bank, "Fixture title", limit=2, request_context=request_context)
        assert [hit["id"] for hit in hits] == [exact["id"], other["id"]]
        assert len({hit["id"] for hit in hits}) == 2
        assert hits[0]["score"] == 1.0
        assert 0 < hits[1]["score"] <= 1.0
    finally:
        await memory.delete_bank(bank, request_context=request_context)
