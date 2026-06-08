# Morning AI research agent

**Vision:** A junior-analyst-style research agent that scans mixed sources, filters noise, summarizes what matters, highlights trends, and drops everything into one structured digest — ~10 minutes to read instead of checking 20 tabs.

**Today:** Gmail newsletters → score → summarize → synthesize → Streamlit.  
**Next:** More sources, smarter ranking, public deploy via GitHub Pages, optional second agent for counterpoints.

---

## Ultimate goal

One useful morning report built from **many sources**, not one inbox:

| Source (planned) | Status |
|------------------|--------|
| Newsletters (Gmail) | ✅ `fetch_gmail.py` |
| GitHub trending repos | Planned |
| arXiv (cs.AI / cs.LG) | Planned |
| Hacker News | Planned |
| AI subreddits | Planned |
| X/Twitter lists, company blogs | Later |

The agent should **rank by relevance and novelty**, summarize (not just forward links), and merge overlapping stories across sources. Longer term: a **second agent** (`challenge_digest`) that pushes back on conclusions and surfaces opposing viewpoints.

---

## Architecture (current)

```text
fetch_gmail → items.jsonl                    (bootstrap — Python, before loop)

before_agent.py (hardcoded loop) → score_signal → summarize_item (high-signal only) 
                                 → synthesize_digest → digest.jsonl
agent.py ReAct loop:
  orchestrator (Ollama qwen2.5:3b) → pick tool → run_tool → observation → repeat until finish
  progress rebuilt from JSONL each turn (no items_as_blurbs step)

run_tool dispatches to tools.py:
  score_signal      → Ollama — high/low signal → signals.jsonl
  summarize_item    → Groq (llama-3.3-70b) — summary + topics → summaries.jsonl
  synthesize_digest → Groq — merged report → digest.jsonl
  finish            → blocked unless digest.jsonl has a row (or no high-signal items)

app.py → load_jsonl(DIGEST_FILE) → Streamlit UI (local)

launchd (8:00 AM) → scripts/daily_agent.sh → agent.py
```

**Two layers of LLM reasoning**

| Layer | Where | Model | Job |
|-------|-------|-------|-----|
| **Orchestrator** | `react_loop` in `agent.py` | Ollama `qwen2.5:3b` | Workflow — which tool, which `item_id`, when to synthesize/finish |
| **Specialists** | `tools.py` | Ollama (score) + Groq (summarize/synthesize) | Judgment — noise filter, per-item summary, digest synthesis |

Python (`run_tool`) runs tools, enforces phase guards, and returns **observations** to the orchestrator. `format_progress_state()` injects counts, unscored ids, and suggested next action each turn.

**Backup:** `before_agent.py` — hardcoded `for` loop (no ReAct), useful for debugging.

---

## Data files

| File | Contents |
|------|----------|
| `items.jsonl` | Raw sources (`id`, `source`, `subject`, `sender`, `date`, `url`, `body`) |
| `signals.jsonl` | Per-item noise filter (`id`, `sender`, `high_signal`, `reason`, `trend_hint`) |
| `summaries.jsonl` | Per-item analyst output (`id`, `sender`, `summary`, `topics`) |
| `digest.jsonl` | Morning report (`title`, `report`, `themes`, `source_count`) — last line = current |

Shared helper: `load_jsonl()` in `tools.py` (used by `agent.py`, `app.py`, `synthesize_digest`).

New fetchers should use the same item shape and distinct `item_id` prefixes (`gmail_…`, `hn_…`, `arxiv_…`).

---

## `agent.py` — ReAct pieces

| Function | Role |
|----------|------|
| `progress_sets()` | Rebuild scored / high-signal / unscored / needs-summary from JSONL |
| `format_progress_state()` | Human-readable snapshot + suggested next action for orchestrator |
| `resolve_item_id()` | Explicit `item_id` from LLM, or auto-pick first allowed id |
| `run_tool(action, tool_args)` | Dispatch + phase guards → `tools.py` |
| `react_loop()` | Thought → action → observation loop (`MAX_STEPS = 40`) |
| `main()` | `clear_daily_files()` → `fetch_gmail()` → `react_loop()` |

**Orchestrator actions:** `score_signal`, `summarize_item`, `synthesize_digest`, `finish`

**`tool_args`:** `{}` auto-picks next id from progress; or `{"item_id": "gmail_..."}`.

