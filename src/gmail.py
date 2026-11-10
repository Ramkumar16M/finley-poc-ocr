"""
Gmail client for the local Finley: single-user OAuth, polls one inbox for messages tagged with the
watch label, downloads the raw .eml, and moves the message between labels when done.

First run opens a browser for consent and writes token.json next to credentials.json; after that the
token refreshes on its own.
"""

import base64
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read + label; not send
ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = ROOT / "credentials.json"
TOKEN = ROOT / "token.json"


def service():
    """Authenticated Gmail client. Opens a browser for consent on first run."""
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS.exists():
                raise RuntimeError(
                    f"Put your OAuth client credentials at {CREDENTIALS} "
                    "(Google Cloud Console -> APIs & Services -> Credentials -> OAuth client -> Desktop app)."
                )
            creds = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES).run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def label_id(svc, name: str) -> str:
    """Resolve a label name to its id; create it if missing (gmail.modify covers labels.create)."""
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == name.lower():
            return label["id"]
    created = svc.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def pending(svc, screened_name: str, extra_query: str = "") -> list[str]:
    """
    Inbox messages not yet screened, optionally narrowed by a Gmail search string
    (e.g. 'newer_than:1d subject:Draw'). The label and inbox filters are always applied.
    """
    query = f"in:inbox -label:{screened_name} {extra_query}".strip()
    q = svc.users().messages().list(userId="me", q=query, maxResults=50).execute()
    return [m["id"] for m in q.get("messages", [])]


def raw_eml(svc, message_id: str) -> bytes:
    """The whole message as a .eml file, exactly as Gmail stored it."""
    msg = svc.users().messages().get(userId="me", id=message_id, format="raw").execute()
    return base64.urlsafe_b64decode(msg["raw"].encode("ascii"))


def header(svc, message_id: str, name: str) -> str:
    """One header value from the message metadata (Subject, From, Date, ...)."""
    msg = svc.users().messages().get(
        userId="me", id=message_id, format="metadata", metadataHeaders=[name],
    ).execute()
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def mark_screened(svc, message_id: str, screened_id: str):
    """Done: add the screened label so the next pass skips it. The message stays in the inbox."""
    svc.users().messages().modify(
        userId="me", id=message_id,
        body={"addLabelIds": [screened_id]},
    ).execute()
