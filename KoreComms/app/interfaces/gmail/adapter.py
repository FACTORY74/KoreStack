# ====================================================================================================
# MARK: OVERVIEW
# ====================================================================================================
# Gmail interface adapter for KoreComms.
#
# Authentication:
#   OAuth2 with a stored refresh token.  The refresh token and client credentials are
#   stored encrypted in interfaces.config_json under the keys 'client_id', 'client_secret',
#   and 'refresh_token'.
#
# Polling:
#   Uses the Gmail "after:" search operator anchored to the timestamp of the last successful
#   poll (stored as 'last_poll_epoch' in config_json).  Messages are de-duplicated via their
#   Gmail message ID stored in external_messages.external_message_id.
#
# Related modules:
#   - app/interfaces/common/base.py     -- BaseInterface ABC
#   - app/interfaces/common/registry.py -- registered as type "gmail"
#   - app/interfaces/gmail/oauth.py     -- OAuth helpers for the WebUI authorization flow
#   - app/crypto.py                     -- encrypt/decrypt OAuth credentials
#   - app/database.py                   -- conversation routing and dedup records
# MARK: FUNCTIONS
# Primary types: GmailInterface.
# Function inventory:
# - _decrypt_config: Implements the  decrypt config operation for this module.
# - _build_service: Implements the  build service operation for this module.
# - _extract_body: Implements the  extract body operation for this module.
# - _header_value: Implements the  header value operation for this module.
# - poll: Implements the poll operation for this module.
# - route_reply: Implements the route reply operation for this module.
# - send_new: Implements the send new operation for this module.
# ====================================================================================================
from __future__ import annotations

import base64
import json
import logging
import re
import time
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr

import google.auth.transport.requests
import google.oauth2.credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app import crypto, database as db
from app.email_contract import build_email_received_event, build_email_send_command
from app.interfaces.common.base import BaseInterface
from app.interfaces.gmail.oauth import SCOPES

logger = logging.getLogger(__name__)

_SECRET_KEYS = ("client_id", "client_secret", "refresh_token")
_MESSAGE_ID_RE = re.compile(r"<[^>]+>")


