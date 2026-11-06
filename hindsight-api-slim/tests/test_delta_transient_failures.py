"""Transient delta/retraction failures keep their cause and never freeze a page.

Only inference is stubbed. The refresh, store and failure handler run against the
suite's isolated pg0; no live model or application database is used.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest

from hindsight_api import MemoryEngine, RequestContext
from hindsight_api.cancellation import OperationCancelledError
from hindsight_api.config import clear_config_cache
from hindsight_api.engine.llm_interface import ProviderRateLimitResetError
from tests.test_consolidation_stale_batch import cross_encoder, embeddings  # noqa: F401 (fixtures)
from tests.test_mental_model_delta import _seed_fact_row, patch_llm_call, patch_reflect  # noqa: F401 (fixtures)


class _ProviderStatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"fixture HTTP {status_code}")


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
@pytest.mark.parametrize("retraction", [False, True], ids=["delta", "retraction"])
@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("fixture connection interrupted"),
        TimeoutError("fixture deadline"),
        _ProviderStatusError(408),
        _ProviderStatusError(429),
        _ProviderStatusError(503),
        ProviderRateLimitResetError(datetime.now(timezone.utc) + timedelta(hours=1), "fixture quota reset"),
        OperationCancelledError("fixture cancelled"),
        asyncio.CancelledError(),
    ],
    ids=["connection", "timeout", "http408", "http429", "http503", "quota-reset", "cooperative-cancel", "task-cancel"],
)
async def test_transient_edit_keeps_original_error_and_baseline(
    memory: MemoryEngine,
    request_context: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
    patch_reflect,
    patch_llm_call,
    error: BaseException,
    retraction: bool,
) -> None:
    bank = "test-edit-transient-" + uuid.uuid4().hex[:10]
    query = "What does the fixture team know?"
    try:
        await memory.ensure_bank_profile(bank, request_context=request_context)
        first = await _seed_fact_row(memory, bank, "The baseline fact remains true.")
        model = await memory.create_mental_model(
            bank_id=bank,
            name="Fixture team",
            source_query=query,
            content="## Baseline\n\nThe baseline fact remains true.\n",
            trigger={"mode": "delta", "refresh_after_consolidation": True},
            request_context=request_context,
        )
        patch_reflect(
            memory,
            text="## Baseline\n\nThe baseline fact remains true.\n",
            facts=[{"id": first, "text": "The baseline fact remains true.", "type": "observation"}],
        )
        patch_llm_call(memory, returns=[])
        await memory.refresh_mental_model(bank_id=bank, mental_model_id=model["id"], request_context=request_context)
        if retraction:
            current = await memory.get_mental_model(bank, model["id"], request_context=request_context)
            response = current["reflect_response"]
            response["based_on"]["observation"].append(
                {"id": str(uuid.uuid4()), "text": "A retired fixture fact.", "type": "observation", "context": None}
            )
            await memory.update_mental_model(
                bank, model["id"], reflect_response=response, request_context=request_context
            )
            patch_reflect(memory, text="No new fixture facts.")
        else:
            second = await _seed_fact_row(memory, bank, "A new fixture fact.")
            patch_reflect(
                memory,
                text="## Narrow candidate\n\nOnly the new fact.\n",
                facts=[{"id": second, "text": "A new fixture fact.", "type": "observation"}],
            )
        before = await memory.get_mental_model(bank, model["id"], detail="full", request_context=request_context)

        budget_expiry = type(error) is TimeoutError
        if budget_expiry:
            monkeypatch.setenv("HINDSIGHT_API_MENTAL_MODEL_DELTA_TIMEOUT", "0.02")
            clear_config_cache()

        async def fail_call(**_kwargs: object) -> None:
            if budget_expiry:
                await asyncio.Event().wait()
            raise error

        monkeypatch.setattr(memory._mental_model_refresh_llm_config, "call", fail_call)
        with pytest.raises(type(error)) as raised:
            await memory._handle_refresh_mental_model({"bank_id": bank, "mental_model_id": model["id"]})
        if not budget_expiry:
            assert raised.value is error
        after = await memory.get_mental_model(bank, model["id"], detail="full", request_context=request_context)
        for field in ("content", "structured_content", "last_refreshed_at", "last_memory_seen_at", "source_query"):
            assert after[field] == before[field], field
        assert after["reflect_response"]["based_on"] == before["reflect_response"]["based_on"]
        assert after["last_refresh_failed_at"] is None
        assert not await memory._automatic_refresh_paused(bank, model["id"])
    finally:
        await memory.delete_bank(bank, request_context=request_context)


@pytest.mark.asyncio
@pytest.mark.memory_backend_incompatible
async def test_invalid_delta_still_preserves_content_and_pauses(
    memory: MemoryEngine,
    request_context: RequestContext,
    monkeypatch: pytest.MonkeyPatch,
    patch_reflect,
    patch_llm_call,
) -> None:
    from hindsight_api.engine.memory_engine import MentalModelRefreshError

    bank = "test-edit-structural-" + uuid.uuid4().hex[:10]
    try:
        await memory.ensure_bank_profile(bank, request_context=request_context)
        model = await memory.create_mental_model(
            bank_id=bank,
            name="Structural fixture",
            source_query="Fixture facts",
            content="## Baseline\n\nKeep this fact.\n",
            trigger={"mode": "delta"},
            request_context=request_context,
        )
        patch_reflect(memory, text="## Baseline\n\nKeep this fact.\n")
        patch_llm_call(memory, returns=[])
        await memory.refresh_mental_model(bank_id=bank, mental_model_id=model["id"], request_context=request_context)
        before = await memory.get_mental_model(bank, model["id"], detail="full", request_context=request_context)
        fact_id = await _seed_fact_row(memory, bank, "New fixture fact.")
        patch_reflect(
            memory,
            text="Only the new fact.",
            facts=[{"id": fact_id, "text": "New fixture fact.", "type": "observation"}],
        )

        async def malformed_call(**_kwargs: object) -> None:
            raise ValueError("fixture invalid structured response")

        monkeypatch.setattr(memory._mental_model_refresh_llm_config, "call", malformed_call)
        with pytest.raises(MentalModelRefreshError):
            await memory._handle_refresh_mental_model({"bank_id": bank, "mental_model_id": model["id"]})
        after = await memory.get_mental_model(bank, model["id"], detail="full", request_context=request_context)
        for field in ("content", "structured_content", "last_refreshed_at", "last_memory_seen_at"):
            assert after[field] == before[field], field
        assert after["last_refresh_failed_at"] is not None
        assert after["reflect_response"]["refresh_skipped"] == "delta_ops_failed"
    finally:
        await memory.delete_bank(bank, request_context=request_context)
