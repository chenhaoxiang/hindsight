"""One delta-phase deadline covers retries without changing full synthesis."""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from hindsight_api.config import ENV_MENTAL_MODEL_DELTA_TIMEOUT, _get_raw_config
from hindsight_api.engine.reflect.delta_ops import request_delta_operations
from hindsight_api.engine.response_models import LLMCallResult, TokenUsage
from hindsight_api.worker.poller import ClaimedTask, WorkerPoller
from hindsight_api.worker.stage import StageHolder, _current_holder, bind_holder, get_stage, get_task_deadline


@pytest.mark.asyncio
async def test_semantic_correction_does_not_renew_edit_deadline() -> None:
    calls = 0
    cancelled = False

    class FixtureLLM:
        async def call(self, **_kwargs: object) -> LLMCallResult:
            nonlocal calls, cancelled
            calls += 1
            if calls == 1:
                await asyncio.sleep(0.01)
                return LLMCallResult(content={"operations": [{"op": "not-a-real-op"}]}, usage=TokenUsage())
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled = True
                raise

    holder = StageHolder()
    token = bind_holder(holder)
    try:
        with pytest.raises(TimeoutError):
            await request_delta_operations(
                FixtureLLM(),
                system_prompt="fixture",
                user_prompt="fixture",
                scope="mental_model_delta_ops",
                deadline=asyncio.get_running_loop().time() + 0.05,
            )
        assert calls == 2 and cancelled
        assert get_stage().endswith(".budget_exhausted")
    finally:
        _current_holder.reset(token)


@pytest.mark.asyncio
async def test_expired_edit_budget_does_not_start_another_model_call() -> None:
    class FixtureLLM:
        async def call(self, **_kwargs: object) -> None:
            raise AssertionError("No call may start past the deadline")

    with pytest.raises(TimeoutError, match="no remaining"):
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=asyncio.get_running_loop().time() - 1,
        )


@pytest.mark.asyncio
async def test_external_cancellation_is_not_reclassified_as_stage_expiry() -> None:
    class FixtureLLM:
        async def call(self, **_kwargs: object) -> None:
            raise asyncio.CancelledError()

    holder = StageHolder(stage="fixture-cancel")
    token = bind_holder(holder)
    try:
        with pytest.raises(asyncio.CancelledError):
            await request_delta_operations(
                FixtureLLM(),
                system_prompt="fixture",
                user_prompt="fixture",
                scope="mental_model_delta_ops",
                deadline=asyncio.get_running_loop().time() + 10,
            )
        assert get_stage() == "fixture-cancel"
    finally:
        _current_holder.reset(token)


@pytest.mark.asyncio
async def test_late_provider_result_cannot_bypass_expired_budget() -> None:
    class FixtureLLM:
        async def call(self, **_kwargs: object) -> LLMCallResult:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return LLMCallResult(content={"operations": []}, usage=TokenUsage())

    with pytest.raises(TimeoutError, match="after its stage deadline"):
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=asyncio.get_running_loop().time() + 0.02,
        )


@pytest.mark.asyncio
async def test_worker_exposes_and_clears_absolute_task_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from hindsight_api.worker import poller as poller_module

    seen = []

    async def executor(_task: object) -> None:
        seen.append(get_task_deadline())
        assert 0 < get_task_deadline() - asyncio.get_running_loop().time() <= 1

    monkeypatch.setattr(poller_module, "_wall_timeout_for", lambda _kind: 1.0)
    poller = WorkerPoller(backend=MagicMock(), worker_id="fixture", executor=executor)
    task = ClaimedTask(operation_id="fixture-op", schema=None, task_dict={"type": "refresh_mental_model"})
    holder = StageHolder()
    token = bind_holder(holder)
    try:
        await poller._run_executor(task, "refresh_mental_model", holder)
        assert len(seen) == 1 and seen[0] is not None
        assert get_task_deadline() is None
    finally:
        _current_holder.reset(token)


@pytest.mark.asyncio
async def test_outer_deadline_keeps_budget_expiry_breadcrumb(monkeypatch: pytest.MonkeyPatch) -> None:
    from hindsight_api.worker import poller as poller_module

    class FixtureLLM:
        async def call(self, **_kwargs: object) -> None:
            await asyncio.Event().wait()

    async def executor(_task: object) -> None:
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=get_task_deadline(),
        )

    monkeypatch.setattr(poller_module, "_wall_timeout_for", lambda _kind: 0.03)
    poller = WorkerPoller(backend=MagicMock(), worker_id="fixture", executor=executor)
    holder = StageHolder()
    token = bind_holder(holder)
    try:
        with pytest.raises(poller_module._WallTimeoutExceeded):
            await poller._run_executor(
                ClaimedTask(operation_id="fixture-op", schema=None, task_dict={}), "refresh_mental_model", holder
            )
        assert get_stage().endswith(".budget_exhausted")
        assert get_task_deadline() is None
    finally:
        _current_holder.reset(token)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_delta_budget_rejects_nonpositive_or_nonfinite_values(value: float) -> None:
    config = replace(_get_raw_config(), mental_model_delta_timeout=value)
    with pytest.raises(ValueError, match=ENV_MENTAL_MODEL_DELTA_TIMEOUT):
        config.validate()


def test_delta_budget_is_opt_in_without_lowering_provider_timeout() -> None:
    config = _get_raw_config()
    assert config.mental_model_delta_timeout is None
    assert replace(config, mental_model_delta_timeout=600).llm_timeout == config.llm_timeout


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [{"operations": [{"op": "not-a-real-op"}]}, {"operations": None}])
async def test_late_invalid_answer_cannot_start_a_correction_or_escape_as_schema_error(content) -> None:
    calls = 0

    class FixtureLLM:
        async def call(self, **_kwargs: object) -> LLMCallResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            return LLMCallResult(content=content, usage=TokenUsage())

    with pytest.raises(TimeoutError):
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=asyncio.get_running_loop().time() + 0.02,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_exception_after_stage_expiry_is_classified_as_budget_exhaustion() -> None:
    class FixtureLLM:
        async def call(self, **_kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ValueError("fixture invalid late response")

    with pytest.raises(TimeoutError) as raised:
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=asyncio.get_running_loop().time() + 0.02,
        )
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancel", "quota-reset"])
async def test_control_signal_keeps_its_type_when_stage_expiry_races_it(control: str) -> None:
    from datetime import datetime, timedelta, timezone

    from hindsight_api.cancellation import OperationCancelledError
    from hindsight_api.engine.llm_interface import ProviderRateLimitResetError

    error = (
        OperationCancelledError("fixture cancel")
        if control == "cancel"
        else ProviderRateLimitResetError(datetime.now(timezone.utc) + timedelta(hours=1), "fixture quota reset")
    )

    class FixtureLLM:
        async def call(self, **_kwargs: object) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise error

    with pytest.raises(type(error)) as raised:
        await request_delta_operations(
            FixtureLLM(),
            system_prompt="fixture",
            user_prompt="fixture",
            scope="mental_model_delta_ops",
            deadline=asyncio.get_running_loop().time() + 0.02,
        )
    assert raised.value is error