class GmailInterface(BaseInterface):

    def _decrypt_config(self) -> dict:
        """Return config dict with secret values decrypted."""
        raw = json.loads(self.config.get("config_json", "{}"))
        result = {}
        for k, v in raw.items():
            if k in _SECRET_KEYS and v:
                try:
                    result[k] = crypto.decrypt(v)
                except Exception:
                    result[k] = v
            else:
                result[k] = v
        return result

    def _build_service(self):
        cfg = self._decrypt_config()
        creds = google.oauth2.credentials.Credentials(
            token=None,
            refresh_token=cfg.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cfg.get("client_id"),
            client_secret=cfg.get("client_secret"),
            scopes=SCOPES,
        )
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    @staticmethod
    def _extract_body(payload: dict) -> str:
        """Recursively extract plain-text body from a Gmail message payload."""
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if "parts" in payload:
            for part in payload["parts"]:
                text = GmailInterface._extract_body(part)
                if text:
                    return text
        return ""

    @staticmethod
    def _header_value(headers: list[dict], name: str) -> str:
        name_lower = name.lower()
        for h in headers:
            if h.get("name", "").lower() == name_lower:
                return h.get("value", "")
        return ""

    def poll(self) -> list[dict]:
        cfg = self._decrypt_config()
        if not cfg.get("refresh_token"):
            logger.warning("Gmail interface %d: no refresh token — skipping poll", self.interface_id)
            return []

        last_epoch = int(cfg.get("last_poll_epoch", 0))
        after = max(0, last_epoch - 30)
        query = f"is:unread after:{after}" if after else "is:unread"

        try:
            service = self._build_service()
            result = service.users().messages().list(
                userId="me", q=query, maxResults=100
            ).execute()
        except HttpError as exc:
            logger.error("Gmail poll failed for interface %d: %s", self.interface_id, exc)
            return []

        messages: list[dict] = []
        for ref in result.get("messages", []):
            gm_id = ref["id"]
            if db.external_message_exists(gm_id):
                continue
            try:
                gm_msg = service.users().messages().get(
                    userId="me", id=gm_id, format="full"
                ).execute()
            except HttpError as exc:
                logger.error("Gmail get message %s failed: %s", gm_id, exc)
                continue

            headers = gm_msg.get("payload", {}).get("headers", [])
            subject = self._header_value(headers, "Subject") or "(no subject)"
            from_header = self._header_value(headers, "From")
            reply_to = self._header_value(headers, "Reply-To") or None
            sender = reply_to if reply_to and parseaddr(reply_to)[1] else from_header
            thread_id = gm_msg.get("threadId", gm_id)
            body = self._extract_body(gm_msg.get("payload", {}))
            header_values = {
                name: self._header_value(headers, name) or None
                for name in ("Message-ID", "In-Reply-To", "References", "Date")
            }
            references = _MESSAGE_ID_RE.findall(
                f"{header_values['References'] or ''} {header_values['In-Reply-To'] or ''}"
            )
            messages.append(build_email_received_event(
                connection_id=self.interface_id,
                connection_name=self.name,
                connection_type="gmail",
                message={
                    "external_message_id": gm_id,
                    "external_thread_id": thread_id,
                    "reply_references": references,
                    "sender": sender,
                    "from": from_header,
                    "reply_to": reply_to,
                    "to": [address for _, address in getaddresses([self._header_value(headers, "To")]) if address],
                    "cc": [address for _, address in getaddresses([self._header_value(headers, "Cc")]) if address],
                    "subject": subject,
                    "content": body,
                    "content_type": "text/plain",
                    "headers": {
                        "message_id": header_values["Message-ID"],
                        "in_reply_to": header_values["In-Reply-To"],
                        "references": header_values["References"],
                        "date": header_values["Date"],
                    },
                },
            ))

        raw_cfg = json.loads(self.config.get("config_json", "{}"))
        raw_cfg["last_poll_epoch"] = int(time.time())
        db.interface_update(
            self.interface_id,
            self.config.get("name", self.name),
            raw_cfg,
            bool(self.config.get("enabled", True)),
        )

        return messages

    def route_reply(self, conversation_id: int, content: str) -> None:
        conv = db.conversation_get(conversation_id)
        last_msg = db.external_message_get_last_inbound(conversation_id)

        thread_id = conv["external_thread_id"] if conv else None
        to = last_msg["sender_display"] if last_msg else ""
        subject = conv.get("subject", "") if conv else ""

        command = build_email_send_command(
            recipient=to,
            subject=f"Re: {subject}" if not subject.startswith("Re:") else subject,
            content=content,
            content_type="text/plain",
            thread_id=thread_id,
        )
        message_data = command["message"]
        mime = MIMEText(message_data["content"])
        mime["To"] = message_data["recipient"]
        mime["Subject"] = message_data["subject"]
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        body: dict = {"raw": raw}
        if message_data.get("thread_id"):
            body["threadId"] = message_data["thread_id"]

        try:
            service = self._build_service()
            service.users().messages().send(userId="me", body=body).execute()
        except HttpError as exc:
            raise RuntimeError(f"Gmail send failed: {exc}") from exc

    def send_new(self, recipient: str, subject: str, content: str) -> dict:
        command = build_email_send_command(
            recipient=recipient,
            subject=subject,
            content=content,
            content_type="text/plain",
        )
        message_data = command["message"]
        mime = MIMEText(message_data["content"])
        mime["To"] = message_data["recipient"]
        mime["Subject"] = message_data["subject"]
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        try:
            service = self._build_service()
            sent = service.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
        except HttpError as exc:
            raise RuntimeError(f"Gmail send_new failed: {exc}") from exc

        return {
            "external_thread_id": sent.get("threadId", sent.get("id")),
            "external_message_id": sent.get("id"),
        }