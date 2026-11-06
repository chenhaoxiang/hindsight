"""Full-title searches find empty pages, without escaping their bank or tags."""

import pytest

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("title", ["Fixture maintenance and layered retrieval", "中文维护计划与分层检索"])
async def test_exact_title_precedes_hybrid_ranking_and_respects_tags(client, bank_id, settled, title: str) -> None:
    await client.banks.update_bank_config(bank_id, {"updates": {"enable_text_search": False}})
    page = await client.knowledge_base.create_knowledge_page(
        bank_id, {"name": title, "source_query": "Fixture scope?", "tags": ["scope:allowed"]}
    )
    await client.knowledge_base.create_knowledge_page(
        bank_id, {"name": "Related fixture", "source_query": title, "tags": ["scope:other"]}
    )
    await settled(bank_id)
    found = await client.knowledge_base.search_knowledge_base(bank_id, q=f"  {title.upper()}  ", limit=1)
    assert found.total == 1 and len(found.results) == 1
    hit = found.results[0]
    assert hit.id == page.page_id
    assert hit.name == title
    assert hit.source_query == "Fixture scope?"
    assert hit.snippet == "No content yet."
    assert hit.score == 1.0
    assert hit.mental_model_id == page.mental_model_id
    assert hit.updated_at is not None
    filtered = await client.knowledge_base.search_knowledge_base(
        bank_id, q=title, limit=1, tags=["scope:other"], tags_match="all_strict"
    )
    assert all(result.id != page.page_id for result in filtered.results)
    missing_bank = await client.acreate_bank(bank_id + "-isolated")
    try:
        empty = await client.knowledge_base.search_knowledge_base(missing_bank.bank_id, q=title, limit=1)
        assert empty.total == 0 and empty.results == []
    finally:
        await client.banks.delete_bank(missing_bank.bank_id)
