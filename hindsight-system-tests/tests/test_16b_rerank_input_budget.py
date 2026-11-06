"""Recall fits the ranking copy while returning the complete stored fact."""

from __future__ import annotations

import pytest
from hindsight_client import Hindsight

from hindsight_system_tests import start_hindsight_server
from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio
CONTENT = "Alice lives in Berlin. " + "She enjoys quiet neighborhoods. " * 12
QUERY = "Where does Alice live?"


async def test_recall_returns_the_full_source_after_bounded_ranking(client, llm, bank_id, settled, stub_server, stubs, tmp_path) -> None:
    llm.on_step("extract_facts").returns(extracted(fact(CONTENT)))
    llm.on_step("consolidate").returns(consolidation())
    await client.aretain(bank_id=bank_id, content=CONTENT)
    await settled(bank_id)
    baseline = await client.arecall(bank_id=bank_id, query=QUERY, types=["world"])
    stubs.rerank.ranked.clear()
    stubs.rerank.queries.clear()
    stubs.rerank.max_input_characters = 96
    stubs.rerank.token_overhead = 8
    server = start_hindsight_server(stub_url=stub_server.url, log_path=tmp_path / "input-budget-server.log", extra_env={
        "HINDSIGHT_API_WORKER_ENABLED": "false",
        "HINDSIGHT_API_RERANKER_PROVIDER": "cohere",
        "HINDSIGHT_API_RERANKER_COHERE_API_KEY": "stub-key",
        "HINDSIGHT_API_RERANKER_COHERE_BASE_URL": f"{stub_server.url}/rerank",
        "HINDSIGHT_API_RERANKER_COHERE_TOKENIZER_URL": f"{stub_server.url}/tokenize",
        "HINDSIGHT_API_RERANKER_COHERE_MAX_INPUT_TOKENS": "96",
        "HINDSIGHT_API_RERANKER_COHERE_TOKEN_OVERHEAD": "8",
    })
    budget_client = Hindsight(base_url=server.url)
    try:
        response = await budget_client.arecall(bank_id=bank_id, query=QUERY, types=["world"])
        assert len(response.results) == len(baseline.results) == 1
        actual = response.results[0]
        previous = baseline.results[0]
        assert actual.text == CONTENT == previous.text
        assert actual.id == previous.id
        assert actual.document_id == previous.document_id
        assert actual.chunk_id == previous.chunk_id
        assert actual.type == "world" and actual.context == ""
        assert actual.tags == [] and actual.metadata == {}
        assert actual.occurred_start is None and actual.occurred_end is None
        assert actual.scores.reranker == stubs.rerank.score(QUERY, CONTENT[: 96 - 8 - len(QUERY)])
        assert actual.scores.semantic == previous.scores.semantic
        assert actual.scores.keyword == previous.scores.keyword
        assert response.entities is None and response.chunks is None and response.source_facts is None
        assert stubs.rerank.queries == [QUERY]
        assert stubs.rerank.ranked == [CONTENT[: 96 - 8 - len(QUERY)]]
    finally:
        await budget_client.aclose()
        server.stop()
