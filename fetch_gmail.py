import base64
import json
import os
import re
from html import unescape

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ONLY reads emails
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_MESSAGES = 10
OUTPUT_FILE = "emails.jsonl"


def get_header(headers, name):
  """Find one header value like Subject or From."""
  for header in headers:
    if header.get("name", "").lower() == name.lower():
      return header.get("value", "")
  return ""


def decode_body_data(data):
  """Gmail body data is base64url-encoded text."""
  if not data:
    return ""
  padded = data + "=" * (-len(data) % 4)
  return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def html_to_text(html):
  """Convert HTML email bodies to plain text (stdlib only)."""
  text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
  text = re.sub(r"(?i)<br\s*/?>", "\n", text)
  text = re.sub(r"(?i)</p>", "\n", text)
  text = re.sub(r"<[^>]+>", "", text)
  text = unescape(text)
  return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def extract_body(payload):
  """Pull plain text from a Gmail message payload (handles simple + multipart)."""
  mime_type = payload.get("mimeType", "")
  body_data = payload.get("body", {}).get("data")

  if mime_type == "text/plain" and body_data:
    return decode_body_data(body_data)

  if mime_type == "text/html" and body_data:
    return html_to_text(decode_body_data(body_data))

  parts = payload.get("parts", [])
  plain_text = ""
  html_text = ""

  for part in parts:
    part_type = part.get("mimeType", "")
    part_data = part.get("body", {}).get("data")

    if part_type == "text/plain" and part_data:
      plain_text += decode_body_data(part_data)
    elif part_type == "text/html" and part_data:
      html_text += decode_body_data(part_data)
    elif part.get("parts"):
      nested = extract_body(part)
      if nested:
        return nested

  if plain_text.strip():
    return plain_text.strip()
  if html_text.strip():
    return html_to_text(html_text)
  return ""


def message_to_email_dict(service, message_id):
  """Fetch one full message with .get(id=...) and map to agent.py's email shape."""
  msg = service.users().messages().get(
    userId="me",
    id=message_id,
    format="full",
  ).execute()

  payload = msg.get("payload", {})
  headers = payload.get("headers", [])
  body = extract_body(payload)
  if not body.strip():
    body = msg.get("snippet", "")

  return {
    "id": message_id,
    "sender": get_header(headers, "From"),
    "date": get_header(headers, "Date"),
    "subject": get_header(headers, "Subject"),
    "body": body.strip(),
  }


# Shows basic usage of the Gmail API.
# Lists the user's Gmail labels.
def main():

  # there are no credentials that we have access to gmail...
  creds = None

  # The file token.json stores the user's access and refresh tokens, and is
  # created automatically when the authorization flow completes for the first
  # time.
  if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)

  # If there are no (valid) credentials available, let the user log in.
  if not creds or not creds.valid:

    # credentials exist but expired, it refreshes
    if creds and creds.expired and creds.refresh_token:
      creds.refresh(Request())

    # need to fully log in...
    else:

      # gives read-only permission when signing into google
      # store those tokens as our credentials of logging in
      flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
      creds = flow.run_local_server(port=0)

    # save permission in json file so we don't have to log in every time
    with open("token.json", "w") as token:
      token.write(creds.to_json())


  # try to call Google for email access
  try:
    # call the Gmail API
    service = build("gmail", "v1", credentials=creds)

    # .list gets many IDs (not full emails yet)
    results = service.users().messages().list(
      userId="me",
      labelIds=["INBOX"],
      maxResults=MAX_MESSAGES,
    ).execute()
    messages = results.get("messages", [])

    if not messages:
      print("No messages found.")
      return

    print("messages:")
    emails = []
    for message in messages:
      # each message from .list only has id + threadId
      message_id = message["id"]
      print(message_id)

      # .get fetches subject, sender, date, body for one id
      email_dict = message_to_email_dict(service, message_id)
      emails.append(email_dict)
      print(f"Fetched: {email_dict['subject'][:60]}")

    # write them into emails.jsonl (one email per line)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
      for email_dict in emails:
        file.write(json.dumps(email_dict) + "\n")

    print(f"Wrote {len(emails)} emails to {OUTPUT_FILE}")

  except HttpError as error:
    # TODO(developer) - Handle errors from gmail API.
    print(f"An error occurred: {error}")


if __name__ == "__main__":
  main()
