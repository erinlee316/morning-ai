# Agent tools — implement these yourself.
# See AGENT_PLAN.md for JSONL shapes.
import os
import re
import json
import ollama
from dotenv import load_dotenv
from openai import OpenAI

OLLAMA_MODEL = "qwen2.5:3b"
GROQ_MODEL = "llama-3.3-70b-versatile"

load_dotenv()

ITEMS_FILE = "items.jsonl"
SUMMARIES_FILE = "summaries.jsonl"
SIGNALS_FILE = "signals.jsonl"
DIGEST_FILE = "digest.jsonl"


def load_jsonl(path):
  """Return parsed rows from a JSONL file; missing file → []."""
  try:
    with open(path, 'r', encoding='utf-8') as file:
      return [json.loads(line) for line in file if line.strip()]
  except FileNotFoundError:
    return []


def groq_chat(messages):
  api_key = os.environ.get("GROQ_API_KEY")
  if not api_key:
    raise RuntimeError("GROQ_API_KEY not set (add to .env)")

  client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=api_key,
  )

  response = client.chat.completions.create(
    model=GROQ_MODEL,
    messages=messages,
    response_format={"type": "json_object"},
  )
  return response.choices[0].message.content




def score_signal(item_id, sender, subject, body, source="newsletter"):
  """Filter noise: high_signal + reason → signals.jsonl"""

  sender = clean_sender_email(sender)

  system_prompt = """You are a noise filter for a morning AI/tech research digest.

Your job: decide if this source item is worth including — not to summarize it.

Respond with ONLY valid JSON (no markdown, no code fences, no extra text).
Required keys:
- high_signal (boolean): true or false — not strings
- reason (string): one sentence why this is or is not worth reading
- trend_hint (string): optional 2-5 word tag, or "" if none

Example shape only (do not reuse these facts):
{
  "high_signal": true,
  "reason": "The newsletter reports a new open-source LLM release with benchmark numbers.",
  "trend_hint": "open source models"
}

Do not copy text from this system prompt into reason or trend_hint. Judge only from the item in the user message.

Mark high_signal TRUE when the item has substantive AI/tech content, such as:
- model or product launches, research papers, benchmarks
- funding, acquisitions, major policy/regulation
- meaningful open-source releases or developer tooling
- clear industry trends with specifics (names, numbers, dates)

Mark high_signal FALSE for:
- pure ads, sponsorships, affiliate pitches
- unsubscribe/view-in-browser boilerplate with no real story
- vague hype with no concrete detail
- content unrelated to AI/tech (sports, general business, lifestyle, quotes)
- do not mark TRUE just because the email has numbers or names — AI/tech must be the main topic

Rules:
- high_signal must be JSON boolean true or false (not strings)
- reason must be one clear sentence
- trend_hint: use "" if none
- do not invent facts"""

  user_prompt = json.dumps({
    "item_id": item_id,
    "sender": sender,
    "subject": subject,
    "body": body[:6000],
    "source": source
  })

  response = ollama.chat(
    model=OLLAMA_MODEL,
    messages=
    [
      {"role": "system", "content": system_prompt},
      {"role": "user", "content": user_prompt}
    ],
    format="json",
  )

  try:
    data = clean_llm_response(response.message.content)
    high_signal = data.get("high_signal")
    reason = data.get("reason")
    trend_hint = data.get("trend_hint") or ""

    if isinstance(high_signal, str):
      if high_signal.lower() == "true":
        high_signal = True
      elif high_signal.lower() == "false":
        high_signal = False

    if not isinstance(high_signal, bool):
      high_signal = False
      reason = "Score failed: model did not return high_signal boolean"
    elif not isinstance(reason, str) or not reason.strip():
      high_signal = False
      reason = "Score failed: model did not return a valid reason"
    elif not isinstance(trend_hint, str):
      trend_hint = ""

  except json.JSONDecodeError:
    high_signal = False
    reason = "Score failed: model returned invalid JSON"
    trend_hint = ""

  # always write — success or failure — so orchestrator sees what happened
  with open(SIGNALS_FILE, 'a', encoding='utf-8') as file:
    file.write(json.dumps({
      'item_id': item_id,
      'sender': sender,
      'high_signal': high_signal,
      'reason': reason.strip(),
      'trend_hint': trend_hint.strip(),
    }) + '\n')

  return f"Scored {sender}"




