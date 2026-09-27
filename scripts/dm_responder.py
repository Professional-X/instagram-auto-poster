"""
dm_responder.py
---------------
Hourly job that polls Instagram DMs for new conversations and auto-replies
with a friendly intake message asking about budget / timeline / project type.
Captures leads 24/7 even when you're asleep.

Flow:
  1. Read content/dm_replied.json -> set of conversation IDs already handled
  2. Call Composio INSTAGRAM_LIST_ALL_CONVERSATIONS
  3. For each conversation not in replied set:
     a. Call INSTAGRAM_LIST_ALL_MESSAGES (get last few messages)
     b. Find the latest message FROM THE USER (not from us)
     c. If it's new (within last 24h) and we haven't replied:
        - Send intake DM via INSTAGRAM_SEND_TEXT_MESSAGE
        - Mark conversation as replied
  4. Save dm_replied.json

Idempotent: never auto-replies to the same conversation twice.

Usage:
  python scripts/dm_responder.py [--replied content/dm_replied.json]
                                   [--hours 24] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_instagram import (  # noqa: E402
    ComposioError,
    ComposioConfigError,
    call_composio,
    load_config,
    verify_connected_account,
)

ACTION_LIST_CONVERSATIONS = "INSTAGRAM_LIST_ALL_CONVERSATIONS"
ACTION_LIST_MESSAGES = "INSTAGRAM_LIST_ALL_MESSAGES"
ACTION_SEND_TEXT = "INSTAGRAM_SEND_TEXT_MESSAGE"
ACTION_MARK_SEEN = "INSTAGRAM_MARK_SEEN"


# The intake message sent to new DMers. Asks the 3 key qualifying questions.
INTAKE_MESSAGE = (
    "Hey! Thanks for reaching out. I'm a freelance web designer helping small "
    "businesses get websites that actually bring in clients. "
    "To give you the best response, can you tell me:\n\n"
    "1. What's your current website? (or is this a fresh start?)\n"
    "2. What's your budget range?\n"
    "3. When do you need it ready?\n\n"
    "I'll get back to you within a few hours. Looking forward to your project!"
)


def load_replied(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"replied": {}, "last_run": None}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"replied": {}, "last_run": None}
        data.setdefault("replied", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"replied": {}, "last_run": None}


def save_replied(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def list_conversations(config: dict[str, str], limit: int = 20) -> list[dict[str, Any]]:
    """Fetch recent DM conversations."""
    data = call_composio(
        ACTION_LIST_CONVERSATIONS,
        arguments={"limit": limit},
        config=config,
    )
    convos = data.get("data") or []
    return convos if isinstance(convos, list) else []


def list_messages(conversation_id: str, config: dict[str, str], limit: int = 5) -> list[dict[str, Any]]:
    """Fetch recent messages in a conversation."""
    data = call_composio(
        ACTION_LIST_MESSAGES,
        arguments={"conversation_id": str(conversation_id), "limit": limit},
        config=config,
    )
    msgs = data.get("messages") or data.get("data") or []
    return msgs if isinstance(msgs, list) else []


def send_text_message(recipient_id: str, text: str, config: dict[str, str]) -> dict[str, Any]:
    return call_composio(
        ACTION_SEND_TEXT,
        arguments={"recipient_id": str(recipient_id), "text": text},
        config=config,
    )


def mark_seen(recipient_id: str, config: dict[str, str]) -> None:
    """Mark conversation as seen (reduces notification noise for the user)."""
    try:
        call_composio(
            ACTION_MARK_SEEN,
            arguments={"recipient_id": str(recipient_id)},
            config=config,
        )
    except ComposioError:
        pass  # non-fatal


def extract_recipient_from_conversation(convo: dict[str, Any]) -> str | None:
    """Extract the recipient PSID from a conversation object.
    IG Graph API conversation structure varies; try multiple fields.
    Returns a NUMERIC PSID string (e.g. '1234567890'), NOT the conversation ID.
    """
    # Try common fields
    recipients = convo.get("recipients") or convo.get("participants") or {}
    if isinstance(recipients, dict):
        data = recipients.get("data") or []
        if data and isinstance(data, list):
            for r in data:
                rid = r.get("id")
                if rid and str(rid).isdigit():
                    return str(rid)
    if isinstance(recipients, list) and recipients:
        for r in recipients:
            rid = r.get("id")
            if rid and str(rid).isdigit():
                return str(rid)
    # Fall back to conversation id ONLY if it's numeric; otherwise return None
    # (conversation IDs like 'aWdfZAG...' are NOT valid recipient IDs)
    convo_id = convo.get("id", "")
    if str(convo_id).isdigit():
        return str(convo_id)
    return None


def extract_recipient_from_messages(messages: list[dict[str, Any]],
                                      our_user_id: str) -> str | None:
    """Find the recipient PSID from the messages in a conversation.
    Look at the latest message NOT sent by us — its from.id is the recipient.
    Returns a NUMERIC PSID string, or None if not found.
    """
    for msg in messages:
        msg_from = msg.get("from") or {}
        from_id = msg_from.get("id")
        # Skip messages from us (our_user_id)
        if from_id and str(from_id) != str(our_user_id) and str(from_id).isdigit():
            return str(from_id)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-reply to Instagram DMs with intake questions.")
    parser.add_argument("--replied", default="content/dm_replied.json")
    parser.add_argument("--hours", type=int, default=24, help="Only reply to DMs from last N hours")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    replied_path = Path(args.replied).resolve()
    print(f"[INFO] DM responder starting (last {args.hours}h, dry_run={args.dry_run})")

    replied_data = load_replied(replied_path)
    replied_map: dict[str, Any] = replied_data.setdefault("replied", {})

    if args.dry_run:
        print("[INFO] Dry-run mode: skipping Composio calls.")
        replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_replied(replied_path, replied_data)
        return 0

    try:
        config = load_config()
    except ComposioConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    try:
        verify_connected_account(config)
    except ComposioError as exc:
        print(f"[ERROR] Composio auth failed: {exc}", file=sys.stderr)
        return 1

    # 1. List recent conversations
    print("[INFO] Fetching recent DM conversations...")
    try:
        conversations = list_conversations(config)
    except ComposioError as exc:
        print(f"[ERROR] Failed to list conversations: {exc}", file=sys.stderr)
        return 1

    print(f"[INFO] Found {len(conversations)} conversations.")

    total_replied = 0
    total_skipped = 0
    total_errors = 0

    for convo in conversations:
        convo_id = str(convo.get("id", ""))
        if not convo_id:
            continue

        # Skip if already replied
        if convo_id in replied_map:
            total_skipped += 1
            continue

        # Fetch recent messages to find the latest from the user
        try:
            messages = list_messages(convo_id, config)
        except ComposioError as exc:
            print(f"[WARN] Failed to list messages for {convo_id}: {exc}")
            total_errors += 1
            continue

        if not messages:
            continue

        # Find the latest message (messages may be in reverse chronological order)
        latest_msg = messages[0] if messages else {}
        msg_from = latest_msg.get("from") or {}
        msg_text = latest_msg.get("message") or latest_msg.get("text") or ""
        msg_timestamp = latest_msg.get("created_time") or latest_msg.get("timestamp") or ""

        # Check if the latest message is from the user (not from us)
        # IG Graph API: messages from the business have from.username == our username
        # We check if we already sent a message in this conversation
        we_already_replied = any(
            (m.get("from") or {}).get("id") == config.get("INSTAGRAM_USER_ID")
            for m in messages
        )
        if we_already_replied:
            # We've already engaged in this conversation — don't auto-reply
            replied_map[convo_id] = {
                "status": "already_engaged",
                "messages_count": len(messages),
                "last_message": str(msg_text)[:80],
                "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            total_skipped += 1
            continue

        # Get recipient ID to send the DM.
        # PRIMARY: extract from messages (the from.id of the latest message
        # not sent by us). This is the reliable method.
        # FALLBACK: extract from conversation participants (if numeric).
        our_user_id = config.get("INSTAGRAM_USER_ID", "")
        recipient_id = extract_recipient_from_messages(messages, our_user_id)
        if not recipient_id:
            recipient_id = extract_recipient_from_conversation(convo)
        if not recipient_id:
            print(f"[WARN] Could not extract numeric recipient ID for conversation {convo_id}. "
                  f"Skipping (Instagram requires numeric PSID, not conversation ID).")
            replied_map[convo_id] = {
                "status": "no_recipient_id",
                "messages_count": len(messages),
                "last_message": str(msg_text)[:80],
                "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            total_skipped += 1
            continue

        print(f"[INFO] New DM conversation {convo_id} (recipient PSID={recipient_id}, "
              f"msg='{str(msg_text)[:60]}'). Sending intake message...")

        try:
            send_text_message(recipient_id, INTAKE_MESSAGE, config)
            mark_seen(recipient_id, config)
            replied_map[convo_id] = {
                "status": "intake_sent",
                "recipient_id": recipient_id,
                "first_message": str(msg_text)[:80],
                "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            total_replied += 1
            print(f"[INFO]   ↳ Intake message sent to {recipient_id}")
            time.sleep(2.0)  # be polite
        except ComposioError as exc:
            print(f"[WARN]   ↳ Failed to send DM: {exc}")
            replied_map[convo_id] = {
                "status": "failed",
                "recipient_id": recipient_id,
                "error": str(exc)[:200],
                "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            total_errors += 1
            time.sleep(2.0)

    replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_replied(replied_path, replied_data)

    print(f"[INFO] Done. DMs sent: {total_replied}, Skipped: {total_skipped}, Errors: {total_errors}")
    if total_errors > 0 and total_replied == 0 and total_skipped == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
