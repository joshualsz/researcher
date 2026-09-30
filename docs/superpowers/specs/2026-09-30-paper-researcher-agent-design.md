# Paper Researcher Agent — Design

## Goal
A Claude Code subagent that researches a topic across multiple academic sources and produces a report of relevant **verbatim excerpts, each with a reference** back to its source paper. The user should be able to trust and check every quoted line.

## Confirmed decisions
- **Sources:** academic APIs (arXiv, Semantic Scholar). No general web search, no local PDFs (for now).
- **Form:** Claude Code subagent (`.claude/agents/`) plus a helper script.
- **Output:** a saved Markdown report per topic.

## Approach
Subagent prompt defines workflow and citation rules; a stdlib-only Python helper does the deterministic work (search, fetch, verify) so quotes are checked, not trusted.

## Components
| File | Purpose |
|---|---|
| `.claude/agents/paper-researcher.md` | Agent definition: workflow, citation rules, report format |
| `scripts/papers.py` | CLI helper with `search`, `fetch`, `verify` commands |
| `tests/test_papers.py` | Unit tests for verify normalisation and dedupe |
| `research/<topic-slug>.md` | Generated report (output) |
| `README.md` | Short usage note |

### `scripts/papers.py`
- `search <query> [--limit N]`: queries arXiv and Semantic Scholar, dedupes by DOI / arXiv ID, prints JSON (id, title, authors, year, venue, citation count, URL, abstract).
- `fetch <id>`: downloads full text (arXiv HTML, else PDF text extraction) to a local cache dir; prints the path and whether text is full or abstract-only.
- `verify <id> "<quote>"`: checks the quote appears in the fetched text after normalising whitespace, hyphenation, ligatures and case; exits non-zero on failure and reports the location (page/section if known).

## Agent workflow
1. Expand the topic into 3-5 search queries.
2. Run `search` for each; dedupe.
3. Shortlist 5-10 papers by relevance, favouring recent and well-cited.
4. `fetch` each shortlisted paper.
5. Extract 3-6 verbatim excerpts per paper, noting page or section.
6. `verify` every excerpt; fix or drop failures.
7. Write `research/<topic-slug>.md`.

## Report format
- Short synthesis grouped by theme.
- Each point: verbatim quote followed by a reference, e.g. `[3, §4.2]`.
- Numbered bibliography: authors, year, title, URL/DOI, and source type (full text or abstract-only).

## Error handling
- No accessible full text: fall back to the abstract; label those excerpts abstract-only.
- Never invent a quote; never cite a paper that was not fetched.
- API failure: retry once, then skip and note it in the report.

## Testing
- Unit tests: `verify` normalisation (whitespace, hyphenation, ligatures), dedupe logic.
- One live end-to-end run on a sample topic, checking that every quote in the report passes `verify`.

## Out of scope
Web search, local PDF ingestion, caching layer beyond the fetch directory, database, UI, multi-agent fan-out.