def summarize_item(item_id, sender, subject, body, source="newsletter"):
  """Analyst: summary + topics → summaries.jsonl"""

  sender = clean_sender_email(sender)

  system_prompt = """You are a research analyst summarizing one source for a morning digest.

Write 2-4 factual sentences about the main stories in the text. Name organizations, people, products, or numbers when the source includes them. Plain English — no hype or filler.

Respond with ONLY valid JSON (no markdown, no code fences, no extra text).
Required keys:
- summary (string): 2-4 sentences using ONLY facts from the user message
- topics (array of strings): 2-6 lowercase tags

Example shape only (do not reuse these facts):
{
  "summary": "OpenAI released GPT-5 with improved coding benchmarks and a 40% price cut for API access. Several enterprise customers said they plan to migrate internal tools to the new model this quarter.",
  "topics": ["llm releases", "api pricing", "enterprise adoption"]
}

Do not copy text from this system prompt into summary or topics. Every fact must come from the item body in the user message.

Rules:
- summary: complete sentences — never meta text like "This summary captures..."
- Do not mention AI or ML unless the source explicitly discusses them
- topics: tags must match what you wrote in summary; use specific domains (e.g. cloud computing, hardware, robotics) not vague ai-inventions unless AI is the main story
- Skip ads, unsubscribe links, and boilerplate
- Do not invent facts not supported by the text"""

  user_prompt = json.dumps({
    "item_id": item_id,
    "sender": sender,
    "subject": subject,
    "body": body[:8000],
    "source": source
  })

  try:
    content = groq_chat([
      {"role": "system", "content": system_prompt},
      {"role": "user", "content": user_prompt},
    ])
  except Exception as err:
    return f"Skipped {sender}: Groq error ({err})"

  try:
    data = clean_llm_response(content)
  except json.JSONDecodeError:
    return f'Skipped {sender}: invalid JSON'
  
  summary = clean_text(data.get("summary") or "")
  reject_reason = text_rejection_reason(summary, field="summary", placeholder_phrases=SUMMARY_PLACEHOLDER_PHRASES)

  if reject_reason:
    return f"Skipped {sender}: {reject_reason}"

  topics = normalize_topics(data.get("topics"))
  if not topics:
    return f"Skipped {sender}: bad topics"


  with open(SUMMARIES_FILE, 'a', encoding='utf-8') as file:
    summaries = {
      "item_id": item_id,
      "sender": sender,
      "summary": summary, 
      "topics": topics
    }
    file.write(json.dumps(summaries) + '\n')

  return f"Summarized {sender}"




