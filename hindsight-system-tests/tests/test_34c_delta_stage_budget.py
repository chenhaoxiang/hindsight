"""A held structured edit is bounded without rewriting the stored page.

The second server is HTTP-only over the suite's isolated store: its global edit
budget applies to dry-run execution, while the main worker handles setup work.
"""

from __future__ import annotations

import asyncio

import pytest
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException

from hindsight_system_tests import reflect_loop, start_hindsight_server
from hindsight_system_tests.payloads import add_section, consolidation, extracted, fact

pytestmark = pytest.mark.asyncio
ANSWER = "Alice lives in Berlin."
QUERY = "Where does Alice live?"


async def test_edit_deadline_releases_the_request_and_preserves_page(
    client, llm, bank_id, settled, stub_server, tmp_path
) -> None:
    llm.on_step("extract_facts").returns(extracted(fact(ANSWER, who="Alice", entities=["Alice"])))
    llm.on_step("consolidate").returns(consolidation())
    reflect_loop(llm, answer=ANSWER, query=QUERY)
    await client.aretain(bank_id=bank_id, content=ANSWER)
    await settled(bank_id)
    created = await client.mental_models.create_mental_model(
        bank_id,
        {"name": "Housing", "source_query": QUERY, "trigger": {"mode": "delta", "fact_types": ["world"], "refresh_after_consolidation": False}},
    )
    await settled(bank_id)
    before = await client.mental_models.get_mental_model(bank_id, created.mental_model_id, detail="full")
    llm.reset()
    llm.on_step("extract_facts").returns(extracted(fact("Alice moved to Munich.", who="Alice", entities=["Alice"])))
    llm.on_step("consolidate").returns(consolidation())
    await client.aretain(bank_id=bank_id, content="Alice moved to Munich.")
    await settled(bank_id)

    llm.reset()
    llm.on_step("reflect_round_query").returns_text(QUERY)
    reflect_loop(llm, answer=ANSWER, query=QUERY)
    llm.on_step("mental_model_delta").returns(add_section("Moves", "Alice moved to Munich."))
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path / "budget-server.log",
        extra_env={"HINDSIGHT_API_WORKER_ENABLED": "false", "HINDSIGHT_API_MENTAL_MODEL_DELTA_TIMEOUT": "2"},
    )
    budget_client = Hindsight(base_url=server.url)
    try:
        async with llm.hold("mental_model_delta") as held:
            pending = asyncio.create_task(budget_client.mental_models.dry_run_refresh_mental_model(bank_id, created.mental_model_id))
            await held.reached()
            with pytest.raises(ApiException) as raised:
                await pending
            assert raised.value.status == 500
        after = await client.mental_models.get_mental_model(bank_id, created.mental_model_id, detail="full")
        assert after.content == before.content
        assert after.last_memory_seen_at == before.last_memory_seen_at
        assert after.last_refreshed_at == before.last_refreshed_at
        assert after.last_refresh_failed_at is None
    finally:
        await budget_client.aclose()
        server.stop()
