"""Internal read/round alignment drains every source without changing LLM batch size.

Each server owns an isolated schema: a worker on the suite's default schema must
not accidentally execute the candidate with the baseline's global read limit.
"""

from __future__ import annotations

import re
import uuid

import pytest
from hindsight_client import Hindsight

from hindsight_system_tests import start_hindsight_server, wait_until_settled
from hindsight_system_tests.payloads import FactConsolidation, FactEntry, extracted, fact

pytestmark = pytest.mark.asyncio
FACTS = [f"Fixture policy {index:03d} is {'disabled' if index % 2 else 'enabled'}." for index in range(100)]


@pytest.mark.parametrize(
    ("read_size", "round_size", "expected_batches"),
    [(50, 100, [8] * 6 + [2] + [8] * 6 + [2]), (48, 96, [8] * 12 + [4])],
)
async def test_alignment_preserves_every_fact_and_background_round(
    stub_server, llm, tmp_path, read_size: int, round_size: int, expected_batches: list[int]
) -> None:
    llm.on_step("extract_facts").returns(extracted(*(fact(text) for text in FACTS)))
    seen: list[str] = []
    batches: list[int] = []

    def consolidate(request) -> FactConsolidation:
        section = request.user_text.split("### New facts", 1)[1].split("### Existing observations", 1)[0]
        entries = re.findall(r"(?m)^\[(F\d+)\] (Fixture policy \d{3} is (?:enabled|disabled)\.)", section)
        assert entries
        batches.append(len(entries))
        seen.extend(text for _, text in entries)
        return FactConsolidation(
            facts=[FactEntry(fact_id=identity, about=text, adds="yes", new=[text]) for identity, text in entries]
        )

    llm.on_step("consolidate").answers_with(consolidate)
    key = uuid.uuid4().hex[:12]
    bank = f"systest-packing-{key}"
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path / "packing-server.log",
        extra_env={
            "HINDSIGHT_API_DATABASE_SCHEMA": f"packing_{key}",
            "HINDSIGHT_API_CONSOLIDATION_BATCH_SIZE": str(read_size),
            "HINDSIGHT_API_CONSOLIDATION_MAX_MEMORIES_PER_ROUND": str(round_size),
            # Similarity adjudication is a separate story: these equal-form synthetic
            # policies must remain independent observations. Static/server-level only.
            "HINDSIGHT_API_CONSOLIDATION_DEDUP_THRESHOLD": "1.0",
        },
    )
    client = Hindsight(base_url=server.url)
    try:
        await client.banks.update_bank_config(
            bank,
            {
                "updates": {
                    "retain_chunk_size": 20000,
                    "consolidation_llm_batch_size": 8,
                }
            },
        )
        await client.aretain(bank_id=bank, content="\n".join(FACTS), observation_scopes="shared")
        await wait_until_settled(client, bank)
        pending = await client.memory.list_memories(bank, consolidation_state="pending", limit=1)
        failed = await client.memory.list_memories(bank, consolidation_state="failed", limit=1)
        raw = await client.memory.list_memories(bank, type="world", limit=200)
        observations = await client.memory.list_memories(bank, type="observation", limit=200)
        assert pending.total == failed.total == 0
        assert raw.total == observations.total == 100
        assert sorted(memory.text for memory in raw.items) == sorted(FACTS)
        assert sorted(memory.text for memory in observations.items) == sorted(FACTS)
        assert sorted(seen) == sorted(FACTS)  # No omissions or repeated model processing.
        assert batches == expected_batches
    finally:
        await client.banks.delete_bank(bank)
        await client.aclose()
        server.stop()