def synthesize_digest():
  """Analyst: one morning report from high-signal items → digest.jsonl"""

  summaries = load_jsonl(SUMMARIES_FILE)
  if not summaries:
    return "No summaries to synthesize"
    
  system_prompt = """You are a senior AI research analyst writing one morning digest from several per-source summaries.

Your job: synthesize — do NOT simply list summaries one by one.

Input: JSON with a "summaries" array. Each item has item_id, sender, summary, and topics from newsletters already filtered as high-signal.

Respond with ONLY valid JSON (no markdown fences, no extra text).
Required keys:
- title (string): one-line digest title, e.g. Morning AI Digest
- report (string): full morning briefing as plain text; use blank lines between sections; optional ## headings
- themes (array of strings): 3-8 short lowercase cross-cutting tags

Example shape only (do not reuse these facts):
{
  "title": "Morning AI Digest",
  "report": "OpenAI released a new model with improved reasoning benchmarks. Anthropic expanded Claude's context window to 200k tokens.",
  "themes": ["model releases", "benchmarks", "context windows"]
}

Do not copy text from this system prompt into title, report, or themes. Every fact must come from the summaries in the user message.

How to write the report:
- Merge overlapping stories (same company/trend across sources → one paragraph)
- Group by topic/story, not by source order
- Lead with the most important developments
- Include specifics already in the summaries (names, products, numbers) — do not invent new facts
- Note open questions or weak claims only if the summaries hint at uncertainty
- Skip fluff, ads, and meta commentary about newsletters themselves
- If summaries cover few items, write a shorter digest — do not pad

Rules:
- title: one line, no angle brackets
- report: briefing body only — news and analysis, no meta wrap-up; never placeholder text like "full morning briefing"
- themes: tags in the themes array only — not repeated at the end of report
- report must NOT contain: "themes", "key themes", "today's big ideas", or a bullet/tag list at the end
- Use only information supported by the provided summaries"""


  user_prompt = json.dumps({"summaries": summaries})

  try:
    content = groq_chat([
      {"role": "system", "content": system_prompt},
      {"role": "user", "content": user_prompt},
    ])
  except Exception as err:
    return f"Skipped digest: Groq error ({err})"

  try:
    data = clean_llm_response(content)

  except json.JSONDecodeError:
    return "Skipped digest: invalid JSON"

  title = clean_text(data.get("title") or "")
  report = clean_text(data.get("report") or "")
  themes = normalize_topics(data.get("themes"))

  if not title:
    return "Skipped digest: bad title"

  report_reject = text_rejection_reason(report, field="report", placeholder_phrases=DIGEST_REPORT_PLACEHOLDER_PHRASES)

  if report_reject:
    return f"Skipped digest: {report_reject}"

  if not themes:
    return "Skipped digest: bad themes"

  with open(DIGEST_FILE, 'a', encoding='utf-8') as file:
    digest = {
      "title": title,
      "report": report,
      "themes": themes,
      "source_count": len(summaries)
    }
    file.write(json.dumps(digest) + '\n')
  
  return f"Synthesized digest ({len(summaries)} sources)"



# extracts sender's email cleanly 
def clean_sender_email(sender):

  # email address returned as <email@domain.com>
  # searches and returns email@domain.com
  pattern = r"<([^>]+)>"
  match = re.search(pattern, sender or "") # use "" if sender is None

  if match: 
    # group(0) -> finds <email@domain.com>
    # group(1) -> has <...> -> extract email
    return match.group(1).strip()
  
  # if sender is None -> use ""
  return (sender or "").strip()



# for summary or digest
# agent accidently adds "<__: ...>", and we only want ... part 
def clean_text(text):
  text = (text or "").strip()
  text = re.sub(r"^<[^>]+:\s*", "", text, flags=re.IGNORECASE)
  text = text.rstrip(">").strip()
  return text


MIN_TEXT_CHARS = 80

SUMMARY_PLACEHOLDER_PHRASES = (
  "who/what",
  "why it matters",
  "this summary captures",
  "key developments, who",
  "key developments who",
)

DIGEST_REPORT_PLACEHOLDER_PHRASES = (
  "full morning briefing",
  "cross-cutting theme",
  "optional ## headings",
)



def text_rejection_reason(text, field, placeholder_phrases, min_chars=MIN_TEXT_CHARS):
  if not text or not text.strip():
    return f"empty {field}"

  text = text.strip()
  if len(text) < min_chars:
    return f"{field} too short ({len(text)} chars, need {min_chars}+)"

  lower = text.lower()
  for phrase in placeholder_phrases:
    if phrase in lower:
      return f"{field} looks like prompt placeholder text"

  if text.startswith("<") or text.endswith(">"):
    return f"{field} contains template angle brackets"

  return None


def normalize_topics(topics_list):
  if not isinstance(topics_list, list):
    return []

  topics = []
  for tag in topics_list:
    if not isinstance(tag, str):
      continue
    tag = tag.strip().lower()
    if tag.startswith("<") and tag.endswith(">"):
      tag = tag[1:-1].strip()
    if not tag:
      continue
    topics.append(tag)
  return topics



# convert llm's response into clean dictionary
def clean_llm_response(text):
  text = text.strip()
  if text.startswith("```"):
    text = text.split("```")[1]
    if text.startswith("json"):
      text = text[4:]
    text = text.strip()
  return json.loads(text)




