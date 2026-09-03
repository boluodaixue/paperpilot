# PaperPilot Branch History

Last updated: 2026-09-04

This document records the active Git topology after the September branch-history
cleanup. The cleanup changed only the parentage and hashes of the September
product commits. The complete May-to-August main history and the final product
file tree were preserved.

## Active topology

```text
2026-05-10  30533d7  Initial DeepResearch Agent
    |
    |  Complete May initialization history
    |  Complete August 23-27 Evidence / Web / dynamic-fork history
    |  Complete August 28 N0-N6 / Memory W0-W6 history
    |  Complete August 29 Writer / retrieval / evaluation history
    v
2026-08-30  7ecebd6  main
    |
    +-- 2026-09-01  510ea9d  codex/supervisor-worker-v2
    |
    `-- 2026-09-03  17c382a
            `-- 00c331b  codex/homogeneous-recursive-fork
                  `-- 4cbbb8f  codex/paperpilot-baseline-clean-2026-09-04
                        `-- 74fcfad  reparented unified-conversation product head
                              `-- current documentation commit
                                  codex/unified-conversation-orchestrator
```

`main@7ecebd6` is a tip after 58 existing commits, not a new root. Every May and
August commit remains an unchanged ancestor of both active architecture lines.

## Branch responsibilities

| Ref | Purpose | Active development |
| --- | --- | ---: |
| `main@7ecebd6` | Common May-August product history and clean branch point | No |
| `codex/supervisor-worker-v2@510ea9d` | Pure Supervisor-Worker experiment | No |
| `codex/homogeneous-recursive-fork@00c331b` | Pure homogeneous recursive-fork architecture baseline | No |
| `codex/paperpilot-baseline-clean-2026-09-04@4cbbb8f` | Frozen research + Memory product baseline on the correct parent | No |
| `codex/unified-conversation-orchestrator` | Latest product state | Yes |

## Reparented commit mapping

Author and committer timestamps were preserved with
`--committer-date-is-author-date`.

| Original mis-parented commit | Clean equivalent | Original timestamp | Subject |
| --- | --- | --- | --- |
| `195f258` | `4cbbb8f` | 2026-09-03 17:13 +08:00 | freeze research baseline and Memory product layer |
| `b9602ca` | `9d8c7c8` | 2026-09-03 17:16 +08:00 | define unified conversation architecture |
| `bebbc39` | `9377eb9` | 2026-09-03 18:08 +08:00 | add headless core and unified conversation routing |
| `c35f909` | `8582ae4` | 2026-09-03 18:51 +08:00 | render user messages before routing |
| `5e48a5e` | `495ac69` | 2026-09-03 18:57 +08:00 | clarify incomplete research requests |
| `7a1b980` | `f206362` | 2026-09-03 20:20 +08:00 | complete minimal conversation services |
| `44b0d26` | `772b94c` | 2026-09-03 20:30 +08:00 | resolve conversational research follow-ups |
| `cc3f157` | `2f82d94` | 2026-09-03 20:33 +08:00 | deterministic clarification continuations |
| `e3e48c9` | `f1e40e8` | 2026-09-03 20:38 +08:00 | isolate provider config to project env |
| `726281c` | `74fcfad` | 2026-09-03 21:01 +08:00 | record unified-conversation status |

Hashes changed because the parent chain changed. Commit contents and timestamps
were retained.

## Preservation proof

The old and reparented product heads produced no file-tree difference:

```text
git diff --exit-code \
  archive/unified-on-supervisor-2026-09-04 \
  74fcfad

exit code: 0
```

Verified ancestor checks also returned exit code `0`:

```text
30533d7 -> 74fcfad
7ecebd6 -> 74fcfad
00c331b -> 74fcfad
```

The `main` ref remained exactly
`7ecebd6f0cc6bb6af717470061c29f63d6263597` throughout the local rewrite.

## Archived wrong-parent history

The old product history remains recoverable through:

- branch `archive/unified-on-supervisor-2026-09-04@726281c`;
- tag `backup/unified-before-cleanup-2026-09-04@726281c`;
- tag `backup/main-before-cleanup-2026-09-04@7ecebd6`;
- existing branch `codex/paperpilot-baseline-2026-09-03@195f258`.

No existing remote branch is deleted by this cleanup. The old September baseline
may be marked superseded later, but deletion requires a separate explicit decision.

## Verification

Full test run on the zero-difference clean tree:

```text
1142 passed, 3 skipped, 1 failed
```

The only failure was the already tracked Windows-only `60ms` Vault Writer
heartbeat timing case. There were no new failures.

## Remote publication result

Published on 2026-09-04 without force push:

- `origin/main`: normal fast-forward `22dcba5 -> 7ecebd6`;
- `origin/codex/supervisor-worker-v2@510ea9d`;
- `origin/codex/homogeneous-recursive-fork@00c331b`;
- `origin/codex/paperpilot-baseline-clean-2026-09-04@4cbbb8f`;
- `origin/codex/unified-conversation-orchestrator`;
- `origin/archive/unified-on-supervisor-2026-09-04@726281c`;
- backup tags for old `main@7ecebd6` and old unified head `726281c`.

The existing `origin/codex/paperpilot-baseline-2026-09-03@195f258`, portfolio,
and full-history archive branches were retained. No remote ref was deleted.
