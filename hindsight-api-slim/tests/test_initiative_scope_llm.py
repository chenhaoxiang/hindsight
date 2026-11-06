"""Real-model counterpart to the deterministic initiative evidence-scope story.

The blackbox test pins which facts reach synthesis; this one judges whether the
synthesis preserves planned-vs-delivered status without borrowing another plan.
"""

import uuid

import pytest

from hindsight_api import MemoryEngine, RequestContext
from tests.llm_judge import assert_meets_criteria

pytestmark = [pytest.mark.asyncio, pytest.mark.hs_llm_core]


async def test_scoped_initiative_does_not_promote_a_plan_to_delivery(
    memory_real_llm: MemoryEngine, request_context: RequestContext
) -> None:
    memory = memory_real_llm
    bank = "test-initiative-scope-" + uuid.uuid4().hex
    source_tag = "initiative:" + str(uuid.uuid4())
    try:
        await memory.retain_batch_async(
            bank_id=bank,
            contents=[
                {
                    "content": "Atlas cache repair is an approved plan. Implementation has not started and nothing was deployed.",
                    "document_id": "initiative-source-atlas",
                    "tags": ["knowledge:feature-work", source_tag],
                    "observation_scopes": "shared",
                },
                {
                    "content": "The separate Boreal cache repair was implemented and deployed yesterday. It is not the Atlas initiative.",
                    "document_id": "initiative-source-boreal",
                    "tags": ["knowledge:feature-work", "initiative:other"],
                    "observation_scopes": "shared",
                },
            ],
            request_context=request_context,
        )
        await memory.wait_for_background_tasks()
        page = await memory.create_knowledge_page(
            bank_id=bank,
            name="Atlas cache repair",
            source_query=(
                'Summarize only the "Atlas cache repair" initiative, anchored by source document '
                "initiative-source-atlas. Its planned scope is to repair Atlas's cache. "
                "Use its captured source and updates as evidence; distinguish plans from verified delivery. "
                "Do not include adjacent initiatives or other projects merely because their terminology overlaps."
            ),
            content="",
            tags=["knowledge:feature-work", source_tag],
            trigger={
                "tags_match": "all_strict",
                "fact_types": ["world", "experience"],
                "exclude_mental_models": True,
                "refresh_after_consolidation": False,
            },
            request_context=request_context,
        )
        assert page is not None
        model = await memory.refresh_mental_model(
            bank_id=bank, mental_model_id=page["mental_model_id"], request_context=request_context
        )
        await assert_meets_criteria(
            response=model["content"],
            criteria=(
                "The summary is about Atlas's planned cache repair. It must not say implementation "
                "has started or deployment is complete. It must not import Boreal's delivery as "
                "Atlas's status or expand into a separate overview of Boreal's initiative."
            ),
            context="Atlas is an approved but unimplemented plan. Boreal is separate and deployed. Only Atlas's source is in scope.",
        )
    finally:
        await memory.delete_bank(bank, request_context=request_context)
