import re
import streamlit as st

from tools import DIGEST_FILE, load_jsonl






st.set_page_config(page_title="AI Morning News tl;dr", page_icon=":bookmark:")


def load_latest_digest():
  lines = load_jsonl(DIGEST_FILE)
  if not lines:
    return None
  return lines[-1]


# safety check 
# remove themes if LLM failed to do so when synthesizing newsletters
def clean_report(report):
  pattern = r"(\*\*Themes:\*\*.*$)" # .* -> all characters EXCEPT \n
  report = re.sub(pattern, "", report or "", flags=re.DOTALL) # re.DOTALL -> all characters, including \n
  return report.strip()




def main():
  digest = load_latest_digest()

  if digest is None:
    st.title("Morning AI research digest") # fallback when no digest
    st.warning("No digest yet. Run `python agent.py` first.")
    return

  st.title(digest['title'])
  st.caption(f"{digest['source_count']} sources")

  # gives our daily digest of AI news!
  st.markdown(clean_report(digest["report"]))
  st.write("**Themes:**", ", ".join(digest["themes"]))




if __name__ == "__main__":
  main()
