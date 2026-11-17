from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def _load_validator(filename: str) -> Draft202012Validator:
    schema = json.loads((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


_EMAIL_RECEIVED_VALIDATOR = _load_validator("email.received.schema.json")
_EMAIL_SEND_VALIDATOR = _load_validator("email.send.schema.json")


class EmailSchemaError(ValueError):
    """Raised when an email event or command violates its shared schema."""


def _validate(validator: Draft202012Validator, schema_name: str, payload: dict[str, Any]) -> None:
    try:
        validator.validate(payload)
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "$"
        raise EmailSchemaError(f"{schema_name} schema violation at {location}: {exc.message}") from exc


def validate_email_received(event: dict[str, Any]) -> dict[str, Any]:
    _validate(_EMAIL_RECEIVED_VALIDATOR, "email.received", event)
    return event


def build_email_received_event(
    *,
    connection_id: int,
    connection_name: str,
    connection_type: str,
    message: dict[str, Any],
) -> dict[str, Any]:
    event = {
        "event_type": "email.received",
        "schema_version": "1.0.0",
        "source": {
            "service": "KoreComms",
            "connection_id": connection_id,
            "connection_name": connection_name,
            "connection_type": connection_type,
        },
        "message": message,
    }
    return validate_email_received(event)


def build_email_send_command(
    *,
    recipient: str,
    subject: str,
    content: str,
    content_type: str,
    in_reply_to: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "recipient": recipient,
        "subject": subject,
        "content": content,
        "content_type": content_type,
    }
    if in_reply_to is not None:
        message["in_reply_to"] = in_reply_to
    if thread_id is not None:
        message["thread_id"] = thread_id
    command = {
        "event_type": "email.send",
        "schema_version": "1.0.0",
        "message": message,
    }
    _validate(_EMAIL_SEND_VALIDATOR, "email.send", command)
    return command