**Workflow rules (prompt + Python guards):**

- Score **all** items before summarizing
- Summarize every **high-signal** item only
- `synthesize_digest` only after all high-signal items summarized
- `finish` only when `digest.jsonl` has a row (or inbox had no high-signal items)

---

## `tools.py` — specialists

| Function | Role |
|----------|------|
| `groq_chat()` | Groq API for summarize + synthesize (`GROQ_API_KEY` in `.env`) |
| `score_signal()` | Ollama JSON filter → append `signals.jsonl` |
| `summarize_item()` | Groq summary + topics → append `summaries.jsonl` |
| `synthesize_digest()` | Groq merged digest → append `digest.jsonl` |
| `clean_text()` / `text_rejection_reason()` | Strip template junk; reject placeholder summaries |

Prompts use **required keys + concrete JSON example** (not `<...>` placeholders).

---

## Phase status

### Done

- [x] `fetch_gmail` → `items.jsonl`
- [x] `score_signal`, `summarize_item`, `synthesize_digest` in `tools.py`
- [x] Groq for summarize/synthesize; Ollama for score + orchestrator
- [x] ReAct loop with progress block (no `items_as_blurbs`)
- [x] Phase guards + `finish` digest check
- [x] `app.py` — latest digest, title, report, themes
- [x] Daily schedule — `scripts/daily_agent.sh` + launchd plist
- [x] End-to-end run verified (9 items → 1 high-signal → digest → Streamlit)

### Next — more sources

- [ ] `fetch_hn.py`, `fetch_arxiv.py`, `fetch_github.py`, `fetch_reddit.py` → same `items.jsonl` shape
- [ ] Source weighting + novelty (seen topics across days)
- [ ] Merge duplicate stories across sources in synthesize step

### Next — second agent

- [ ] `challenge_digest` — counterarguments, missing angles, bias check
- [ ] Optional section in UI or separate `challenges.jsonl`

### Deferred — UI

- [ ] Per-source breakdown in digest (which sources contributed)
- [ ] Richer Streamlit sidebar (per-item summaries)
- [ ] Source list + open in Gmail (API id ≠ web link — awkward)

---

## Deployment & sharing (planned)

**Split producer and consumer:**

```text
[Private]  Mac cron or GitHub Actions     [Public]  GitHub Pages
           agent.py + secrets                  static site reads digest.json
           Gmail, Groq, Ollama (local)         no keys, no inbox
                    │
                    └── export digest.json → commit to repo (e.g. docs/)
```

| Piece | Where it runs | Public? |
|-------|---------------|---------|
| Agent (`agent.py`) | Mac (launchd) or GitHub Actions | No — needs secrets |
| Ollama scoring | Local Mac only today | Not on Vercel/Pages |
| Groq | API key in `.env` / GitHub Secrets | Key never exposed to readers |
| Digest UI | GitHub Pages or Streamlit Cloud | Yes — anyone with link (if repo/site is public) |

**GitHub Pages flow (target):**

1. Daily job produces digest; export last row to `docs/digest.json` (or similar).
2. Commit + push to repo.
3. GitHub Pages serves a small static page that fetches `digest.json` and renders title, report, themes.
4. Share URL — readers see the **finished report only**, not raw emails or API keys.

**Notes:**

- Public repo → digest JSON is public. Use private repo + access controls only if you need restricted readers.
- Before sharing widely, review digest content (summaries of *your* newsletters).
- Cloud agent likely needs **Groq-only** (drop Ollama) if moved off Mac.
- Streamlit Community Cloud is an alternative reader; still needs digest hosted at a URL or in repo.

---

## Run (local)

```bash
# Produce digest (manual)
python agent.py

# View digest
streamlit run app.py

# Test scheduled script
"/Users/erinlee/Agentic AI/scripts/daily_agent.sh"
tail "/Users/erinlee/Agentic AI/logs/agent.log"
```

**Step budget:** ~`inbox_items + high_signal_items + 2` minimum; `MAX_STEPS = 40` allows orchestrator retries.

---

## Change launchd time

Edit `Hour` / `Minute` in `scripts/com.erinlee.research-agent.plist`, then:

```bash
launchctl unload ~/Library/LaunchAgents/com.erinlee.research-agent.plist
cp "/Users/erinlee/Agentic AI/scripts/com.erinlee.research-agent.plist" ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.erinlee.research-agent.plist
```
