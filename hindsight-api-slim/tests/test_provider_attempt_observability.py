"""Recovered provider errors stay visible without adding rows or leaking payloads."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from hindsight_api.engine.llm_wrapper import LLMProvider
from hindsight_api.worker.stage import StageHolder, bind_holder, get_stage
from tests.test_llm_trace import registered_recorder  # noqa: F401 (fixture)
from tests.test_openai_responses_provider import _fake_response


@pytest.mark.asyncio
async def test_recovered_408_is_visible_inside_one_successful_logical_trace(registered_recorder, monkeypatch):
    llm = LLMProvider(
        provider="openai-responses",
        api_key="fixture-key",
        base_url="https://fixture.invalid/v1",
        model="gpt-6-luna",
        max_retries=1,
        initial_backoff=0.01,
        max_backoff=0.01,
    )
    error = openai.APIStatusError(
        "not-for-attempt-metadata",
        response=httpx.Response(408, request=httpx.Request("POST", "https://fixture.invalid/v1/responses")),
        body={"error": {"type": "fixture_timeout"}},
    )
    create = AsyncMock(side_effect=[error, _fake_response(output_text="fixture answer")])
    llm._provider_impl._client.responses.create = create
    stages = []
    real_sleep = asyncio.sleep

    async def short_sleep(_delay: float) -> None:
        stages.append(get_stage())
        await real_sleep(0)

    holder = StageHolder()
    token = bind_holder(holder)
    monkeypatch.setattr("hindsight_api.engine.providers.openai_responses_llm.asyncio.sleep", short_sleep)
    try:
        result = await llm.call(messages=[{"role": "user", "content": "fixture"}], scope="mental_model_delta_ops")
        assert result.content == "fixture answer"
        assert create.await_count == 2
        assert len(registered_recorder.records) == 1
        record = registered_recorder.records[0]
        assert record.status == "success"
        attempts = record.llm_info["provider_attempts"]
        assert attempts["total"] == 2 and attempts["omitted"] == 0
        first, second = attempts["records"]
        assert (first["attempt"], first["outcome"], first["http_status"]) == (1, "http_error", 408)
        assert first["error_type"] == "APIStatusError"
        assert first["planned_backoff_ms"] > 0
        assert second["attempt"] == 2 and second["outcome"] == "response"
        assert "not-for-attempt-metadata" not in json.dumps(attempts)
        assert stages and all(stage.endswith(".backoff") for stage in stages)
        assert record.output_tokens == 15
    finally:
        from hindsight_api.worker.stage import _current_holder

        _current_holder.reset(token)
        await llm._provider_impl.cleanup()


@pytest.mark.asyncio
async def test_attempt_history_is_local_to_concurrent_calls(registered_recorder):
    llm = LLMProvider(
        provider="openai-responses", api_key="fixture-key", base_url="https://fixture.invalid/v1", model="gpt-6-luna"
    )
    llm._provider_impl._client.responses.create = AsyncMock(return_value=_fake_response(output_text="ok"))
    try:
        await asyncio.gather(
            *(
                llm.call(messages=[{"role": "user", "content": "fixture"}], scope=scope)
                for scope in ("fixture-one", "fixture-two")
            )
        )
        assert len(registered_recorder.records) == 2
        assert all(record.llm_info["provider_attempts"]["total"] == 1 for record in registered_recorder.records)
        assert {record.scope for record in registered_recorder.records} == {"fixture-one", "fixture-two"}
    finally:
        await llm._provider_impl.cleanup()


@pytest.mark.asyncio
async def test_cancelled_upstream_attempt_is_not_hidden(registered_recorder):
    llm = LLMProvider(
        provider="openai-responses", api_key="fixture-key", base_url="https://fixture.invalid/v1", model="gpt-6-luna"
    )
    llm._provider_impl._client.responses.create = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        with pytest.raises(asyncio.CancelledError):
            await llm.call(messages=[{"role": "user", "content": "fixture"}], scope="fixture-cancel")
        assert len(registered_recorder.records) == 1
        record = registered_recorder.records[0]
        assert record.status == "error"
        assert record.llm_info["provider_attempts"]["records"][0]["outcome"] == "cancelled"
    finally:
        await llm._provider_impl.cleanup()


def test_attempt_history_is_bounded_and_snapshot_is_detached() -> None:
    from hindsight_api.engine.llm_attempts import ProviderAttemptLog

    log = ProviderAttemptLog()
    for number in range(1, 41):
        log.start(number).outcome = "http_error"
    snapshot = log.summary()
    assert snapshot.total == 40 and snapshot.omitted == 8 and len(snapshot.records) == 32
    log.records[0].outcome = "response"
    assert snapshot.records[0].outcome == "http_error"


@pytest.mark.asyncio
async def test_attempts_survive_real_sdk_http_transport(registered_recorder) -> None:
    from aiohttp import web
    from tests.aiohttp_stub import stub_server

    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        assert request.path == "/v1/responses"
        body = await request.json()
        assert body["model"] == "gpt-6-luna"
        calls += 1
        if calls == 1:
            return web.json_response(
                {"error": {"message": "fixture timeout", "type": "timeout"}},
                status=408,
                headers={"x-request-id": "req_fixture_timeout"},
            )
        return web.json_response(
            {
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6-luna",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "fixture response", "annotations": []}],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 6,
                    "total_tokens": 16,
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
            headers={"x-request-id": "req_fixture_success"},
        )

    async with stub_server(handler) as url:
        llm = LLMProvider(
            provider="openai-responses",
            api_key="fixture-key",
            base_url=url + "/v1",
            model="gpt-6-luna",
            max_retries=1,
            initial_backoff=0.001,
            max_backoff=0.001,
        )
        try:
            result = await llm.call(messages=[{"role": "user", "content": "fixture"}], scope="fixture-http")
            assert result.content == "fixture response" and calls == 2
            assert len(registered_recorder.records) == 1
            record = registered_recorder.records[0]
            assert record.input_tokens == 10 and record.output_tokens == 5 and record.thoughts_tokens == 1
            attempts = record.llm_info["provider_attempts"]["records"]
            assert [(attempt["http_status"], attempt["request_id"]) for attempt in attempts] == [
                (408, "req_fixture_timeout"),
                (200, "req_fixture_success"),
            ]
        finally:
            await llm._provider_impl.cleanup()
