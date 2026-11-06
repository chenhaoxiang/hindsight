"""A temporary provider error during the edit leg must not permanently pause a page.

The existing refresh-failure story exercises reflect failures. This story crosses
reflect -> structured edit -> failure handling -> the next automatic refresh.
"""

from __future__ import annotations

import pytest

from hindsight_system_tests import reflect_loop, wait_until_settled
from hindsight_system_tests.payloads import add_section, consolidation, extracted, fact

pytestmark = pytest.mark.asyncio
ANSWER = "Alice lives in Berlin."


async def _retain(client, llm, bank: str, text: str) -> None:
    llm.on_step("extract_facts").returns(extracted(fact(text, who="Alice", entities=["Alice"])))
    llm.on_step("consolidate").returns(consolidation())
    await client.aretain(bank_id=bank, content=text)
    await wait_until_settled(client, bank, allow_failed=True)


@pytest.mark.parametrize("status", [408, 429, 503])
async def test_transient_delta_error_preserves_baseline_and_next_automatic_refresh_runs(
    client, llm, bank_id, settled, status: int
) -> None:
    reflect_loop(llm, answer=ANSWER, query="Where does Alice live?")
    await _retain(client, llm, bank_id, "Alice moved to Berlin.")
    created = await client.mental_models.create_mental_model(
        bank_id,
        {
            "name": "Housing",
            "source_query": "Where does Alice live?",
            "trigger": {"mode": "delta", "fact_types": ["world"], "refresh_after_consolidation": True},
        },
    )
    await settled(bank_id)
    before = await client.mental_models.get_mental_model(bank_id, created.mental_model_id, detail="full")

    llm.reset()
    reflect_loop(llm, answer=ANSWER, query="Where does Alice live?")
    llm.on_step("mental_model_delta").fails_with_status(status)
    await _retain(client, llm, bank_id, "Alice bought a bike.")
    failed = await client.mental_models.get_mental_model(bank_id, created.mental_model_id, detail="full")
    assert failed.content == before.content
    assert failed.last_refreshed_at == before.last_refreshed_at
    assert failed.last_memory_seen_at == before.last_memory_seen_at
    assert failed.last_refresh_failed_at is None
    operations = await client.operations.list_operations(bank_id, type="refresh_mental_model", status="failed", limit=100)
    assert operations.total == 1

    llm.reset()
    reflect_loop(llm, answer=ANSWER, query="Where does Alice live?")
    llm.on_step("mental_model_delta").returns(add_section("Update", "Alice bought a bike."))
    await _retain(client, llm, bank_id, "Alice visited the bike shop.")
    after = await client.mental_models.get_mental_model(bank_id, created.mental_model_id, detail="full")
    assert after.content == ANSWER + "\n\n## Update\n\nAlice bought a bike.\n"
    assert after.last_memory_seen_at > before.last_memory_seen_at
    assert after.last_refresh_failed_at is None
    operations = await client.operations.list_operations(bank_id, type="refresh_mental_model", limit=100)
    assert operations.total == 3
