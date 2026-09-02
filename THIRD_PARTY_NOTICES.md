# Third-Party Notices

PaperPilot Research Agent V2 was designed with reference to the following
open-source projects. PaperPilot keeps its own checkpoint, budget, Evidence,
tool-availability, context-compaction, and Single Vault Writer implementations;
the upstream repositories are not runtime dependencies.

## LangChain Deep Research From Scratch

- Upstream: <https://github.com/langchain-ai/deep_research_from_scratch>
- Fixed reference commit: `93f35e5d2a51590f9542207a9ff66a01901da5bc`
- Upstream license: MIT
- Referenced files:
  - `src/deep_research_from_scratch/state_multi_agent_supervisor.py`
  - `src/deep_research_from_scratch/multi_agent_supervisor.py`
  - `src/deep_research_from_scratch/research_agent.py`
  - `src/deep_research_from_scratch/research_agent_full.py`
  - `src/deep_research_from_scratch/research_agent_scope.py`
  - `src/deep_research_from_scratch/prompts.py`
- Use in PaperPilot: architecture and graph-boundary reference. No non-trivial
  upstream source code is copied as of Phase 0.

MIT License notice:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to inclusion of the upstream copyright and
> permission notice. The Software is provided "AS IS", without warranty of any
> kind.

The complete upstream license at the fixed commit remains authoritative.

## GPT Researcher

- Upstream: <https://github.com/assafelovic/gpt-researcher>
- Fixed reference commit: `6f998577d547b1e54ec662dac63583aa11e3b84b`
- Upstream license: Apache License 2.0
- Referenced files:
  - `gpt_researcher/actions/query_processing.py`
  - `gpt_researcher/skills/curator.py`
  - `gpt_researcher/skills/writer.py`
  - `gpt_researcher/actions/web_scraping.py`
  - `multi_agents/agents/orchestrator.py`
  - `multi_agents/agents/fact_checker.py`
  - `multi_agents/agents/fact_review.py`
  - `multi_agents/memory/research.py`
- Use in PaperPilot: normalization, bounded-review, source-curation,
  scraper-routing, defensive fallback, and no-context refusal design reference.
  No non-trivial upstream source code is copied.

Licensed under the Apache License, Version 2.0 (the "License"); you may not use
material from GPT Researcher except in compliance with the License. You may
obtain a copy at <https://www.apache.org/licenses/LICENSE-2.0>. Unless required
by applicable law or agreed to in writing, software distributed under the
License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS
OF ANY KIND, either express or implied. See the License for the specific
language governing permissions and limitations.

## Docling

- Upstream: <https://github.com/docling-project/docling>
- Runtime status: optional local Python dependency (`documents` extra)
- Upstream license: MIT
- Use in PaperPilot: layout- and table-aware extraction for complex text PDFs.
  OCR, remote services, external plugins, image generation, and VLM enrichment
  are disabled. Source page provenance is retained through `prov.page_no`.

## markdownify

- Upstream: <https://github.com/matthewwithanm/python-markdownify>
- Runtime status: core Python dependency
- Upstream license: MIT
- Use in PaperPilot: convert locally cleaned static HTML to Markdown while
  retaining headings, emphasis, lists, tables, and links.
