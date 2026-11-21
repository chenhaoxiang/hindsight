"""The current-state gate has to distinguish page freshness from source facts.

This story stays on the published Python client, as every system story does. It
pins the API seam that the coding-agent integration consumes: a page reports a
scoped freshness state and source watermark, while the caller must make an
explicit raw recall when a page is stale or its metadata cannot prove safety.
The page search itself is retrieval-only; it must not run reflect or a hidden
raw fallback.
"""

from __future__ import annotations

import pytest

from hindsight_system_tests import reflect_loop
from hindsight_system_tests.payloads import extracted, fact, observes

pytestmark = pytest.mark.asyncio

PAGE_NAME = "Alice's current home"
SOURCE_QUERY = "Where does Alice live?"
FIRST_ANSWER = "Alice lives in Berlin."
SECOND_FACT = "Alice moved to Munich"


@pytest.fixture
async def page(client, llm, bank_id, settled):
    llm.on_step("extract_facts", contains="Berlin").returns(
        extracted(fact("Alice moved to Berlin", who="Alice", entities=["Alice", "Berlin"]))
    )
    llm.on_step("consolidate").answers_with(observes("Alice moved to Berlin"))
    reflect_loop(llm, answer=FIRST_ANSWER, query=SOURCE_QUERY)

    await client.aretain(bank_id=bank_id, content="Alice moved to Berlin.")
    await settled(bank_id)
    created = await client.pages.create_page(bank_id, {"name": PAGE_NAME, "source_query": SOURCE_QUERY})
    await settled(bank_id)
    # Change the standing trigger only after the initial page refresh. Supplying a
    # partial trigger on creation makes older API versions choose their full-model
    # default instead of the page delta default, which would leave no watermark.
    await client.pages.update_page_node(
        bank_id, created.page_id, {"trigger": {"refresh_after_consolidation": False}}
    )
    await settled(bank_id)
    return created


async def test_a_refreshed_page_is_safe_only_with_a_source_watermark(client, bank_id, page):
    """A false stale bit alone is not enough: the source watermark is required."""
    model = await client.mental_models.get_mental_model(bank_id, page.mental_model_id, detail="full")

    assert model.is_stale is False
    assert model.last_memory_seen_at is not None
    assert model.last_refresh_failed_at is None


async def test_a_stale_page_requires_an_explicit_raw_recall(client, llm, bank_id, page, settled):
    """A page search reports the old derived layer; raw facts are a separate call."""
    llm.reset()
    llm.on_step("extract_facts", contains="Munich").returns(
        extracted(fact(SECOND_FACT, who="Alice", entities=["Alice", "Munich"]))
    )
    llm.on_step("consolidate").answers_with(observes("Alice moved to Munich"))
    await client.aretain(bank_id=bank_id, content="Alice moved to Munich.")
    await settled(bank_id)

    stale = await client.mental_models.get_mental_model(bank_id, page.mental_model_id, detail="full")
    assert stale.is_stale is True

    # Page search is the derived source and does not refresh it or call the LLM.
    llm.reset()
    found = await client.pages.search_pages(bank_id, q="current home", limit=10)
    assert [result.id for result in found.results] == [page.page_id]
    assert not llm.calls

    # The caller explicitly asks the raw source for the newer fact; no hidden
    # reflect/fallback call is needed or allowed on the page-search path.
    raw = await client.arecall(bank_id=bank_id, query="Where does Alice live now?", types=["world"])
    assert sorted(result.text for result in raw.results) == sorted(
        [
            f"{SECOND_FACT} | Involving: Alice",
            "Alice moved to Berlin | Involving: Alice",
        ]
    )
    assert not llm.calls


async def test_missing_source_watermark_is_unknown_even_for_an_empty_page(client, bank_id, settled):
    """An unwritten page has no evidence that it has seen any source facts."""
    empty = await client.pages.create_page(
        bank_id,
        {"name": "Empty current-state page", "source_query": "What is empty?"},
    )
    await settled(bank_id)

    model = await client.mental_models.get_mental_model(bank_id, empty.mental_model_id, detail="full")
    assert model.last_memory_seen_at is None
    # The API may report no staleness for an empty model, but the absent
    # watermark still means a current-state consumer cannot treat it as safe.
    assert model.is_stale is not True


async def test_search_and_raw_recall_do_not_start_automatic_reflect_or_fallback(
    client, llm, bank_id, page, settled
):
    """Retrieval endpoints compose only when the caller explicitly invokes both."""
    llm.reset()
    llm.on_step("extract_facts", contains="Munich").returns(
        extracted(fact(SECOND_FACT, who="Alice", entities=["Alice", "Munich"]))
    )
    llm.on_step("consolidate").answers_with(observes("Alice moved to Munich"))
    await client.aretain(bank_id=bank_id, content="Alice moved to Munich.")
    await settled(bank_id)
    llm.reset()

    await client.pages.search_pages(bank_id, q="current home", limit=10)
    assert not llm.calls
    await client.arecall(bank_id=bank_id, query="Where does Alice live now?", types=["world"])
    assert not llm.calls
