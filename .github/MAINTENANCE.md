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

The fork keeps ordinary deterministic CI. The common live-provider gate and the
optional Hermes provider key are restricted to upstream. Upstream publishing,
documentation deployment, signing, scheduled quality/performance evaluations and
model-driven triage workflows are disabled through this repository's settings;
these are repository settings, not guards added to every inherited workflow file.
Keep those external-effect workflows disabled unless their execution is explicitly
intended. Fork releases use an explicit, separate artifact path rather than
publishing under upstream package identities.

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
- [x] Complete source review.
- [x] Deliver a focused PR with a verified public diff.

Delivered in [PR #1](https://github.com/chenhaoxiang/hindsight/pull/1), merged as
`ab82994b58fa9b26990ba7a4b69266275db24395`. The reviewed candidate passed
69 targeted API regressions, both refresh-failure blackbox scenarios, lint and
remote CI (including the full blackbox suite). An earlier CI failure exposed a
429-classification regression; it was corrected without weakening the scenario.
This records source delivery only, not a new API installation.

Acceptance: preserve scope isolation, atomic writes, cancellation and source-version
checks. Do not manufacture success by dropping data or disabling consistency checks.
A bounded throughput observation is not a permanent capacity guarantee.

### 2. Correct the Pi adapter and status contracts

- [x] Preserve tool `isError` in the Pi-native result.
- [x] Validate native tool arguments and propagate cancellation to supported HTTP calls.
- [x] Count active work from authoritative aggregation instead of the first page of
      operation history; unavailable/invalid status is unknown, not zero.
- [x] Distinguish service availability, ingestion activity, consolidation backlog and
      page freshness. Keep existing fields compatible where possible.
- [x] Report compiled integration/API identity without leaking configuration values.
- [x] Add synthetic adapter/client tests and deliver a focused PR.

Delivered in [PR #2](https://github.com/chenhaoxiang/hindsight/pull/2), merged as
`2b4f0d527f4af8ad4ca4fa9f79a23f3baab75919`. The runtime candidate passed
1,444 isolated unit tests, full typecheck/lint/build and compiled native smoke on
Node 22 and 26 (not the full Node 26 unit suite). Final exact-head remote CI passed,
including generated documentation and the full blackbox suite. Earlier failures
and the generated-copy/buffered-trace readiness corrections remain in the PR.
No runtime installation is claimed here.

Acceptance: errors remain errors in the host, older pending work cannot disappear
behind a page of completed operations, and cancellation is not described as rollback
of an already-started upstream effect.

### 3. Expose bounded, layered retrieval

- [x] Surface available page freshness, refresh failure and source-watermark metadata;
      missing metadata stays unknown rather than being inferred from page age.
- [x] Expose structured raw-fact recall using the existing client/API, including
      provenance, fact type and supported temporal fields.
- [x] Expose bounded recent-update reads through the existing list APIs. A recall
      temporal-ranking window must not be represented as an exclusive ingestion-time filter.
- [x] Keep bank/authentication binding under the host's control and enforce argument
      and result budgets. Do not let model arguments replace API URLs or credentials.
- [x] Add layered usage guidance while preserving the existing configuration extension point.
- [x] Test stale-page/new-fact, unconsolidated-fact, missing metadata, API failure,
      cancellation and bank/tag-boundary scenarios.
- [x] Keep automatic multi-step fallback out of the initial default path; none is added.
- [x] Complete independent source review and focused PR delivery.

Delivered in [PR #3](https://github.com/chenhaoxiang/hindsight/pull/3), merged as
`d4a819cc841b025938f107a1fd75274312f98f6e`. The final candidate passed
1,487 isolated unit tests, typecheck/lint/build, repeatable generated documentation,
compiled ten-tool smoke on Node 22/26, independent review and normal remote CI.
The microsecond comparison correction preserves the API's write-time precision.
This remains source delivery, not an installed-session acceptance claim.

The explicit tools are `hindsight_recall` and `hindsight_recent_memories`. Recent
reads use the API's memory-row `updated_at` axis; no event-time substitute or
unverifiable creation-time ordering is invented. Agent-facing rosters/body reads
are paged/bounded too; internal seed and automatic-retrieval defaults are retained.

Acceptance: recent facts can be found without waiting for consolidation; raw
statements are attributed evidence, not automatic proof of current business state.
No-result, unsupported capability and unavailable service remain distinct.

### 4. Publish fixed component artifacts and verify installation

- [x] Pin API and Coding Agents sources/artifacts independently.
- [x] Use legal ecosystem-specific versions; test capability and update comparisons.
- [x] Make the maintenance update source explicit so the official updater cannot
      silently replace fork-only changes.
- [x] Build, inspect and scan public artifacts; publish only from verified source.
- [x] Retain rollback artifacts and existing runtime settings/data.
- [x] Verify installation bytes, effective settings, read-only health and actual
      loaded tools in a safe session. Do not force-reload active sessions.

Initial delivery: [PR #4](https://github.com/chenhaoxiang/hindsight/pull/4),
merged as `ca1636a5d8072f8570b7e94ea760e705bc45e566`, and immutable Release
`maintenance-20261106.1`. Its original API identity predates the fork naming
convention below; preserve that release and its source rather than replacing its
assets. Local installation and an isolated native SDK session were verified
separately; this does not claim all existing sessions reloaded.

Acceptance: source, checks, PR merge, Release, disk installation and loaded runtime
are separately identifiable. A rejected or interrupted validation is retained as such.

### Fixed component build inputs

- API: upstream `v1.2.0` commit `34a138684a899b6ec34175f7a2b633860466d289`,
  plus [.github/patches/api-1.2.0-fork.3.patch](patches/api-1.2.0-fork.3.patch)
  for the incremental-retain candidate. Preserve the previously released
  [fork.1 overlay](patches/api-1.2.0-fork.1.patch) and
  [fork.2 overlay](patches/api-1.2.0-fork.2.patch) unchanged.
  Candidate public API identity and release naming are `1.2.0-fork.3`; Python
  distribution metadata and wheel names use the equivalent `1.2.0+fork.3`. PEP 440 rejects
  `1.2.0-fork.3`, while the local-version form satisfies the existing full wrapper
  dependency `==1.2.0`. Do not use a post-release or change that dependency to
  work around naming. The fork.3 overlay changes only nineteen production
  modules and the two version declarations, combining the previous reliability,
  refresh, trace, ranking-input and exact-title fixes with opt-in stable JSONL
  chunk reuse. No newer API schema,
  migration or unrelated main-branch feature is included. Keep regression tests
  bound to the public reliability-fix source as well as the stable API export.
- Coding Agents: `0.8.5` component tree at
  `02e498b1537f6aec14c5a799f5a0c193b8394869` equals its maintenance-start tree;
  the focused maintenance PRs and version/update guard produce `0.8.5-fork.1`.
  Package/lock/plugin versions must agree. Upstream automatic updating must not
  query/install over this maintenance identity, even when staged through npx.

Build only from committed public source into ignored temporary directories. A
fresh API export can be prepared with `git archive` at the exact API base. From
the maintenance repository root, apply the committed zero-context overlay with
`git apply --unidiff-zero --directory=tmp/api-1.2.0-src .github/patches/api-1.2.0-fork.3.patch`.
Use a fresh export and verify the resulting source bytes; this avoids patch-tool
backup files entering the package. Build its
`hindsight-api-slim` wheel there with `uv build --package hindsight-api-slim --wheel`.
The integration uses its committed lockfile, generated skill and `npm run build`,
then `npm pack --ignore-scripts`. These operations are builds, not publication.

Use a separate maintenance Release tag, not upstream `v*`/`integrations/**` tags,
never official npm/PyPI publication. Before attaching assets, inspect expanded
wheel/tar contents, reject `.orig`/`.rej`/`.bak` source residue, scan them and record
public source/patch/artifact hashes.
Local settings, installation paths, credentials, data, logs and rollback copies
are not Release assets.

The upstream API config's `DEFAULT_XAI_OAUTH_CLIENT_ID` can trigger a generic-key
scanner rule. It is a public application identifier used by the device-code flow,
not a client secret or user token. Retain the raw finding; classify only that
specific match after checking that the identifier assignment and OAuth auth
source are byte-identical to the pinned public baseline and confirming the
`client_id` usage. The surrounding config may contain reviewed new settings;
that does not make a different identifier or any actual credential allowable. Do not suppress
other findings or infer that an arbitrary token-like value is safe. Install only verified bytes, keep previous artifacts, and
verify the actual loaded integration/API identities separately from disk labels.

## Approved follow-up order

- [x] Deliver the naming correction as a new immutable `api-1.2.0-fork.1` release;
      verify the Python metadata/display mapping and preserve previous artifacts.
      [PR #5](https://github.com/chenhaoxiang/hindsight/pull/5) merged as
      `c47faf1df7ad202e526ee55467202d0807e3b381`; previous assets remain immutable.
- [ ] Verify potentially missing source documents and recover only confirmed gaps,
      through normal retention with original identity and provenance. No bulk replay.
- [ ] Trial recovery of three to five historical queue/backoff-paused pages, keeping
      prior content, structure, grounding and source watermarks available for rollback.
      Diagnose delta/retrieval failures separately; an HTTP success is not quality acceptance.
- [ ] Compare consolidation batch packing and context cost with isolated fixtures
      before considering a bounded tuning change. Keep scope serialization and
      consistency checks; more concurrency cannot parallelize one shared scope.
- [ ] Bind initiative synthesis to the actual initiative/source scope and make its
      marker visible before initial synthesis. Test competing projects and asynchronous
      retention without relabeling historical facts or splitting banks.
      The `0.8.5-fork.2` candidate uses a dedicated source tag and strict raw-fact
      matching for new pages; it deliberately requires explicit captures for later
      milestones instead of inferring progress from unrelated shared observations.
      Existing pages are not silently migrated. Source waits are bounded/cancellable;
      accepted writes survive cancellation, and no fallback page id is fabricated.
- [ ] Verify source delivery, artifacts, installation and observed recovery separately.

Local operational evidence stays private. No database schema, embedding, inference
model, reasoning-level or global refresh-frequency change is authorized by this
follow-up. Initialization profiling, nested-tool usage accounting and storage
lifecycle tuning remain lower-priority work, not new delivery prerequisites.

## Refresh resilience follow-up

Approved implementation order (no model, reasoning, embedding or schema change):

1. Preserve transient transport, quota/defer and cancellation causes through the
   delta and retraction legs. Never convert a temporary outage into a structural
   refusal that permanently pauses the page. Real schema/reference failures still
   preserve content/structure/grounding/watermarks and fail closed.
2. Expose bounded provider-attempt metadata and real backoff stages, separately
   from logical-call success and worker retry counts. Bound delta editing against
   one stage deadline and the task's remaining budget; keep full synthesis separate.
3. Account for query plus candidate text in a configurable reranker input budget,
   with provider-template/tokenizer headroom and boundary verification. Do not
   increase GPU batch or truncate stored source documents to hide a ranking error.
4. Prefer exact knowledge-page title matches without broadening bank/tag access;
   preserve hybrid search for non-exact queries.
5. Backport to the independently pinned stable API, build a new immutable
   `1.2.0-fork.N` artifact (`1.2.0+fork.N` Python metadata), retain rollback,
   verify installation and recover only small preserved page groups.

Use deterministic/isolated-store and blackbox regressions for the control flow.
Any real quality sample is bounded and judged separately; generated text being
nonempty is not semantic acceptance. Operational traces and real recovery data
stay outside the public repository. No bulk replay, bank split, global relabeling,
worker-concurrency increase or removal of consistency guards is part of this batch.

## Incremental-retain optimization follow-up

Approved order for the next focused batch:

1. Compare internal candidate reads/round caps of `50/100` with `48/96` while
   keeping LLM batches at 8, the current model/reasoning, evidence budgets and
   shared-scope serialization. Check effective overrides and preserve rollback.
   This is a small possible packing gain, not a capacity or quality guarantee.
2. Reuse complete, byte-identical old chunks in source order and re-cut changed
   gaps, avoiding the boundary cascade after historical JSONL insertions/deletions.
   Preserve unchanged fact IDs, embeddings and valid observation evidence while
   atomically maintaining correct chunk positions/references. Real edits/deletions
   still invalidate their sources. Keep CAS, cancellation, retries and explicit
   re-extraction intact; unsupported/ambiguous cases retain the safe old path.
3. Add only the diagnostic counts/reasons needed to distinguish new work from
   reconstruction. Do not log source text, credentials or private paths.
4. Validate with isolated synthetic lifecycle and published-client stories, then
   a bounded quality comparison before activation. Backport to the independently
   pinned stable API and publish a new immutable `-fork.N` component artifact.

Do not change the global splitter of every populated document, the public chunk-ID
format, schema, embedding, model/reasoning, evidence budgets or shared write
concurrency. No whole-bank replay/refresh or silent historical relabeling. The
source-specific retraction-readiness proposal is a separate future contract,
not part of this batch. Preserve input/source integrity instead of treating fewer
calls or nonempty generated text as semantic acceptance.

Source acceptance for this candidate:

- `HINDSIGHT_API_RETAIN_STABLE_CHUNKS` is off by default and bank-configurable.
  PostgreSQL reuse is limited to one JSONL document of at most 2 Mi characters
  and 512 chunks. Attachments, ambiguous anchors, unsupported stores and changed
  extraction context/strategy are not silently reused. Normal forced reprocessing
  still extracts the source again.
- Relocation keeps fact UUIDs, text, embeddings, creation/update/consolidation times
  and observation evidence unchanged. Genuine edits still invalidate their facts
  and observations. Rollback, task cancellation, stale same-hash recovery,
  concurrent append and cross-bank isolation have dedicated store regressions.
- A committed queued stable delta records bounded `chunk_reuse` counts on its
  executing operation; a receipt parent is not an aggregate of these counts.
  Metadata-only/no-op retains intentionally omit them: absence is not a zero-work
  or reuse aggregate. Source text is not added to these diagnostic fields.
- Recovery binds hashes to chunk positions and rechecks layout and extraction
  context/strategy under the document lock, including after the flag is disabled.
  A partial stable layout resumes its verified positions through the bounded
  producer/consumer pipeline. Incompatible layouts retire old sources normally;
  all three SQL streaming write entrances (including a 0-fact consumer batch)
  recheck under the lock. Concurrent append conflicts retain their typed retry/
  defer behavior instead of becoming terminal generic failures.
  Staging prelocks the union of document facts, retiring observations and their
  surviving co-sources in one UUID order, revalidating expansions before writes.
  The standard obsolete-source sweep then runs before moving facts; lock-only
  moving sources never become retirement inputs. This covers shared-source
  observations split across more than one observation row.
- The internal packing comparison is not adopted: mechanical packing tests alone
  cannot establish semantic equivalence. Retain the previous configuration when
  a bounded quality trial is inconclusive; do not rewind memory data.
- Real-model synthetic extraction and independent semantic adjudication are
  separate from deterministic lifecycle tests. The narrow sample covers dated
  corrections, project identity, budgets and proposal/report authority; it is not
  a whole-bank or consolidation-quality score. Retain failed harness attempts and
  reuse completed outputs instead of repeatedly resampling paid extraction.

Stable backport, independent review, PR/CI merge, immutable artifact publication,
installation and feature activation remain separate delivery steps.

## Completion record

Update the relevant checkboxes when the associated work is actually complete. Keep
remaining gaps explicit. Remove local patches when upstream supplies an equivalent,
verified implementation rather than preserving duplicate behavior indefinitely.
