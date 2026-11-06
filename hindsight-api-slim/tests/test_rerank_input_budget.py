"""Exact backend tokenization guards ranking copies without changing sources."""

import asyncio
from unittest.mock import patch

import pytest
from aiohttp import web
from pydantic import ValidationError

from hindsight_api.config import HindsightConfig
from hindsight_api.engine.cross_encoder import CohereCrossEncoder, MultiCrossEncoder, create_cross_encoder_from_env
from hindsight_api.engine.rerank_input_budget import RerankInputBudgetError, ServerTokenizerBudget
from tests.aiohttp_stub import stub_server
from tests.test_llm_trace import registered_recorder  # noqa: F401 (fixture)


@pytest.mark.asyncio
async def test_budget_uses_server_tokenizer_keeps_query_pool_and_trace(registered_recorder) -> None:
    requests = []
    query = "中文 query"
    # A character tokenizer deliberately differs from Hindsight's BPE token count.
    pairs = [(query, "完整 source " * 20), ("second", "tiny"), (query, "短文")]
    original = list(pairs)

    async def handler(request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer fixture-key"
        body = await request.json()
        if request.path == "/tokenize":
            assert body["add_special"] is False
            return web.json_response({"tokens": [ord(char) for char in body["content"]]})
        if request.path == "/detokenize":
            return web.json_response({"content": "".join(chr(token) for token in body["tokens"])})
        assert request.path == "/rerank"
        requests.append(body)
        assert all(len(body["query"]) + len(doc) + 4 <= 24 for doc in body["documents"])
        return web.json_response(
            {"results": [{"index": i, "relevance_score": (i + 1) / 10} for i in range(len(body["documents"]))]}
        )

    async with stub_server(handler) as url:
        encoder = CohereCrossEncoder(
            api_key="fixture-key",
            base_url=f"{url}/rerank",
            tokenizer_url=f"{url}/tokenize",
            max_input_tokens=24,
            token_overhead=4,
        )
        await encoder.initialize()
        scores = await encoder.predict(pairs)
    assert scores == [0.1, 0.1, 0.2]
    assert pairs == original
    assert len(requests) == 2
    first = next(body for body in requests if body["query"] == query)
    assert first["query"] == query
    assert first["documents"] == [original[0][1][:12], "短文"]
    assert "top_n" not in first
    record = registered_recorder.records[0]
    assert record.llm_info["input_count"] == 3
    assert record.input == {"queries": [query, "second"], "documents": [original[0][1][:12], "tiny", "短文"]}
    # Tokenizer work is not a billed model call, nor are estimates billed tokens.
    assert len(registered_recorder.records) == 1 and record.input_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["query", "invalid_tokens", "empty_tokens", "changed_prefix", "redirect"])
async def test_unverifiable_budget_fails_before_reranking(bad: str) -> None:
    sent_rerank = False

    async def handler(request: web.Request) -> web.Response:
        nonlocal sent_rerank
        body = await request.json()
        if request.path == "/rerank":
            sent_rerank = True
            return web.json_response({"results": []})
        if bad == "redirect":
            return web.Response(status=307, headers={"Location": "https://untrusted.invalid/tokenize"})
        if request.path == "/detokenize":
            return web.json_response({"content": "not a source prefix"})
        if bad == "invalid_tokens":
            return web.json_response({"tokens": [True, "12"]})
        if bad == "empty_tokens":
            return web.json_response({"tokens": []})
        return web.json_response({"tokens": list(range(len(body["content"])))})

    async with stub_server(handler) as url:
        encoder = CohereCrossEncoder(
            api_key="fixture",
            base_url=f"{url}/rerank",
            tokenizer_url=f"{url}/tokenize",
            max_input_tokens=12,
            token_overhead=2,
        )
        await encoder.initialize()
        with pytest.raises((RerankInputBudgetError, ValidationError)):
            await encoder.predict([("q" * 12 if bad == "query" else "q", "document" * 10)])
    assert not sent_rerank


@pytest.mark.parametrize(
    "url",
    [
        "https://other.invalid/tokenize",
        "https://user:secret@fixture.invalid/tokenize",
        "https://fixture.invalid/tokenize?key=value",
        "https://fixture.invalid/not-tokenize",
    ],
)
def test_tokenizer_cannot_receive_credentials_at_another_origin(url: str) -> None:
    with pytest.raises(RerankInputBudgetError, match="same-origin"):
        ServerTokenizerBudget(
            rerank_url="https://fixture.invalid/rerank", tokenizer_url=url, api_key="fixture", max_input_tokens=256
        )


