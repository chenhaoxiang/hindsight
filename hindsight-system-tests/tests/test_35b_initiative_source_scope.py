"""Visible captured facts -> strict raw-fact page, without a competing initiative.

The integration's ordering and cancellation live in its client tests. This story
checks the backend seam its request composes: settled extraction, shared
consolidation, then a page that must not widen to other plans or observations.
It checks the evidence delivered to the synthesizer, not a model's judgment.
"""

from __future__ import annotations

import pytest

from hindsight_system_tests import reflect_loop
from hindsight_system_tests.payloads import consolidation, extracted, fact

pytestmark = pytest.mark.asyncio

SOURCE_TAG = "initiative:11111111-2222-5333-8444-555555555555"
OTHER_TAG = "initiative:aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"
SOURCE_FACT = "Atlas cache repair is planned, not yet deployed"
OTHER_FACT = "Boreal cache repair was deployed yesterday"
ANSWER = "Atlas cache repair is planned, not yet deployed."


async def test_scoped_initiative_page_reads_only_its_visible_source(client, llm, bank_id, settled):
    await client.banks.update_bank_config(
        bank_id, {"updates": {"enable_observations": True, "enable_auto_consolidation": True}}
    )
    llm.on_step("extract_facts", contains="Atlas").returns(extracted(fact(SOURCE_FACT, entities=["Atlas"])))
    llm.on_step("extract_facts", contains="Boreal").returns(extracted(fact(OTHER_FACT, entities=["Boreal"])))
    llm.on_step("consolidate").returns(consolidation())
    reflect_loop(llm, answer=ANSWER, query="Atlas cache repair")

    await client.aretain(
        bank_id=bank_id,
        content=SOURCE_FACT,
        document_id="initiative-source-atlas",
        tags=["knowledge:feature-work", SOURCE_TAG],
        observation_scopes="shared",
    )
    await client.aretain(
        bank_id=bank_id,
        content=OTHER_FACT,
        document_id="initiative-source-boreal",
        tags=["knowledge:feature-work", OTHER_TAG],
        observation_scopes="shared",
    )
    await settled(bank_id)
    visible = await client.memory.list_memories(bank_id, document_id="initiative-source-atlas", limit=1)
    assert visible.total == 1
    assert [item.text for item in visible.items] == [SOURCE_FACT]

    page = await client.acreate_knowledge_page(
        bank_id,
        name="Atlas cache repair",
        source_query="Summarize only Atlas cache repair, anchored by initiative-source-atlas.",
        tags=["knowledge:feature-work", SOURCE_TAG],
        trigger={
            "fact_types": ["world", "experience"],
            "tags_match": "all_strict",
            "exclude_mental_models": True,
            "refresh_after_consolidation": False,
        },
    )
    await settled(bank_id)
    read = await client.aget_knowledge_page(bank_id, page.page_id)
    assert read.body == ANSWER + "\n"
    prompts = "\n".join(llm.prompts_for("reflect"))
    assert SOURCE_FACT in prompts, "the page never received its captured source"
    assert OTHER_FACT not in prompts, "the page received another initiative's evidence"
    model = await client.mental_models.get_mental_model(bank_id, page.mental_model_id, detail="full")
    assert model.trigger.tags_match == "all_strict"
    assert model.trigger.fact_types == ["world", "experience"]
    assert model.trigger.exclude_mental_models is True
