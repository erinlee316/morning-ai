"""Shared JSONL I/O, Groq calls, and desk pipeline tools (score, summarize, synthesize)."""

import os
import time
import json
from datetime import datetime, timezone
from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError, APIError, BadRequestError
from dotenv import load_dotenv
from prompts import load_prompt
from content_filters import (
    clean_text,
    clean_tags,
    marketing_filter_reason,
    report_reason,
    invalid_text_reason,
    SUMMARY_PLACEHOLDER_PHRASES,
    tags_reason,
)

load_dotenv()

# Pipeline: JSONL I/O -> Groq calls -> score / summarize / synthesize

# --- Config ---

GROQ_MODEL = "llama-3.3-70b-versatile"

ITEMS_FILE = "items.jsonl"
SUMMARIES_FILE = "summaries.jsonl"
SIGNALS_FILE = "signals.jsonl"
REPORT_FILE = "report.jsonl"

# Two Groq accounts, one 70b daily pool each. The orchestrator makes a call every ReAct step
# (the highest call volume of any role), so it runs on its own account (KEY5) to keep that
# volume off the desk pool that scoring/summaries/synthesis draw from (KEY4). groq_chat still
# rides out each account's per-minute (TPM) limit with backoff + pacing below. Stage aliases
# kept so existing callers/imports keep working.
GROQ_API_KEY = "GROQ_API_KEY4"
GROQ_KEY_ORCHESTRATOR = "GROQ_API_KEY5"
GROQ_KEY_HN = GROQ_KEY_ARXIV = GROQ_KEY_GITHUB = GROQ_KEY_DESK = GROQ_API_KEY

# Retry / pacing for the two-account strategy.
GROQ_MAX_RETRIES = 6       # attempts per call before giving up
GROQ_BACKOFF_BASE = 2.0    # seconds; doubles each retry (2, 4, 8, ... capped)
GROQ_BACKOFF_CAP = 45.0    # max seconds to wait on any single retry
GROQ_PACING_DELAY = 4.0    # seconds slept after each successful call. Lowered from 12s now that load
                           # is split across two accounts (more TPM headroom each). Still proactively
                           # spaces calls; backoff above catches any 429 overflow. Watch the logs and
                           # bump back up if "Groq rate limit" lines start showing up often.

ANALYST_PROMPT = load_prompt("analyst.txt")
REVIEWER_PROMPT = load_prompt("reviewer.txt")
SCORE_SIGNAL_PROMPT = load_prompt("score_signal_system.txt")
SYNTHESIZE_REPORT_PROMPT = load_prompt("synthesize_report.txt")


# --- JSONL Input/Output ---
# Read/write items, signals, summaries, and report rows on disk.

def load_jsonl(path):
    """Load a JSONL file into a list of dicts -> return [] if the file is missing."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]
    except FileNotFoundError:
        return []


def write_items(items, path=ITEMS_FILE):
    """Overwrite file with fetched JSON-encoded item per line (default: items.jsonl)."""
    with open(path, "w", encoding="utf-8") as file:
        file.write("".join(json.dumps(item) + "\n" for item in items))
    print(f"Wrote {len(items)} items to {path}")


def items_by_item_id():
    """Read items.jsonl -> dict {item_id: item info} for fast lookup."""
    items = {}
    for item in load_jsonl(ITEMS_FILE):
        item_id = str(item.get("item_id") or "")
        if item_id:
            items[item_id] = {**item, "item_id": item_id}
    return items


def write_signal_row(item_id, author, high_signal, reason):
    """Append one score row to signals.jsonl -> return that row."""
    signal_row = {
        "item_id": str(item_id or ""),
        "author": author,
        "high_signal": high_signal,
        "reason": reason.strip(),
    }
    with open(SIGNALS_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(signal_row) + "\n")
    return signal_row


def latest_signal_row(item_id):
    """Return the most recent signals.jsonl row for item_id, if any."""
    item_id = str(item_id or "")
    row = None
    for signal in load_jsonl(SIGNALS_FILE):
        if str(signal.get("item_id") or "") == item_id:
            row = signal
    return row



# --- Groq ---
# groq_chat: JSON-mode completions. parse_llm_json: strip fences. pick_item_ids: fetch-time pick.

def _retry_after_seconds(err):
    """Pull Groq's Retry-After hint (in seconds) from a 429 response, if present."""
    try:
        value = err.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


