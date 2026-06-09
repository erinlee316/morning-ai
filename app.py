import re
import streamlit as st

from tools import DIGEST_FILE, ITEMS_FILE, SUMMARIES_FILE, load_jsonl, normalize_item_id

TITLE_LINK_RE = re.compile(r"\s*\[↗\]\([^)]+\)\s*$")
THEMES_TAIL_RE = re.compile(r"\*\*Themes:\*\*.*$", re.DOTALL)

st.set_page_config(page_title="AI Morning News tl;dr", page_icon=":bookmark:")


def format_digest_report(digest):
  """Clean report markdown and add [↗] source links on ## titles only."""
  report = THEMES_TAIL_RE.sub("", digest.get("report", "")).strip()
  urls = digest.get("section_urls")
  if not urls:
    items = {normalize_item_id(r["item_id"]): r.get("url", "") for r in load_jsonl(ITEMS_FILE)}
    urls = [
      summary.get("url") or items.get(normalize_item_id(summary.get("item_id")), "")
      for summary in load_jsonl(SUMMARIES_FILE)
    ]

  link_idx = 0
  lines = []
  for line in report.splitlines():
    if line.startswith("## ") and not line.startswith("###") and link_idx < len(urls):
      heading = TITLE_LINK_RE.sub("", line[3:].strip())
      url = (urls[link_idx] or "").strip()
      link_idx += 1
      lines.append(f"## {heading} [↗]({url})" if url else f"## {heading}")
    else:
      lines.append(line)
  return "\n".join(lines)


def main():
  digests = load_jsonl(DIGEST_FILE)
  if not digests:
    st.title("Morning AI research digest")
    st.warning("No digest yet. Run `python agent.py` first.")
    return

  digest = digests[-1]
  st.title(digest["title"])
  st.caption(f"{digest['source_count']} sources")
  st.markdown(format_digest_report(digest))
  st.write("**Themes:**", ", ".join(digest["themes"]))


if __name__ == "__main__":
  main()
