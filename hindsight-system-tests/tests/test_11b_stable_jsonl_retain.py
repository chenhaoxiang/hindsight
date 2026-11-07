"""History insertion preserves untouched facts/observations through the public client.

A tiny outer token threshold forces the large-document seam, while extraction,
consolidation and graph maintenance all run normally against explicit stubs.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from hindsight_client import Hindsight

from hindsight_system_tests import start_hindsight_server, wait_until_settled
from hindsight_system_tests.payloads import FactConsolidation, FactEntry, extracted, fact

pytestmark = pytest.mark.asyncio
DOCUMENT = "fixture-conversation"


async def test_insert_append_and_correction_keep_the_right_source_identities(stub_server, llm, tmp_path) -> None:
    expected = {
        index: f"Fixture {index:03d}: the policy is enabled and only applies to its named fixture."
        for index in range(60)
    }

    def line(index: int) -> str:
        return json.dumps({"role": "user", "content": expected[index]})

    def extract(request):
        texts = re.findall(r'"content": "(Fixture \d+: [^"]+)"', request.user_text)
        assert texts, "the declared extractor must see actual JSONL source turns"
        for text in texts:
            identity = int(re.search(r"Fixture (\d+):", text).group(1))
            assert text == expected[identity]
        return extracted(*(fact(text) for text in texts))

    def consolidate(request) -> FactConsolidation:
        section = request.user_text.split("### New facts", 1)[1].split("### Existing observations", 1)[0]
        rows = re.findall(r"(?m)^\[(F\d+)\] (Fixture (\d+): [^(\n]+)", section)
        assert rows
        for _, text, number in rows:
            assert text.strip() == expected[int(number)]
        return FactConsolidation(
            facts=[
                FactEntry(fact_id=identity, about=f"Fixture {number}", adds="yes", new=[text.strip()])
                for identity, text, number in rows
            ]
        )

    llm.on_step("extract_facts").answers_with(extract)
    llm.on_step("consolidate").answers_with(consolidate)
    key = uuid.uuid4().hex[:12]
    bank = f"systest-stable-{key}"
    server = start_hindsight_server(
        stub_url=stub_server.url,
        log_path=tmp_path / "stable-server.log",
        extra_env={
            "HINDSIGHT_API_DATABASE_SCHEMA": f"stable_{key}",
            "HINDSIGHT_API_RETAIN_BATCH_TOKENS": "500",
            # Similarity adjudication has its own stories. These structurally equal
            # synthetic policies must remain independently inspectable observations.
            "HINDSIGHT_API_CONSOLIDATION_DEDUP_THRESHOLD": "1.0",
        },
    )
    client = Hindsight(base_url=server.url)
    try:
        await client.banks.update_bank_config(
            bank,
            {
                "updates": {
                    "retain_stable_chunks": True,
                    "retain_chunk_size": 600,
                    "retain_structured_chunk_size": 600,
                    "consolidation_llm_batch_size": 8,
                }
            },
        )
        lines = [line(index) for index in range(60)]

        async def retain(body: str, mode: str = "replace", queued: bool = False) -> dict:
            receipt = await client.aretain(
                bank_id=bank,
                document_id=DOCUMENT,
                content=body,
                update_mode=mode,
                observation_scopes="shared",
                retain_async=queued,
            )
            await wait_until_settled(client, bank)
            if queued:
                status = await client.operations.get_operation_status(bank, receipt.operation_id)
                assert status.status == "completed", status.error_message
                if status.child_operations:
                    # Large queued submissions use a receipt parent. Counts belong
                    # to the executing retain, not an invented parent aggregate.
                    assert len(status.child_operations) == 1
                    status = await client.operations.get_operation_status(bank, status.child_operations[0].operation_id)
                    assert status.status == "completed", status.error_message
                return status.result_metadata
            return {}

        async def raw():
            result = await client.memory.list_memories(bank, type="world", document_id=DOCUMENT, limit=200)
            assert result.total == len(result.items)
            return {item.text: item for item in result.items}

        await retain("\n".join(lines))
        before = await raw()
        # Deliberately outside the growing tail, which is allowed to be repacked.
        suffix = before[expected[45]]
        observations = await client.memory.list_memories(bank, type="observation", q=expected[45], limit=20)
        assert observations.total == 1
        suffix_observation = observations.items[0].id

        expected[999] = "Fixture 999: the inserted policy is disabled, not enabled."
        lines.insert(20, line(999))
        metadata = await retain("\n".join(lines), queued=True)
        reuse = metadata["chunk_reuse"]
        assert reuse["reused_chunks"] >= reuse["old_chunks"] - 2
        assert reuse["retired_chunks"] <= 2 and reuse["extraction_chunks"] <= 2
        assert reuse["moved_chunks"] > 0 and reuse["moved_fact_refs"] > 0
        assert set(reuse) == {
            "old_chunks",
            "new_chunks",
            "reused_chunks",
            "moved_chunks",
            "retired_chunks",
            "extraction_chunks",
            "extraction_chars",
            "moved_fact_refs",
        }
        after_insert = await raw()
        assert set(after_insert) == set(expected.values())
        assert after_insert[expected[45]].id == suffix.id
        assert (
            sum(text in after_insert and item.id == after_insert[text].id for text, item in before.items())
            >= len(before) - 6
        )
        observations = await client.memory.list_memories(bank, type="observation", q=expected[45], limit=20)
        assert [item.id for item in observations.items] == [suffix_observation]

        expected[1000] = "Fixture 1000: a final appended policy is disabled."
        tail = line(1000)
        await retain(tail, "append")
        lines.append(tail)
        after_append = await raw()
        assert set(after_append) == set(expected.values())
        assert after_append[expected[45]].id == suffix.id

        old_text = expected[45]
        expected[45] = "Fixture 045: the corrected policy is prohibited, not enabled."
        lines = [line(45) if old_text in text else text for text in lines]
        await retain("\n".join(lines))
        corrected = await raw()
        assert set(corrected) == set(expected.values())
        assert old_text not in corrected
        assert corrected[expected[45]].id != suffix.id
        stale = await client.memory.list_memories(bank, type="observation", q=old_text, limit=20)
        assert stale.total == 0
        observations = await client.memory.list_memories(bank, type="observation", limit=200)
        assert sorted(item.text for item in observations.items) == sorted(expected.values())
        pending = await client.memory.list_memories(bank, consolidation_state="pending", limit=1)
        assert pending.total == 0
        document = await client.documents.get_document(bank, DOCUMENT)
        assert document.original_text == "\n".join(lines)
        # Same-body replay neither changes identities nor invents new sources.
        noop_metadata = await retain(document.original_text, queued=True)
        assert "chunk_reuse" not in noop_metadata  # metadata-only is intentionally not an aggregate
        assert noop_metadata["unit_ids_count"] == 0
        assert {text: item.id for text, item in (await raw()).items()} == {
            text: item.id for text, item in corrected.items()
        }
    finally:
        await client.banks.delete_bank(bank)
        await client.aclose()
        server.stop()
