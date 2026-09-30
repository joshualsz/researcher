# Paper Researcher

A Claude Code subagent that researches a topic on arXiv and Semantic Scholar. It writes a report of verbatim excerpts, each with a reference. Every quote is checked against the paper's text before it goes into the report.

## Use

In Claude Code, from this folder (restart the session or run `/agents` after cloning so the agent loads):

> Use the paper-researcher agent to research "retrieval augmented generation for code".

The report is saved to `research/<topic-slug>.md`.

## How it works

- `.claude/agents/paper-researcher.md`: the agent's workflow and citation rules.
- `scripts/papers.py`: the helper it calls (stdlib-only Python).
  - `search "<query>" [--limit N]` searches arXiv and Semantic Scholar and dedupes the results.
  - `fetch <id>` downloads a paper's full text (arXiv HTML, else PDF via `pdftotext`) or falls back to the abstract.
  - `verify <id> "<quote>"` checks that a quote appears in the fetched text.

`pdftotext` (poppler, `brew install poppler`) is optional. Without it, papers that lack an arXiv HTML version fall back to abstract-only, and the report labels them.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
