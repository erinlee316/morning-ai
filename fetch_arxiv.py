import re
import requests
import xml.etree.ElementTree as ET # ArXiv papers return XML not JSON format

from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from prompts import load_prompt
from tools import GROQ_KEY_ARXIV, groq_select_ids

load_dotenv()

# arXiv announces Mon–Fri in a daily batch around this hour (UTC).
# Papers in that batch are stamped ~17:59 UTC, so we match by announcement day.
ARXIV_ANNOUNCE_HOUR_UTC = 18
ARXIV_API = "http://export.arxiv.org/api/query"
# later tells format of tags belonging to Atom or ArXiv
# helps us find the entry for the tree (title, abstract, author, etc.)
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

MAX_CANDIDATES = 40
MAX_GROQ_CANDIDATES = 20
MAX_OUTPUT = 4
MIN_OUTPUT = 3
MAX_BODY_CHARS = 8000
MAX_GROQ_BODY_CHARS = 1200
USER_AGENT = "AgenticAI-ResearchBot/1.0 (+https://github.com/)"

ARXIV_CATEGORIES = ("cs.RO", "cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.SY", "cs.MA")


def arxiv_id_from_entry(entry):
    """Extract bare arXiv id (no version) from an Atom entry id URL."""
    raw_id = (entry.findtext("atom:id", default="", namespaces=ATOM_NS) or "").strip()
    match = re.search(r"arxiv\.org/abs/([^/\s]+)", raw_id)
    if not match:
        return ""
    paper_id = match.group(1)
    return re.sub(r"v\d+$", "", paper_id)


def entry_published_dt(entry):
    """Parse Atom published timestamp to UTC datetime, or None on failure."""
    published = (entry.findtext("atom:published", default="", namespaces=ATOM_NS) or "").strip()
    if not published:
        return None
    try:
        # arXiv uses ISO-8601, e.g. 2024-06-10T12:34:56Z
        if published.endswith("Z"):
            published = published[:-1] + "+00:00"
        return datetime.fromisoformat(published)
    except ValueError:
        return None


def previous_weekday(day):
    """Step back one calendar day, skipping Saturday and Sunday."""
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def latest_announcement_day(now=None):
    """Calendar date (UTC) of the arXiv batch we expect to be current."""
    now = now or datetime.now(timezone.utc)
    day = now.date()

    if day.weekday() >= 5:  # weekend -> Friday's batch
        day -= timedelta(days=day.weekday() - 4)
    elif now.hour < ARXIV_ANNOUNCE_HOUR_UTC:  # before today's drop -> previous weekday
        day = previous_weekday(day)

    return day


def entries_for_batch_day(all_entries, batch_day):
    """Keep parsed entries whose published date matches batch_day."""
    matched = []
    for entry in all_entries:
        if not in_batch_on_day(entry, batch_day):
            continue
        paper_id = arxiv_id_from_entry(entry)
        if not paper_id:
            continue
        matched.append(entry)
    return matched


def in_batch_on_day(entry, batch_day):
    """True if this paper was published on the given announcement day (UTC)."""
    published = entry_published_dt(entry)
    if published is None:
        return False
    return published.date() == batch_day


def entry_categories(entry):
    """Return arXiv category terms for one entry."""
    terms = []
    for tag in entry.findall("atom:category", ATOM_NS):
        term = (tag.get("term") or "").strip()
        if term:
            terms.append(term)
    return terms


