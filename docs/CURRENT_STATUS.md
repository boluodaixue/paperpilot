# PaperPilot Current Status

Last updated: 2026-09-04

Active development branch: `codex/unified-conversation-orchestrator`

Clean frozen product/research baseline: `4cbbb8f` on
`codex/paperpilot-baseline-clean-2026-09-04`

This document is the concise source of truth for what is implemented, what has
been validated, and what is still transitional. Detailed design documents remain
useful, but they must not be read as proof that every proposed migration is live.

## 1. Implemented product path

The Web product now has one default conversational entry with explicit overrides:

```text
message
-> Conversation Orchestrator
   |-> ordinary reply / one clarification
   |-> selected-Memory answer
   |-> bounded Quick Web answer
   |-> confirmed research proposal
   `-> confirmed Memory write proposal
```

Implemented behavior:

- ordinary conversation no longer creates a Research Brief;
- explicit `Memory only`, `Check online`, and `Deep research` modes bypass model
  routing;
- continuation rules prevent repeated scope questions after a concrete answer and
  reconstruct the prior topic for a command such as `快速联网查`;
- Quick Answer uses `acquire_evidence`, opens at most three readable Web sources,
  validates `S1/S2/S3` citations, and never starts the AgentGraph or writes Memory;
- a research proposal may be created without a selected Memory;
- confirmation deterministically creates and binds a managed Memory when needed;
- Memory answers, controlled note writes, imports, migration, Obsidian links, SSE,
  recovery, and the single Vault Writer remain available.

## 2. Research baseline retained

The accepted default remains the homogeneous Legacy Research AgentGraph. This
conversation work did not switch the product to Supervisor V2 or enable Red/Blue.

Current default limits:

| Setting | Value |
| --- | ---: |
| Architecture | `legacy` |
| Root / Child / Grandchild depth | 3 levels, max fork depth `2` |
| Maximum total/concurrent agents | `10 / 10` |
| Maximum total tool calls | `96` |
| Research wall time | `1200s` |
| Additional Root finalization grace | `300s` |
| Global estimated-token budget | `700,000` |
| Root final-output reserve | `50,000` |
| Child refundable lease | initial `60,000`, top-up `25,000`, max `125,000` |

The frozen online scores and diagnosis remain in
[`RESEARCH_AGENT_BASELINE_2026-09-03.md`](RESEARCH_AGENT_BASELINE_2026-09-03.md).
`budget_forced` is a structured resource stop and is not by itself a failed report.

## 3. Headless Research Core boundary

Implemented and tested:

- persistence-neutral `CoreResearchRequest` / `CoreResearchResult`;
- generic `PriorEvidenceBundle` injection into the homogeneous AgentGraph;
- product-side `Memory hit -> PriorEvidenceProjection` with opaque `prior://`
  sources and separate path bindings;
- import-boundary tests preventing Core contracts from acquiring Memory, session,
  Vault, Obsidian, or Web identities.

Not yet complete:

- the stable Web research workflow still invokes the existing recoverable
  Research Workflow rather than exclusively calling `run_core_research`;
- CLI and Rubric evaluation have not been fully migrated to the new headless
  entry point;
- product persistence has not yet been moved entirely behind a Core result
  adapter.

Therefore the accurate claim is **“the boundary and adapter exist”**, not **“all
entry points now use the Headless Core.”** Migration must remain staged behind
quality and compatibility gates.

## 4. Provider and search configuration

Provider credentials, Base URLs, and model names are project-owned configuration.
The loader now:

1. removes inherited system `*_API_KEY`, `*_BASE_URL`, and `*_MODEL` values;
2. loads the shared Git checkout `.env/.env.local` (needed by Git worktrees);
3. applies worktree-local `.env/.env.local` overrides.

The current project configuration was verified without printing secrets:

- model API: Volcengine Ark Coding API;
- model: `deepseek-v4-flash`;
- search backend: Tavily;
- real router/Brief calls: successful;
- real bounded Quick Answer: successful with cited Web sources.

## 5. Verification status

- focused Quick Answer, registry migration, late Memory binding, Runtime, and Web
  regression: `31 passed`;
- conversation routing and Web regression after continuation fixes: `30 passed`;
- project-env isolation plus router regression: `13 passed`;
- full run on the reparented zero-difference tree:
  `1142 passed, 3 skipped, 1 failed`.

The one remaining failure is the pre-existing Windows-only `60ms` Vault Writer
heartbeat timing case. It must remain visible as a flaky timing test rather than
be reported as a product-path failure or silently removed.

## 6. Known limitations and next gates

1. Save one complete real run covering papers/Web acquisition, actual fork,
   restart recovery, final report persistence, Obsidian backlinks, Memory Q&A,
   and continued research.
2. Improve Quick Answer source ranking toward primary/official sources and expose
   freshness more clearly.
3. Migrate CLI/Rubric to Headless Core first; compare quality and budget metrics
   before changing the Web research wrapper.
4. Only after those gates pass, remove compatibility identities from the existing
   full Research Workflow.
5. Keep Red/Blue disabled until an isolated evaluation shows a net benefit without
   destabilizing the accepted baseline.

## 7. Git state

- the complete May-August history remains unchanged through `main@7ecebd6`;
- `codex/supervisor-worker-v2@510ea9d` and
  `codex/homogeneous-recursive-fork@00c331b` are sibling architecture branches;
- clean baseline `4cbbb8f` and the unified-conversation commits now descend from
  the pure homogeneous branch;
- old mis-parented head `726281c` remains protected by an archive branch and
  backup tag;
- the existing pushed baseline
  `origin/codex/paperpilot-baseline-2026-09-03@195f258` is retained and will not
  be deleted during publication;
- `origin/main` was normally fast-forwarded to `7ecebd6`; the Supervisor,
  homogeneous-recursive, clean-baseline, unified-conversation, and wrong-parent
  archive branches plus both backup tags are now published without force push;
- detailed topology and old/new commit mapping are recorded in
  [`BRANCH_HISTORY.md`](BRANCH_HISTORY.md);
- the external Obsidian interview-study vault is not part of this repository or
  this commit.
