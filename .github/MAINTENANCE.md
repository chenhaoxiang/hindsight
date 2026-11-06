---
doc_type: plan
project: hindsight
status: active
truth_mode: maintained
created: 2026-11-06
ssot: true
---

# Thin maintenance fork: reliability and Pi memory integration

This document is the implementation plan for this fork. It records scope,
acceptance and delivery state; a completed source change is not an installation or
runtime acceptance claim.

## Scope and invariants

- Keep upstream structure and reuse the existing Hindsight client, authentication,
  bank resolution, incremental retention and memory engine.
- Maintain the API reliability changes separately from the Coding Agents package.
  The initial component baselines are API `1.2.0` and Coding Agents `0.8.5`.
  A monorepo branch tip is not a substitute for either published component baseline.
- Do not change the database schema, embedding model/dimensions/prefixes, configured
  inference model, or reasoning level as part of this plan.
- Preserve default search behavior and `autoInject=none` where explicitly selected.
  No mandatory per-turn reflection, new judgment model or always-on controller.
- Do not split existing banks, relabel historical memories, clear page content,
  erase failure records or replay all failed operations.
- Upstream is a read-only synchronization source. Changes land through focused
  branches, applicable checks and normal PR merges, not direct pushes to `main`.

## Public repository boundary

Only generic implementation, necessary documentation and synthetic fixtures may be
published. Never copy a local runtime directory or its temporary evidence into this
repository.

Do not publish credentials, non-example environment/configuration files, private
service endpoints, machine/user paths, internal project material, real transcripts,
memory documents, screenshots, logs, diagnostics, database dumps or backups.

The fork keeps ordinary deterministic CI. Live-provider CI is gated to upstream;
upstream publishing, documentation deployment, signing and model-driven triage
workflows are disabled for this maintenance fork. Fork releases use an explicit,
separate artifact path rather than publishing under upstream package identities.

Review the complete staged diff and outgoing commit range before a push. Review PR
text, CI output, archives and Release attachments too: they are public output, not
an exception to the boundary. Use isolated test stores and simulated upstreams.
Runtime-specific verification evidence remains outside public commits and artifacts.

## Execution order

### 0. Establish the maintenance workspace and baseline

- [x] Clone the public fork without moving an existing runtime checkout.
- [x] Keep the canonical checkout on `main`; make tracked changes in a dedicated worktree.
- [x] Record this plan and the no-sensitive-data boundary.
- [x] Verify upstream/component baseline mapping and preserve existing changes.

The API source baseline is upstream tag `v1.2.0` (commit
`34a138684a899b6ec34175f7a2b633860466d289`). Coding Agents `0.8.5` is
independently pinned to npm source commit
`02e498b1537f6aec14c5a799f5a0c193b8394869`. Do not turn an integration update
into an implicit whole-API upgrade.

### 1. Preserve the API reliability fixes

Move only the generic fixes and their regression tests:

- [x] Treat structured invalid-request errors as deterministic even when a proxy
      wraps them in HTTP 502.
- [x] Do not repeat an exhausted provider transport retry budget in the reflect loop.
- [x] A refresh waiting for local LLM permits must not acquire a permanent failure pause.
- [x] Bound refresh execution admission by the configured refresh capacity; keep
      overflow pending and queue-timeout retries finite.
- [x] Give consolidation groups their fair reservation, then fill unused capacity.
- [x] After source facts change, discard stale results and re-read the facts rather
      than repeatedly asking the model about the same obsolete snapshot.
- [x] Run deterministic and isolated-store regressions and lint.
- [ ] Complete source review.
- [ ] Deliver a focused PR with a verified public diff.

Acceptance: preserve scope isolation, atomic writes, cancellation and source-version
checks. Do not manufacture success by dropping data or disabling consistency checks.
A bounded throughput observation is not a permanent capacity guarantee.

### 2. Correct the Pi adapter and status contracts

- [ ] Preserve tool `isError` in the Pi-native result.
- [ ] Validate native tool arguments and propagate cancellation to supported HTTP calls.
- [ ] Count active work from authoritative aggregation instead of the first page of
      operation history; unavailable/invalid status is unknown, not zero.
- [ ] Distinguish service availability, ingestion activity, consolidation backlog and
      page freshness. Keep existing fields compatible where possible.
- [ ] Report compiled integration/API identity without leaking configuration values.
- [ ] Add synthetic adapter/client tests and deliver a focused PR.

Acceptance: errors remain errors in the host, older pending work cannot disappear
behind a page of completed operations, and cancellation is not described as rollback
of an already-started upstream effect.

### 3. Expose bounded, layered retrieval

- [ ] Surface available page freshness, refresh failure and source-watermark metadata;
      missing metadata stays unknown rather than being inferred from page age.
- [ ] Expose structured raw-fact recall using the existing client/API, including
      provenance, fact type and supported temporal fields.
- [ ] Expose bounded recent-update reads through the existing list APIs. A recall
      temporal-ranking window must not be represented as an exclusive ingestion-time filter.
- [ ] Keep bank/authentication binding under the host's control and enforce argument
      and result budgets. Do not let model arguments replace API URLs or credentials.
- [ ] Add layered usage guidance through the existing configuration extension point.
- [ ] Test stale-page/new-fact, unconsolidated-fact, missing metadata, API failure,
      cancellation and bank/tag-boundary scenarios.
- [ ] Keep automatic multi-step fallback opt-in and out of the initial default path.

Acceptance: recent facts can be found without waiting for consolidation; raw
statements are attributed evidence, not automatic proof of current business state.
No-result, unsupported capability and unavailable service remain distinct.

### 4. Publish fixed component artifacts and verify installation

- [ ] Pin API and Coding Agents sources/artifacts independently.
- [ ] Use legal ecosystem-specific versions; test capability and update comparisons.
- [ ] Make the maintenance update source explicit so the official updater cannot
      silently replace fork-only changes.
- [ ] Build, inspect and scan public artifacts; publish only from verified source.
- [ ] Retain rollback artifacts and existing runtime settings/data.
- [ ] Verify installation bytes, effective settings, read-only health and actual
      loaded tools in a safe session. Do not force-reload active sessions.

Acceptance: source, checks, PR merge, Release, disk installation and loaded runtime
are separately identifiable. A rejected or interrupted validation is retained as such.

## Deferred work

Initialization profiling, nested-tool usage accounting, input/consolidation capacity
planning and grouped recovery of older paused pages are separate follow-ups. They
must not be silently included as a bulk migration or a replay of real operations.

## Completion record

Update the relevant checkboxes when the associated work is actually complete. Keep
remaining gaps explicit. Remove local patches when upstream supplies an equivalent,
verified implementation rather than preserving duplicate behavior indefinitely.
