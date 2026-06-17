"""Fetch recent arXiv papers on two tracks (robotics + secondary), Groq-pick, and write items.jsonl."""

import re
import requests
import xml.etree.ElementTree as ET # arXiv returns XML, not JSON

from dotenv import load_dotenv
from prompts import load_prompt
from tools import GROQ_KEY_ARXIV, MAX_GROQ_BODY_CHARS, USER_AGENT, pick_item_ids, write_items

load_dotenv()



# --- Config ---

ARXIV_API = "https://export.arxiv.org/api/query"
# XML namespace URIs must match the feed exactly (arXiv uses http, not https).
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Two-track fetch: cs.RO (robotics) is low-volume and gets drowned out when mixed with the
# high-volume ML/NLP categories in one query, so we pull it on its own track with guaranteed
# slots, then fill the rest from the secondary categories.
ROBOTICS_CATEGORIES = ("cs.RO",)
SECONDARY_CATEGORIES = ("cs.CV", "cs.AI", "cs.LG", "cs.CL", "cs.SY", "cs.MA")

MAX_ROBOTICS_OPTIONS = 12    # guaranteed robotics slots; secondary fills the rest
MAX_PICK_OPTIONS = 20        # total pool size before Groq picks
MAX_PICKS = 4                # Groq pick target (prompt-only, not enforced in code)
MAX_BODY_CHARS = 3000        # title + categories + abstract

ARXIV_SYSTEM_PROMPT = load_prompt("arxiv_system.txt")



# --- Atom entry parsing (arXiv API returns XML, not JSON) ---

def entry_arxiv_id(entry):
    """Extract bare arXiv id (no version) from an Atom entry id URL."""
    raw_id = (entry.findtext("atom:id", default="", namespaces=ATOM_NS) or "").strip()
    match = re.search(r"arxiv\.org/abs/([^/\s]+)", raw_id)
    if not match:
        return ""
    arxiv_id = match.group(1)
    return re.sub(r"v\d+$", "", arxiv_id)


def entry_title(entry):
    """Return a single-line title from an Atom entry."""
    title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
    return re.sub(r"\s+", " ", title)


def entry_abstract(entry):
    """Return a single-line abstract from an Atom entry."""
    abstract = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
    return re.sub(r"\s+", " ", abstract)


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



# --- Item shaping ---

def paper_body(entry):
    """Build a plain-text body from an Atom entry's title, categories, and abstract."""
    categories = entry_categories(entry)
    title = entry_title(entry)
    abstract = entry_abstract(entry)
    primary_category = (entry.findtext("arxiv:primary_category", default="", namespaces=ATOM_NS) or "").strip()
    if not primary_category and categories:
        primary_category = categories[0]

    parts = [
        f"Title: {title}",
        f"Categories: {', '.join(categories) or primary_category or 'unknown'}",
        "",
        abstract,
    ]
    return "\n".join(parts).strip()[:MAX_BODY_CHARS]


def paper_to_item(entry, body=None):
    """Map an arXiv Atom entry to dict in the shared items.jsonl item shape."""
    arxiv_id = entry_arxiv_id(entry)
    return {
        "item_id": f"arxiv_{arxiv_id}",
        "source": "arxiv",
        "subject": entry_title(entry),
        "author": entry_authors(entry),
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "body": body if body is not None else paper_body(entry),
    }



# --- Fetch ---

def fetch_category_entries(categories, max_results):
    """Query arXiv for the newest `max_results` papers in `categories` (OR'd) -> Atom entries."""
    search_query = " OR ".join(f"cat:{cat}" for cat in categories)
    params = {
        "search_query": search_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
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
        print(f"arXiv API error ({'+'.join(categories)}): {err}")
        return []

    root = ET.fromstring(response.text)
    return root.findall("atom:entry", ATOM_NS)


def gather_pick_options():
    """Robotics papers as guaranteed slots, then fill with newest secondary-category papers.

    Returns (entries deduped with robotics first, robotics_count).
    """
    robotics_entries = fetch_category_entries(ROBOTICS_CATEGORIES, MAX_ROBOTICS_OPTIONS)
    # Over-fetch secondary so dedupe against robotics cross-listings still leaves enough to fill.
    secondary_entries = fetch_category_entries(SECONDARY_CATEGORIES, MAX_PICK_OPTIONS)

    seen = set()
    pick_options = []
    for entry in robotics_entries[:MAX_ROBOTICS_OPTIONS]:
        arxiv_id = entry_arxiv_id(entry)
        if arxiv_id and arxiv_id not in seen:
            seen.add(arxiv_id)
            pick_options.append(entry)
    robotics_count = len(pick_options)

    for entry in secondary_entries:
        if len(pick_options) >= MAX_PICK_OPTIONS:
            break
        arxiv_id = entry_arxiv_id(entry)
        if arxiv_id and arxiv_id not in seen:
            seen.add(arxiv_id)
            pick_options.append(entry)

    return pick_options, robotics_count



# --- Groq pick ---

def fetch_selected_papers():
    """Two-track fetch (robotics guaranteed + secondary fill) -> Groq pick -> item dicts for items.jsonl."""
    pick_options, robotics_count = gather_pick_options()
    if not pick_options:
        return []

    print(
        f"arXiv: {robotics_count} robotics + {len(pick_options) - robotics_count} secondary "
        f"= {len(pick_options)} candidates to Groq"
    )

    bodies_by_item_id = {}
    entries_by_item_id = {}
    groq_options = []

    for entry in pick_options:
        arxiv_id = entry_arxiv_id(entry)
        item_id = f"arxiv_{arxiv_id}"
        body = paper_body(entry)
        bodies_by_item_id[item_id] = body
        entries_by_item_id[item_id] = entry
        groq_options.append({
            "item_id": item_id,
            "title": entry_title(entry),
            "categories": entry_categories(entry),
            "body": body[:MAX_GROQ_BODY_CHARS],
        })

    selected_ids = pick_item_ids(
        ARXIV_SYSTEM_PROMPT,
        {"max_pick": MAX_PICKS, "papers": groq_options},
        GROQ_KEY_ARXIV,
    )
    print(f"arXiv: LLM selected {len(selected_ids)} papers")

    items = []
    for item_id in selected_ids:
        entry = entries_by_item_id.get(item_id)
        if entry is None:
            continue
        items.append(paper_to_item(entry, body=bodies_by_item_id.get(item_id)))

    return items


# --- CLI ---

def main():
    """Fetch selected arXiv papers and write them to items.jsonl."""
    print("Fetching arXiv…")
    items = fetch_selected_papers()
    if items:
        write_items(items)
    return items


if __name__ == "__main__":
    main()
