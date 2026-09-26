"""
auto_reply.py
-------------
Hourly job that polls comments on recently-published Instagram posts and
auto-replies with a templated DM-inviting message.

Flow:
  1. Read content/history.json -> find posts published in the last 48 hours
     that have a media_id (i.e. successfully published).
  2. Read content/replied_comments.json -> set of comment IDs already replied to.
  3. For each post:
     a. Call Composio INSTAGRAM_GET_POST_COMMENTS (ig_post_id=media_id)
     b. For each comment not in replied set:
        - Pick a reply template (rotated by comment_id hash to avoid spam pattern)
        - Call Composio INSTAGRAM_REPLY_TO_COMMENT (ig_comment_id, message)
        - Add comment_id to replied set
  4. Save replied_comments.json back to disk.

This script NEVER replies to the same comment twice (idempotent via the
replied_comments.json file). It also never replies to its own comments
(filtered by username match if needed — for now we just track IDs).

Usage:
  python scripts/auto_reply.py [--history content/history.json]
                                 [--replied content/replied_comments.json]
                                 [--hours 48]
                                 [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Reuse the Composio transport + helpers from publish_instagram.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_instagram import (  # noqa: E402
    COMPOSIO_EXECUTE_URL,
    REQUIRED_ENV,
    ComposioError,
    ComposioConfigError,
    _sanitize_for_log,
    call_composio,
    load_config,
    verify_connected_account,
)


# ---------------------------------------------------------------------------
# Reply templates — rotated to avoid Instagram's spam detection
# ---------------------------------------------------------------------------

REPLY_TEMPLATES: list[str] = [
    "Thanks for the comment! DM us to discuss your website project.",
    "Appreciate you reaching out! Send us a DM and we'll talk about your site.",
    "Thanks! Want a website like this for your business? DM us to chat.",
    "Glad you liked it! Drop us a DM if you need a website that converts.",
    "Thanks for engaging! DM us to discuss your project.",
    "Appreciate the comment! Send a DM if you're looking for a website redesign.",
    "Thanks! DM us to learn how we can help your business grow online.",
    "Glad this resonated! DM us to get started on your website.",
]


def pick_reply(comment_id: str) -> str:
    """Deterministic but varied reply selection based on comment_id hash.
    Different comments get different templates — avoids the 'posted identical
    text 50 times' pattern that triggers spam detection.
    """
    h = int(hashlib.sha256(comment_id.encode()).hexdigest(), 16)
    return REPLY_TEMPLATES[h % len(REPLY_TEMPLATES)]


# ---------------------------------------------------------------------------
# History + replied-comments tracking
# ---------------------------------------------------------------------------

def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"publications": []}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "publications" not in data:
            return {"publications": []}
        return data
    except (json.JSONDecodeError, OSError):
        return {"publications": []}


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


def find_recent_posts(history: dict[str, Any], hours: int = 48) -> list[dict[str, Any]]:
    """Return posts published in the last `hours` hours that have a media_id."""
    cutoff = time.time() - hours * 3600
    posts = []
    for entry in history.get("publications", []):
        if entry.get("status") != "published":
            continue
        if not entry.get("media_id"):
            continue
        # Parse recorded_at (ISO format like "2026-09-26T06:40:20Z")
        recorded_at = entry.get("recorded_at", "")
        try:
            ts = time.mktime(time.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ"))
            if ts >= cutoff:
                posts.append(entry)
        except (ValueError, TypeError):
            # If we can't parse the timestamp, include it anyway (safer)
            posts.append(entry)
    return posts


# ---------------------------------------------------------------------------
# Composio comment operations
# ---------------------------------------------------------------------------

ACTION_GET_COMMENTS = "INSTAGRAM_GET_POST_COMMENTS"
ACTION_REPLY_TO_COMMENT = "INSTAGRAM_REPLY_TO_COMMENT"


def get_post_comments(media_id: str, config: dict[str, str], limit: int = 50) -> list[dict[str, Any]]:
    """Fetch up to `limit` comments on a post. Returns list of comment dicts,
    each with at least 'id' and 'text' and 'username' fields (per IG Graph API).
    """
    data = call_composio(
        ACTION_GET_COMMENTS,
        arguments={"ig_post_id": str(media_id), "limit": limit},
        config=config,
    )
    # Composio wraps IG Graph API response: {data: [...], paging: {...}}
    comments = data.get("data") or []
    if not isinstance(comments, list):
        return []
    return comments


def reply_to_comment(comment_id: str, message: str, config: dict[str, str]) -> dict[str, Any]:
    """Reply to a single comment. Returns Composio's response data."""
    return call_composio(
        ACTION_REPLY_TO_COMMENT,
        arguments={"ig_comment_id": str(comment_id), "message": message},
        config=config,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-reply to Instagram comments with DM-inviting messages.")
    parser.add_argument("--history", default="content/history.json", help="Path to history.json")
    parser.add_argument("--replied", default="content/replied_comments.json",
                        help="Path to replied_comments.json (idempotency tracker)")
    parser.add_argument("--hours", type=int, default=48,
                        help="Only reply to comments on posts published within the last N hours (default 48)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List comments that would be replied to, but don't actually reply.")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    replied_path = Path(args.replied).resolve()

    print(f"[INFO] Auto-reply job starting (last {args.hours} hours, dry_run={args.dry_run})")

    history = load_history(history_path)
    recent_posts = find_recent_posts(history, hours=args.hours)
    print(f"[INFO] Found {len(recent_posts)} recently-published posts to scan for comments.")

    if not recent_posts:
        print("[INFO] No posts to scan. Done.")
        # Still update last_run timestamp
        replied_data = load_replied(replied_path)
        replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_replied(replied_path, replied_data)
        return 0

    replied_data = load_replied(replied_path)
    replied_map: dict[str, Any] = replied_data.setdefault("replied", {})

    if args.dry_run:
        # Dry-run: skip Composio auth, just list what we'd do
        print("[INFO] Dry-run mode: skipping Composio auth and reply calls.")
        print("[INFO] Would scan these posts for comments:")
        for post in recent_posts:
            print(f"  - {post.get('id')} media_id={post.get('media_id')} topic={post.get('topic','')}")
        replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_replied(replied_path, replied_data)
        return 0

    # Real run: load Composio config and verify account
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

    total_replied = 0
    total_skipped = 0
    total_errors = 0

    for post in recent_posts:
        media_id = post.get("media_id")
        post_id = post.get("id", "?")
        if not media_id:
            continue
        print(f"[INFO] Scanning post {post_id} (media_id={media_id})...")

        try:
            comments = get_post_comments(media_id, config)
        except ComposioError as exc:
            print(f"[WARN] Failed to fetch comments for {post_id}: {exc}")
            total_errors += 1
            continue

        print(f"[INFO] Post {post_id}: found {len(comments)} comments.")

        for comment in comments:
            comment_id = str(comment.get("id", ""))
            if not comment_id:
                continue
            # Skip if already replied
            if comment_id in replied_map:
                total_skipped += 1
                continue

            comment_text = (comment.get("text") or "")[:80]
            comment_user = comment.get("username") or comment.get("from", {}).get("username", "?")

            reply_msg = pick_reply(comment_id)
            print(f"[INFO] Replying to comment {comment_id} (user={comment_user}, "
                  f"text='{comment_text}'): \"{reply_msg}\"")

            try:
                reply_to_comment(comment_id, reply_msg, config)
                replied_map[comment_id] = {
                    "post_id": post_id,
                    "media_id": media_id,
                    "comment_text": comment_text,
                    "comment_user": comment_user,
                    "reply": reply_msg,
                    "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                total_replied += 1
                # Be polite to the API — small delay between replies
                time.sleep(1.0)
            except ComposioError as exc:
                print(f"[WARN] Failed to reply to comment {comment_id}: {exc}")
                total_errors += 1
                time.sleep(2.0)

    replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_replied(replied_path, replied_data)

    print(f"[INFO] Done. Replied: {total_replied}, Skipped (already replied): {total_skipped}, Errors: {total_errors}")
    # Exit 0 if we successfully processed at least one post (even if others errored).
    # Only exit 1 if ALL posts errored — that signals a real problem.
    successful_posts = len(recent_posts) - total_errors
    if total_errors > 0 and successful_posts == 0:
        print(f"[ERROR] All {total_errors} posts failed. Exiting 1.", file=sys.stderr)
        return 1
    if total_errors > 0:
        print(f"[INFO] {total_errors} posts errored but {successful_posts} succeeded. Continuing (exit 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