def entry_authors(entry):
    """Comma-join author names from an Atom entry."""
    names = []
    for author in entry.findall("atom:author", ATOM_NS):
        name = (author.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
        if name:
            names.append(name)
    return ", ".join(names) or "unknown"


def paper_body(entry, categories):
    """Build a readable body from abstract and metadata."""
    title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
    title = re.sub(r"\s+", " ", title)
    summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
    summary = re.sub(r"\s+", " ", summary)
    primary = (entry.findtext("arxiv:primary_category", default="", namespaces=ATOM_NS) or "").strip()
    if not primary and categories:
        primary = categories[0]

    parts = [
        f"Title: {title}",
        f"Categories: {', '.join(categories) or primary or 'unknown'}",
        "",
        summary,
    ]
    return "\n".join(parts).strip()[:MAX_BODY_CHARS]


def paper_to_item(entry, body=None):
    """Map an arXiv Atom entry to the shared items.jsonl item shape."""
    paper_id = arxiv_id_from_entry(entry)
    item_id = f"arxiv_{paper_id}"
    title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
    title = re.sub(r"\s+", " ", title)
    categories = entry_categories(entry)

    return {
        "item_id": item_id,
        "source": "arxiv",
        "subject": title,
        "author": entry_authors(entry),
        "url": f"https://arxiv.org/abs/{paper_id}",
        "body": body if body is not None else paper_body(entry, categories),
    }


def fetch_recent_papers():
    """Query arXiv for papers from the latest daily announcement batch (18:00 UTC cadence)."""
    category_query = " OR ".join(f"cat:{cat}" for cat in ARXIV_CATEGORIES)
    params = {
        "search_query": category_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": MAX_CANDIDATES,
    }

    try:
        response = requests.get(
            ARXIV_API,
            params=params,
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except requests.RequestException as err:
        print(f"arXiv API error: {err}")
        return []

    # REMEMBER -> ArXiv is in XML format, not JSON
    root = ET.fromstring(response.text) # whole feed of ArXiv paper
    all_entries = root.findall("atom:entry", ATOM_NS)

    # Papers in a batch are stamped ~17:59 UTC on announcement day (just before 18:00).
    batch_day = latest_announcement_day()
    entries = entries_for_batch_day(all_entries, batch_day)

    # After 18:00 UTC, today's batch may not be indexed yet — use the prior weekday.
    if not entries:
        fallback_day = previous_weekday(batch_day)
        entries = entries_for_batch_day(all_entries, fallback_day)
        if entries:
            batch_day = fallback_day

    print(
        f"arXiv: {len(entries)} papers from {batch_day} batch "
        f"(announced ~{ARXIV_ANNOUNCE_HOUR_UTC}:00 UTC, from {MAX_CANDIDATES} fetched)"
    )
    return entries


def fetch_top_papers():
    """Filter recent arXiv papers, curate with Groq, and return item dicts for items.jsonl."""
    entries = fetch_recent_papers()
    if not entries:
        return []

    trimmed = entries[:MAX_GROQ_CANDIDATES]
    if len(entries) > MAX_GROQ_CANDIDATES:
        print(f"arXiv: sending newest {MAX_GROQ_CANDIDATES} of {len(entries)} to Groq")

    bodies_by_id = {}
    groq_candidates = []
    entries_by_id = {}

    for entry in trimmed:
        paper_id = arxiv_id_from_entry(entry)
        item_id = f"arxiv_{paper_id}"
        body = paper_body(entry, entry_categories(entry))
        bodies_by_id[item_id] = body
        entries_by_id[item_id] = entry
        groq_candidates.append({
            "item_id": item_id,
            "title": (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip(),
            "categories": entry_categories(entry),
            "body": body[:MAX_GROQ_BODY_CHARS],
        })

    selected_ids = groq_select_ids(
        load_prompt("arxiv_system.txt"),
        {"min_pick": MIN_OUTPUT, "max_pick": MAX_OUTPUT, "papers": groq_candidates},
        GROQ_KEY_ARXIV,
    )
    print(f"arXiv: LLM selected {len(selected_ids)} papers")

    items = []
    for item_id in selected_ids:
        entry = entries_by_id.get(item_id)
        if entry is None:
            continue
        items.append(paper_to_item(entry, body=bodies_by_id.get(item_id)))

    return items


def main():
    """Fetch curated arXiv papers and print a short summary."""
    from tools import write_items

    print("Fetching arXiv…")
    items = fetch_top_papers()
    if items:
        write_items(items)
    return items


if __name__ == "__main__":
    main()