def groq_chat(messages, api_key_env=GROQ_API_KEY, model=GROQ_MODEL):
    """Call Groq chat completions in JSON mode -> return the assistant message content string in JSON.

    Per-account strategy: on a 429 we wait and retry (honoring Retry-After when Groq sends it,
    else exponential backoff), and we pace successful calls so a burst of summarize calls
    doesn't blow the per-minute token limit. Trades run time for staying under quota.
    """
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"{api_key_env} not set (add to .env)")

    client = OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=api_key,
    )

    delay = GROQ_BACKOFF_BASE
    for attempt in range(1, GROQ_MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
        except RateLimitError as err:
            if attempt == GROQ_MAX_RETRIES:
                raise
            wait = min(_retry_after_seconds(err) or delay, GROQ_BACKOFF_CAP)
            print(f"  Groq rate limit (attempt {attempt}/{GROQ_MAX_RETRIES}) — waiting {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
            continue
        except (APITimeoutError, APIConnectionError) as err:
            # Transient network blip (TLS handshake/connect timeout) — retry with backoff so a
            # single hiccup doesn't kill an unattended daily run. No Retry-After on these.
            if attempt == GROQ_MAX_RETRIES:
                raise
            wait = min(delay, GROQ_BACKOFF_CAP)
            print(f"  Groq connection error: {type(err).__name__} (attempt {attempt}/{GROQ_MAX_RETRIES}) — waiting {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
            continue
        except BadRequestError as err:
            # Groq's JSON mode can reject the model's OWN output as invalid (code
            # 'json_validate_failed') — a transient generation failure (common on the 8b) that
            # usually clears on a re-sample. Retry those; re-raise any other 400 (a genuinely
            # malformed request, which retrying won't fix).
            if getattr(err, "code", None) != "json_validate_failed" and "json_validate_failed" not in str(err):
                raise
            if attempt == GROQ_MAX_RETRIES:
                raise
            wait = min(delay, GROQ_BACKOFF_CAP)
            print(f"  Groq JSON-validation failure (attempt {attempt}/{GROQ_MAX_RETRIES}) — re-sampling in {wait:.0f}s")
            time.sleep(wait)
            delay *= 2
            continue
        time.sleep(GROQ_PACING_DELAY)  # pace successful calls to stay under tokens-per-minute
        return response.choices[0].message.content


def parse_llm_json(text):
    """Parse JSON from an LLM reply, stripping ```json code fences when present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def pick_item_ids(system_prompt, pick_options, api_key_env):
    """Fetch-time pick: Groq picks which item_ids to keep, before they're written to items.jsonl.

    The fetcher has already narrowed the items to choose from — at most MAX_PICK_OPTIONS of
    them, each with its body shortened to a MAX_GROQ_BODY_CHARS preview — so this just asks
    Groq to choose up to max_pick. pick_options is the dict sent to the model, e.g.
    {"max_pick": 4, "stories": [...]}. Returns selected_ids as strings.
    """
    llm_response = groq_chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(pick_options)},
    ], api_key_env=api_key_env)
    parsed = parse_llm_json(llm_response)
    selected_ids = parsed.get("selected_ids") or []
    return [str(item_id) for item_id in selected_ids]



# --- Pipeline tools ---
# score_signal, summarize_item, synthesize_report — called by the orchestrator in agent.py.

def score_signal(item_id, author, subject, body, source="hackernews", url=""):
    """Score one item with Groq, append to signals.jsonl -> returns (status_message, signal_row)."""

    drop_reason = marketing_filter_reason(subject, body, url, source)
    if drop_reason:
        signal_row = write_signal_row(item_id, author, False, f"Auto-filter: {drop_reason}")
        return f"Scored {author}", signal_row

    item_json = json.dumps({
        "item_id": item_id,
        "author": (author or "").strip(),
        "subject": subject,
        "body": body[:6000],
        "source": source
    })

    try:
        llm_response = groq_chat([
            {"role": "system", "content": SCORE_SIGNAL_PROMPT},
            {"role": "user", "content": item_json},
        ], api_key_env=GROQ_KEY_DESK, model=GROQ_MODEL)
        parsed = parse_llm_json(llm_response)
        high_signal = parsed.get("high_signal")
        reason = parsed.get("reason")

        if isinstance(high_signal, str):
            high_signal = {"true": True, "false": False}.get(high_signal.lower())
        if not isinstance(high_signal, bool):
            high_signal, reason = False, "Score failed: model did not return high_signal boolean"
        elif not isinstance(reason, str) or not reason.strip():
            high_signal, reason = False, "Score failed: model did not return a valid reason"

    except (json.JSONDecodeError, RuntimeError, APIError) as err:
        # APIError covers a transient failure that survived groq_chat's retries — degrade this
        # one item to a failed score rather than crashing the whole run on a network outage.
        high_signal, reason = False, f"Score failed: {err}"

    signal_row = write_signal_row(item_id, author, high_signal, reason)
    return f"Scored {author}", signal_row


def summarize_item(item_id, author, subject, body, source="hackernews", url=""):
    """Run analyst + reviewer Groq calls on one item -> append to summaries.jsonl or return a skip reason."""

    signal_row = latest_signal_row(item_id)
    if not signal_row or signal_row.get("high_signal") is not True:
        scored = signal_row.get("high_signal") if signal_row else "unscored"
        return f"Skipped {author}: high_signal required (got {scored})"

    item_json = json.dumps({
        "item_id": item_id,
        "author": (author or "").strip(),
        "subject": subject,
        "body": body[:8000],
        "source": source
    })

    # Analyst drafts first; the reviewer then critiques that draft (not the raw body),
    # so the second call ships a few hundred tokens instead of the full article again.
    try:
        analyst_response = groq_chat([
            {"role": "system", "content": ANALYST_PROMPT},
            {"role": "user", "content": item_json},
        ], api_key_env=GROQ_KEY_DESK)
    except Exception as err:
        return f"Skipped {author}: analyst Groq error ({err})"

    try:
        analyst_parsed = parse_llm_json(analyst_response)
    except json.JSONDecodeError:
        return f"Skipped {author}: invalid JSON (analyst)"

    analyst_draft = json.dumps({
        "item_id": item_id,
        "subject": subject,
        "source": source,  # lets the reviewer tailor its caveat to the genre (paper vs news vs Ask HN)
        "summary": analyst_parsed.get("summary") or "",
        "technical_breakthrough": analyst_parsed.get("technical_breakthrough") or "",
    })

    try:
        reviewer_response = groq_chat([
            {"role": "system", "content": REVIEWER_PROMPT},
            {"role": "user", "content": analyst_draft},
        ], api_key_env=GROQ_KEY_DESK, model=GROQ_MODEL)
    except Exception as err:
        return f"Skipped {author}: reviewer Groq error ({err})"

    try:
        reviewer_parsed = parse_llm_json(reviewer_response)
    except json.JSONDecodeError:
        return f"Skipped {author}: invalid JSON (reviewer)"

    summary_text_fields = {}
    for field_name, parsed, key, min_chars in (
        ("summary", analyst_parsed, "summary", 80),
        ("technical_breakthrough", analyst_parsed, "technical_breakthrough", 80),
        ("limitations_or_critiques", reviewer_parsed, "limitations_or_critiques", 40),
    ):
        field_text = clean_text(parsed.get(key) or "")
        drop_reason = invalid_text_reason(
            field_text,
            field=field_name,
            placeholder_phrases=SUMMARY_PLACEHOLDER_PHRASES,
            min_chars=min_chars,
        )
        if drop_reason:
            return f"Skipped {author}: {drop_reason}"
        summary_text_fields[field_name] = field_text

    topics = clean_tags(analyst_parsed.get("topics"))
    drop_reason = tags_reason(topics, label="topics")
    if drop_reason:
        return f"Skipped {author}: {drop_reason}"

    summary_row = {
        "item_id": str(item_id or ""),
        "author": author,
        "subject": subject,
        "url": url or "",
        **summary_text_fields,
        "topics": topics,
    }
    with open(SUMMARIES_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(summary_row) + "\n")

    return f"Summarized {author}"


def synthesize_report():
    """Merge summaries with Groq into one markdown report -> append to report.jsonl or return a skip reason."""
    items = items_by_item_id()

    summary_rows = []
    for summary_row in load_jsonl(SUMMARIES_FILE):
        item_id = str(summary_row.get("item_id") or "")
        item = items.get(item_id, {})
        summary_rows.append({
            **summary_row, # contains summary, technical breakthrough, limitations_or_critiques, topics
            "item_id": item_id,
            "url": summary_row.get("url") or item.get("url", ""),
            "subject": summary_row.get("subject") or item.get("subject", ""),
        })

    if not summary_rows:
        return "No summaries to synthesize"

    synthesize_payload = json.dumps({"summaries": summary_rows})

    try:
        llm_response = groq_chat([
            {"role": "system", "content": SYNTHESIZE_REPORT_PROMPT},
            {"role": "user", "content": synthesize_payload},
        ], api_key_env=GROQ_KEY_DESK)
    except Exception as err:
        return f"Skipped report: Groq error ({err})"

    try:
        parsed = parse_llm_json(llm_response)
    except json.JSONDecodeError:
        return "Skipped report: invalid JSON"

    title = clean_text(parsed.get("title") or "")
    report_body = clean_text(parsed.get("report") or "")
    section_item_ids = parsed.get("section_item_ids")
    themes = clean_tags(parsed.get("themes"))

    summary_rows_by_item_id = {summary_row["item_id"]: summary_row for summary_row in summary_rows}
    drop_reason = report_reason(
        title,
        report_body,
        section_item_ids,
        len(summary_rows),
        set(summary_rows_by_item_id),
        themes,
    )
    if drop_reason:
        return f"Skipped report: {drop_reason}"

    section_item_ids = [str(item_id or "") for item_id in section_item_ids]
    section_urls = [summary_rows_by_item_id[item_id].get("url") or "" for item_id in section_item_ids]
    # Headings come from the source's real subject, keyed by section_item_ids — not from
    # the item_id or whatever ## text the model wrote (it tends to echo the item_id). The
    # site renders these, so deriving them here keeps titles correct regardless of the model.
    section_titles = [summary_rows_by_item_id[item_id].get("subject") or "" for item_id in section_item_ids]

    report_entry = {
        "title": title,
        "report": report_body,
        "themes": themes,
        "source_count": len(summary_rows),
        "section_titles": section_titles,
        "section_urls": section_urls,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(REPORT_FILE, "a", encoding="utf-8") as file:
        file.write(json.dumps(report_entry) + "\n")

    return f"Synthesized report ({len(summary_rows)} sources)"
