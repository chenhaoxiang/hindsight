"""Regression coverage for permit congestion and deterministic proxy errors; no live services."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from hindsight_api.config import clear_config_cache
from hindsight_api.engine.consolidation.consolidator import _BatchFailureClass, _classify_batch_failure
from hindsight_api.engine.memory_engine import MemoryEngine, _is_non_retryable_task_error
from hindsight_api.engine.providers.openai_responses_llm import OpenAIResponsesLLM, RetryableResponsesError
from hindsight_api.engine.remote_retry import is_invalid_remote_request, is_transient_remote_error
from hindsight_api.worker.poller import _WALL_CEILINGS, ClaimedTask, WorkerPoller, _WallTimeoutExceeded
from hindsight_api.worker.stage import StageHolder


def _invalid_error() -> openai.InternalServerError:
    return openai.InternalServerError(
        "Complexity is unsupported",
        response=httpx.Response(502, request=httpx.Request("POST", "https://fixture.invalid/v1/responses")),
        body={"type": "image_generation_user_error", "code": "invalid_value", "param": "complexity"},
    )


def test_invalid_value_wrapped_in_502_is_not_transient() -> None:
    error = _invalid_error()
    assert is_invalid_remote_request(error)
    assert not is_transient_remote_error(error)
    assert _is_non_retryable_task_error(error)
    assert _classify_batch_failure(error) is _BatchFailureClass.PROPAGATE


def test_context_overflow_can_still_reach_adaptive_bisection() -> None:
    error = openai.BadRequestError(
        "context overflow",
        response=httpx.Response(400, request=httpx.Request("POST", "https://fixture.invalid/v1/responses")),
        body={"type": "invalid_request_error", "code": "context_length_exceeded"},
    )
    assert _classify_batch_failure(error) is _BatchFailureClass.RETRY


def test_exhausted_responses_stream_wrapper_is_recognized_as_transient() -> None:
    assert is_transient_remote_error(RetryableResponsesError("fixture stream interrupted"))


@pytest.mark.asyncio
async def test_responses_invalid_value_does_not_retry() -> None:
    provider = OpenAIResponsesLLM("openai-responses", "fixture-key", "https://fixture.invalid/v1", "gpt-6-luna")
    provider._create_response = AsyncMock(side_effect=_invalid_error())
    try:
        with pytest.raises(openai.InternalServerError):
            await provider.call([{"role": "user", "content": "fixture"}], max_retries=10)
        assert provider._create_response.await_count == 1
    finally:
        await provider.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,paused",
    [
        ("llm.responses.refresh.queued", False),
        ("llm.responses.refresh.backoff", False),
        ("llm.responses.refresh.attempt=1/4", True),
    ],
)
async def test_capacity_timeout_never_pauses_automatic_refresh(stage: str, paused: bool) -> None:
    memory = object.__new__(MemoryEngine)
    memory._record_mental_model_refresh_failure = AsyncMock()
    await memory.on_task_wall_timeout(
        {"type": "refresh_mental_model", "bank_id": "fixture-bank", "mental_model_id": "fixture-page"},
        None,
        "fixture timeout",
        stage,
    )
    assert memory._record_mental_model_refresh_failure.await_args.kwargs["pause_automatic"] is paused


@pytest.mark.asyncio
async def test_excess_refresh_is_deferred_before_executor_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HINDSIGHT_API_MENTAL_MODEL_REFRESH_LLM_MAX_CONCURRENT", "2")
    clear_config_cache()
    poller = WorkerPoller(backend=MagicMock(), worker_id="fixture-worker", executor=AsyncMock())
    poller._in_flight_by_type["refresh_mental_model"] = 2
    poller._defer_operation = AsyncMock()
    poller._execute_task_inner = AsyncMock()
    task = ClaimedTask(
        operation_id=str(uuid.uuid4()),
        schema=None,
        task_dict={"type": "refresh_mental_model", "operation_type": "refresh_mental_model", "bank_id": "fixture-bank"},
    )
    await poller.execute_task(task)
    poller._execute_task_inner.assert_not_awaited()
    poller._defer_operation.assert_awaited_once()
    assert not poller._active_tasks
    clear_config_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_count,retried", [(0, True), (3, False)])
async def test_queued_timeout_retry_is_bounded(
    retry_count: int, retried: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HINDSIGHT_API_WORKER_MAX_RETRIES", "3")
    clear_config_cache()
    poller = WorkerPoller(backend=MagicMock(), worker_id="fixture-worker", executor=AsyncMock())
    holder = StageHolder(stage="fixture")

    async def timed_out(*_args: object) -> None:
        holder.stage = "llm.responses.refresh.queued"
        raise _WallTimeoutExceeded(1800, _WALL_CEILINGS["refresh_mental_model"])

    poller._run_executor = timed_out
    poller._schedule_retry = AsyncMock()
    poller._mark_failed = AsyncMock()
    poller._notify_wall_timeout = AsyncMock()
    task = ClaimedTask(
        operation_id=str(uuid.uuid4()),
        schema=None,
        task_dict={
            "type": "refresh_mental_model",
            "operation_type": "refresh_mental_model",
            "bank_id": "fixture-bank",
            "_retry_count": retry_count,
        },
    )
    with patch("hindsight_api.worker.poller.get_metrics_collector", return_value=MagicMock()):
        await poller._execute_task_inner(task, holder)
    assert poller._schedule_retry.await_count == int(retried)
    assert poller._mark_failed.await_count == int(not retried)
    assert poller._notify_wall_timeout.await_count == int(not retried)
    clear_config_cache()