@pytest.mark.parametrize(
    "options",
    [
        {"tokenizer_url": "https://fixture.invalid/tokenize"},
        {"max_input_tokens": 256},
        {"tokenizer_url": "https://fixture.invalid/tokenize", "max_input_tokens": 128},
    ],
)
def test_incomplete_or_impossible_budget_is_rejected(options) -> None:
    with pytest.raises(RerankInputBudgetError):
        CohereCrossEncoder(api_key="fixture", base_url="https://fixture.invalid/rerank", **options)


@pytest.mark.asyncio
async def test_utf8_split_backs_off_and_verifies_the_prefix() -> None:
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        if request.path == "/tokenize":
            return web.json_response({"tokens": list(body["content"].encode())})
        return web.json_response({"content": bytes(body["tokens"]).decode(errors="replace")})

    async with stub_server(handler) as url:
        budget = ServerTokenizerBudget(
            rerank_url=f"{url}/rerank",
            tokenizer_url=f"{url}/tokenize",
            api_key="fixture",
            max_input_tokens=10,
            token_overhead=1,
        )
        # Available=7 bytes: two complete Chinese characters, not a replacement char.
        assert await budget.fit("qq", ["中文测试"]) == ["中文"]


@pytest.mark.asyncio
async def test_tokenization_is_bounded_and_cancelled_with_its_caller() -> None:
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    reranks = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal active, peak, reranks
        body = await request.json()
        if request.path == "/rerank":
            reranks += 1
            return web.json_response({"results": []})
        if body["content"] != "q":
            active += 1
            peak = max(peak, active)
            entered.set()
            await release.wait()
            active -= 1
        return web.json_response({"tokens": [1]})

    async with stub_server(handler) as url:
        encoder = CohereCrossEncoder(
            api_key="fixture", base_url=f"{url}/rerank", tokenizer_url=f"{url}/tokenize", max_input_tokens=256
        )
        await encoder.initialize()
        pending = asyncio.create_task(encoder.predict([("q", f"document-{i}") for i in range(32)]))
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
    assert 1 <= peak <= 8 and reranks == 0


@pytest.mark.asyncio
async def test_whole_tokenizer_phase_has_one_wall_budget() -> None:
    reranks = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal reranks
        if request.path == "/rerank":
            reranks += 1
        await asyncio.sleep(0.2)
        return web.json_response({"tokens": [1]})

    async with stub_server(handler) as url:
        encoder = CohereCrossEncoder(
            api_key="fixture",
            base_url=f"{url}/rerank",
            tokenizer_url=f"{url}/tokenize",
            max_input_tokens=256,
            timeout=0.05,
        )
        await encoder.initialize()
        with pytest.raises(TimeoutError):
            await encoder.predict([("q", "document")])
    assert reranks == 0


def test_factory_keeps_primary_and_fallback_budgets_isolated(monkeypatch) -> None:
    for name, value in {
        "HINDSIGHT_API_RERANKER_PROVIDER": "cohere",
        "HINDSIGHT_API_RERANKER_COHERE_API_KEY": "fixture",
        "HINDSIGHT_API_RERANKER_COHERE_BASE_URL": "https://primary.invalid/rerank",
        "HINDSIGHT_API_RERANKER_COHERE_TOKENIZER_URL": "https://primary.invalid/tokenize",
        "HINDSIGHT_API_RERANKER_COHERE_MAX_INPUT_TOKENS": "2048",
        "HINDSIGHT_API_RERANKER_COHERE_TOKEN_OVERHEAD": "64",
        "HINDSIGHT_API_RERANKER_1_PROVIDER": "cohere",
        "HINDSIGHT_API_RERANKER_1_COHERE_API_KEY": "fallback-fixture",
        "HINDSIGHT_API_RERANKER_1_COHERE_BASE_URL": "https://fallback.invalid/rerank",
    }.items():
        monkeypatch.setenv(name, value)
    config = HindsightConfig.from_env()
    with patch("hindsight_api.config.get_config", return_value=config):
        chain = create_cross_encoder_from_env()
    assert isinstance(chain, MultiCrossEncoder)
    primary, fallback = chain._members
    assert primary._input_budget.max_input_tokens == 2048
    assert primary._input_budget.token_overhead == 64
    assert fallback._input_budget is None
