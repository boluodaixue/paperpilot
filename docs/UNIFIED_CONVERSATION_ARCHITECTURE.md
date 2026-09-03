# Unified Conversation Architecture

Status: First product slice implemented on top of frozen baseline `195f258`.
See [`CURRENT_STATUS.md`](CURRENT_STATUS.md) for the authoritative implementation
and verification snapshot.

Implemented:

- persistence-neutral `CoreResearchRequest` / `CoreResearchResult` contracts;
- generic prior-Evidence injection into the homogeneous AgentGraph;
- side-effect-free Conversation Orchestrator with explicit action overrides;
- `/api/conversation/route`, which cannot create research tasks or write Memory;
- Web default mode changed to automatic routing while preserving explicit
  Memory-only and deep-research overrides;
- deterministic continuation handling for answered scope questions and
  prior-topic reconstruction when the user selects quick Web search;
- bounded Quick Answer Service using at most three readable sources and validated
  source handles, without Research Core or Memory writes;
- product-side Memory hit to opaque `PriorEvidenceBundle` projection;
- research proposal creation without Memory and managed-Memory creation/binding
  at confirmation time;
- nullable research-locator migration plus atomic session/Registry binding;
- project-only provider configuration with shared-checkout `.env` discovery for
  Git worktrees;
- dependency-boundary and compatibility tests.

Still pending: direct Rubric/CLI migration to the Headless Core entry, Web Wrapper
migration after those quality gates pass, and removal of compatibility-layer
product identities from the existing full Research Workflow.

## 1. Decision

PaperPilot exposes one conversational product entry while preserving a headless,
independently callable Research Core.

The product-facing Conversation Orchestrator decides **what the user wants**.
The Research Root decides **how an accepted research task should be executed**.
They are different responsibilities and never share workflow state.

```text
Web UI
  |
  v
Conversation Orchestrator
  |-- reply / clarify
  |-- Memory Answer Service
  |-- Quick Answer Service
  |-- Research Proposal Service --> Research Core
  `-- Memory Write Proposal ------> Memory Writer

CLI research --------------------------------------> Research Core
Rubric evaluation ---------------------------------> Research Core
```

## 2. Non-negotiable boundaries

1. Research Core must not import Web, conversation, Memory Wiki, Obsidian, or
   product-session modules.
2. Rubric evaluation and the core CLI call Research Core directly. They do not
   pass through conversational routing, Memory retrieval, or Wiki persistence.
3. Research Core never receives a `memory_id`, session ID, Obsidian URI, Vault
   path, or chat transcript.
4. Product Memory may contribute prior knowledge only after conversion into a
   generic immutable `PriorEvidenceBundle`.
5. Conversation routing cannot start deep research or commit a Memory write.
   It may only return a proposal requiring the relevant confirmation gate.
6. Search, LLM access, file reading, retrieval, and persistence are shared
   capability interfaces. They are not private methods of Research Core.
7. Dependency direction points inward: adapters depend on application services;
   application services depend on contracts; Research Core depends only on core
   contracts and injected capability interfaces.

## 3. Modules

### 3.1 Research Core

Owns:

- research planning and requirement decomposition;
- Root / Child / Grandchild execution;
- research tools and Evidence acquisition;
- budget leases, checkpoints, Blackboard coordination, retries and limits;
- research sufficiency assessment;
- cited final report synthesis.

Does not own:

- conversational alignment or intent routing;
- selected-Memory lookup;
- Memory question answering;
- Wiki or Obsidian persistence;
- Web/API/CLI presentation;
- Rubric judging.

### 3.2 Conversation Orchestrator

An application-layer service using a small structured model call. It may use the
same model backend as Research Root, but never the same Agent instance, prompt,
checkpoint, token budget, or state.

It returns exactly one action:

```text
reply
clarify
memory_answer
quick_search
propose_research
propose_memory_write
```

Explicit UI actions or CLI commands bypass model routing. Ambiguous requests return
`clarify`; the router never directly performs a side effect.

### 3.3 Answer Service

Owns bounded conversational answers:

- `reply`: ordinary conversation without retrieval;
- `memory_answer`: selected-Memory retrieval followed by cited synthesis;
- `quick_search`: a small fixed number of Web sources and one answer synthesis.

It cannot create Agent trees. When a question requires comparison, conflict
resolution, multi-direction coverage, or a durable report, it returns a Research
Proposal rather than imitating deep research.

### 3.4 Research Proposal Service

Owns product-only preparation around Research Core:

1. convert selected-Memory hits into `PriorEvidenceBundle`;
2. produce a concise editable Research Brief;
3. obtain explicit confirmation, including intended persistence behavior;
4. construct `CoreResearchRequest` and call Research Core;
5. convert `CoreResearchResult` into an optional Memory write transaction.

### 3.5 Memory Services

- Memory Retriever is read-only and returns generic search hits.
- Memory Writer accepts only validated write plans.
- Chat, Memory answers, and quick search may propose a note write.
- Deep research may persist after the Research Brief explicitly authorizes it.
- Obsidian is only a view/edit adapter over the durable Memory files.

## 4. Contracts

### 4.1 Conversation request

```text
ConversationRequest
  message: string
  recent_messages: ConversationMessage[]
  selected_memory: MemorySelection | null
  explicit_action: ActionOverride | null
