---
name: hindsight-coding-agent
description: How this machine's Hindsight coding-agent memory works — the plugin behind the 🧠 banner. Use when the user says "store/remember this in hindsight", asks what the memory/pages are, wants to configure per-repo memory (disable, rename banks, git depth), or something memory-related looks broken.
---

# Hindsight Coding-Agent Memory

This machine runs the `hindsight-coding-agents` plugin: long-term project memory for coding
sessions, backed by a Hindsight server. You (the agent) are already wired into it — this skill
explains what happens automatically, which tools you have, and how to configure or debug it.

## What happens automatically (no action needed)

- **Per-repo memory bank**: each repository resolves to a bank (shown in the session banner:
  `↳ memory bank “coding-agent::<repo>”`). Worktrees share the main repo's bank.
- **Ingestion builds itself**: on first open, the bank is seeded from recent commit messages and a
  read-only codebase survey; every session start, a background engine tops it up (new commits, new
  conversations) and keeps 5 pages current. There is NO ingest command to run.
- **Session synthesis**: by default, the first prompt of a session triggers one deep memory
  synthesis (`reflect`) injected into context. `autoInject` switches the source: `pages` (knowledge
  page search hits), `recall` (recalled observations), or `none` (nothing injected; the agent
  searches pages first, checks raw/recent facts when they lag, and reflects only for deeper reasoning).
- **Write-back**: the session transcript is retained into the bank automatically at session end
  (per-turn on opencode). The user never needs to "save" a conversation.

## Storing things deliberately

When the user says "store this in hindsight" / "remember this":

- The **current conversation** is captured automatically at session end — say so; no tool needed.
- An **external document, notes, or durable findings** → `hindsight_ingest_document(title, content)`.
- A **new feature/initiative being started** → `hindsight_capture_initiative(title, summary)`,
  right after the plan is agreed and before code is written.
- A **plan that materially changed** (goal, scope, or rationale — including mid-implementation) →
  call `hindsight_capture_initiative` again with `relates_to_page_id` set to that initiative's page
  id, summarising the _current_ intent. Same page, updated plan — never a second page. Trivial
  course-corrections don't count.
- **Verified milestones or completion** of an already tracked initiative → update that same page
  with `relates_to_page_id` and evidence. New scoped initiative pages read explicit captures, not
  unrelated shared-bank observations; ordinary session write-back does not advance their scope.
  Creation can wait up to two minutes for source extraction. A pending/error result is not a page
  success, and cancellation does not roll back an already accepted source retain.

## Retrieving

- `hindsight_search_knowledge_pages(query)` — FIRST STOP for project questions (components,
  conventions, past decisions, initiatives). Server-side hybrid search, fast.
- `hindsight_read_knowledge_page(page_id)` / `hindsight_list_knowledge_pages` — bounded reads;
  follow `next_offset` for the rest. Check reported freshness/failure/source-watermark metadata.
  Missing metadata is unknown; scoped write freshness does not observe deletions or prove correctness.
- `hindsight_recall(query)` — structured source facts when pages are stale, failed or too generic;
  raw facts can be searchable before consolidation, but extraction itself may still be pending.
- `hindsight_recent_memories(since)` — bounded recent memory-row updates, filtered by `updated_at`,
  not event dates. ISO bounds require a timezone; `since` inclusive, `until` exclusive. Truncated
  results are not exhaustive. Recall's temporal window ranks; it is not a strict write-time filter.
- `hindsight_reflect(query)` — deep reasoning over the whole memory for WHY questions and exact
  decided values; slower (seconds), use deliberately.
- Credit visibly whenever memory informs an answer: start that part with
  `🧠 From Hindsight memory (<page>): …` — and never credit memory that didn't contribute.

## Correcting wrong or stale memory

If you verify that something Hindsight served is wrong or outdated (the code, git, or an external
source contradicts it), FIX THE RECORD — don't just ignore it. Call
`hindsight_ingest_document` with:

- **title**: `Correction: <topic>` (e.g. `Correction: retry policy 4xx set`)
- **content**: (1) what memory claimed, (2) what is verifiably true now, (3) the evidence you
  checked (file/commit/output). Quote exact values verbatim.

Corrections are retained asynchronously. Their facts become searchable after extraction, while
knowledge pages may still show the older synthesis until they refresh. Do not promise permanent
ranking precedence or immediate page replacement. Verify important claims against current code and
source evidence, attribute records to their actual project/document, and correct a verified stale
claim rather than leaving it unqualified for the next session.
