"""Bounded real-model baseline/candidate extraction quality after history insertion.

The deterministic lifecycle suite proves ID/reference/transaction mechanics. This
separate judge test checks that the changed extraction grouping still preserves
negation, project identity and source authority. It is not a whole-bank score.
"""

import json
import uuid
from dataclasses import replace
from unittest.mock import patch

import pytest

from hindsight_api.config import _get_raw_config
from tests.llm_judge import assert_meets_criteria
from tests.test_consolidation_stale_batch import cross_encoder, embeddings  # noqa: F401 (fixtures)

pytestmark = [pytest.mark.asyncio, pytest.mark.hs_llm_core]

CONTEXT = (
    "Synthetic conversation. User statements are user decisions; assistant proposals and claimed checks "
    "must remain proposals/reports, never proof of user approval or production deployment. "
    "Keep project identity and dated corrections explicit."
)
TURNS = [
    {
        "role": "user",
        "content": "For project Cedar, the test cache TTL is 30 minutes as of 1 January 2026. This is only the Cedar TEST environment; production is not covered and no automatic deployment is approved.",
    },
    {
        "role": "assistant",
        "content": "I propose enabling automatic deployment for project Cedar, but that is only my proposal. I have not deployed Cedar. The user has not approved this proposal, and the TEST-only permission must not be turned into production approval.",
    },
    {
        "role": "user",
        "content": "For project Maple, retain billing audit records for 90 days. Maple is a different project from Cedar; its billing retention rule must not be applied as Cedar's cache TTL or deployment policy.",
    },
    {
        "role": "assistant",
        "content": "I reported passing synthetic unit tests for project Maple. I did not verify the real host and I did not deploy Maple. This is the assistant's reported test result, not evidence that Maple is live.",
    },
    {
        "role": "user",
        "content": "Project Cedar has a separate TEST-only budget of 50 synthetic units. Unknown charges count toward this limit; do not borrow project Maple's budget. No production spending is authorized by this rule.",
    },
    {
        "role": "user",
        "content": "Project Maple requires manual approval before a production deployment. Keep this Maple rule separate from the unapproved assistant proposal for Cedar; neither project has been declared deployed by this conversation.",
    },
]
CORRECTION = {
    "role": "user",
    "content": "Correction for project Cedar as of 2 January 2026: replace the earlier 30-minute TEST cache TTL with 45 minutes. This correction is for Cedar TEST only, not Maple, and it does not approve automatic or production deployment.",
}


@pytest.mark.parametrize("stable", [False, True], ids=["baseline", "stable-candidate"])
async def test_history_insertion_preserves_project_correction_and_authority(
    memory_real_llm, request_context, monkeypatch, tmp_path, stable: bool
) -> None:
    memory = memory_real_llm
    bank = "quality-stable-" + uuid.uuid4().hex[:10]
    document = "synthetic-conversation"
    await memory.ensure_bank_profile(bank, request_context=request_context)
    config = replace(
        _get_raw_config(),
        retain_stable_chunks=stable,
        retain_chunk_size=650,
        retain_structured_chunk_size=650,
        retain_extraction_mode="concise",
        enable_observations=False,
    )
    monkeypatch.setattr(_get_raw_config(), "retain_batch_tokens", 100)
    before_body = "\n".join(json.dumps(turn) for turn in TURNS)
    changed = [*TURNS[:2], CORRECTION, *TURNS[2:]]
    after_body = "\n".join(json.dumps(turn) for turn in changed)
    try:
        with patch.object(memory._config_resolver, "resolve_full_config", return_value=config):
            await memory.retain_async(
                bank_id=bank,
                document_id=document,
                content=before_body,
                context=CONTEXT,
                request_context=request_context,
            )
            before = await memory.list_memory_units(
                bank, document_id=document, limit=200, request_context=request_context
            )
            await memory.retain_async(
                bank_id=bank, document_id=document, content=after_body, context=CONTEXT, request_context=request_context
            )
            after = await memory.list_memory_units(
                bank, document_id=document, limit=200, request_context=request_context
            )
        stored = await memory.get_document(document, bank, request_context=request_context)
        assert stored["original_text"] == after_body
        assert after["total"] == len(after["items"])
        (tmp_path / "quality-facts.json").write_text(
            json.dumps({"stable": stable, "before": before, "after": after}, ensure_ascii=False, indent=2, default=str)
        )
        response = "\n".join(f"[{item['fact_type']}] {item['text']}" for item in after["items"])
        verdict = await assert_meets_criteria(
            response=response,
            context=after_body,
            criteria=(
                "The stored facts record the 2 January 2026 correction: project Cedar's TEST cache TTL is45 minutes, "
                "replacing its earlier30-minute value (earlier facts may remain as historical evidence). They keep "
                "Maple's90-day billing audit retention separate. Cedar's50-unit TEST-only budget is preserved, "
                "including that unknown charges count. They must NOT turn the assistant's proposed Cedar auto-deploy "
                "into user approval or actual deployment; they must NOT state Maple is deployed or its real host "
                "verified based on the assistant's synthetic-test report. Important source limits and project "
                "identities are retained without conflating the two projects. Judge semantic meaning, not exact phrasing."
            ),
        )
        (tmp_path / "quality-verdict.json").write_text(verdict.model_dump_json(indent=2))
    finally:
        await memory.delete_bank(bank, request_context=request_context)