```

Only the application layer sees `selected_memory`. It is never forwarded to
Research Core.

### 4.2 Route decision

```text
RouteDecision
  action: ConversationAction
  confidence: number
  response: string | null
  query: string | null
  reason_code: string
  requires_memory: boolean
  requires_confirmation: boolean
```

The router cannot return tool calls, filesystem paths, budget changes, or a
completed write.

### 4.3 Prior evidence

```text
PriorEvidenceBundle
  items: PriorEvidence[]

PriorEvidence
  evidence_id: string
  finding: string
  source_ref: string
  title: string
  provenance: string
```

`provenance` may record `memory`, `file`, or another product source for audit,
but the bundle contains no Memory implementation identity.

### 4.4 Core research request

```text
CoreResearchRequest
  objective: string
  scope: string[]
  directions: string[]
  constraints: string[]
  expected_output: string
  prior_evidence: PriorEvidenceBundle
  require_evidence: boolean
  run_id: string
```

### 4.5 Core research result

```text
CoreResearchResult
  run_id: string
  report_markdown: string
  evidence: EvidenceItem[]
  status: ResearchStatus
  termination_reason: TerminationReason | null
  output_status: OutputStatus
  unresolved: string[]
  thread_count: number
  tool_calls_used: number
  estimated_tokens_used: number
```

The result contains no persistence manifest. Web, CLI, and evaluation decide how
to render, save, or judge it.

### 4.6 Memory write proposal

```text
MemoryWriteProposal
  target_memory_id: string
  complete_markdown_preview: string
  source_paths: string[]
  reason: string
  confirmation_required: true
```

Only Memory Writer may turn a confirmed proposal into durable files.

## 5. Routing policy

| User request | Default action | Memory required | Confirmation |
| --- | --- | ---: | ---: |
| Greeting, product help, ordinary chat | `reply` | No | No |
| “What is in this Memory?” | `memory_answer` | Yes | No |
| Narrow current fact or focused topic asking to check online | `quick_search` | No | No |
| Comparison, investigation, multi-source report | `propose_research` | Optional | Yes |
| Ambiguous continuation | `clarify` | No | No |
| “Save this answer” | `propose_memory_write` | Yes | Yes |

If `memory_answer` has insufficient evidence, the answer presents an upgrade
action. It does not silently search the Web or start Research Core.

## 6. Product state machine

```text
idle
  -> routing
      -> replying -> idle
      -> clarifying -> routing
      -> retrieving_memory -> answering -> optional_write_proposal -> idle
      -> quick_searching -> answering -> optional_write_proposal -> idle
      -> research_proposal -> waiting_research_confirmation
          -> cancelled -> idle
          -> confirmed -> researching -> optional_report_write -> idle
```

One session may bind to one Memory, but casual conversation, Quick Answer, and
Research Brief review are allowed before a Memory is selected. Memory-dependent
answers ask for selection only after intent has been established. A confirmed
research proposal creates and binds a managed Memory when the session is unbound.

## 7. User interface

- Do not require “Research / Memory Answer” as mandatory top-level modes.
- Default composer behavior is automatic routing.
- Keep explicit composer actions: `Only search this Memory`, `Check online`, and
  `Deep research`.
- Every answer shows provenance: `Conversation`, `From Memory`, `Quick Web`, or
  `Research proposal`.
- Research always shows a concise Brief before execution. Advanced fields remain
  expandable rather than occupying the default conversation view.
- Memory IDs are implementation details; display the Memory title.

## 8. Independent entry points

### Rubric

```text
Benchmark case -> CoreResearchRequest -> Research Core -> CoreResearchResult -> Judge
```

No Memory database, Wiki writer, Obsidian adapter, conversation router, or product
workflow is initialized.

### CLI

```text
paperpilot research <query> -> Research Core
paperpilot chat             -> Conversation application
paperpilot memory ask       -> Memory Answer Service
```

The core command is the default stable automation surface. Product capabilities
are explicit subcommands.

### Web

```text
message -> Conversation Orchestrator -> selected application service
```

Only a confirmed Research Proposal invokes Research Core.

## 9. Migration sequence

1. [x] Add contracts and dependency-boundary tests without changing runtime behavior.
2. [x] Add the headless Research Core entry point and generic prior Evidence injection.
3. [x] Add Conversation Orchestrator and retain existing endpoints as compatibility adapters.
4. [x] Add bounded Answer Service profiles for Memory and quick Web answers.
5. [x] Make automatic routing the default while retaining explicit overrides.
6. [ ] Move Rubric/CLI to the Headless Core and compare quality/budget results.
7. [ ] Move Web persistence after `CoreResearchResult`; remove Memory identities
   from the core request path.
8. [ ] Delete compatibility routing only after product, CLI, and Rubric regression
   gates pass independently.

## 10. Acceptance gates

- Import-boundary test proves Research Core cannot import product Memory, Web,
  Obsidian, or conversation modules.
- Rubric fixture runs with no Memory/Vault initialization.
- Core CLI produces a report with Memory and Web server disabled.
- Greetings never create a research checkpoint.
- Memory questions never start Research Core.
- Deep research never starts before Brief confirmation.
- No unconfirmed conversation path writes Memory.
- A selected Memory can contribute prior Evidence without exposing its ID or
  paths to Research Core.
- Existing ResearchBench quality configuration and Agent budgets remain unchanged.
