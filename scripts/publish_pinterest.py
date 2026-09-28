"""
publish_pinterest.py
--------------------
Publish a content manifest to Pinterest via Composio's v3 REST API.

Pinterest is a SEARCH ENGINE, not a social feed. People search for solutions
("how to make a website for my business", "small business website tips") and
save pins for later. So pins need:
  - SEO-rich description (keywords people search)
  - Clear CTA to email visualhookdesign@gmail.com or DM on Instagram
  - Title that matches what someone would search
  - Posted to a relevant board

Flow:
  1. Ensure the "Web Design Tips for Small Business" board exists (create if not)
  2. If Groq is available: generate a Pinterest-optimized description (longer,
     keyword-rich, ends with email CTA). Falls back to the Instagram caption.
  3. Call Composio PINTEREST_CREATE_PIN with:
     - board_id
     - media_source = { source_type: "image_url", url: <image_url> }
     - title = image_headline (or topic title)
     - description = Pinterest-optimized description
     - alt_text = image_headline

Idempotency: history.json records pinterest_pin_id when a pin is published.
Re-running for the same content ID is a no-op.

Required env vars (set as GitHub Secrets):
  COMPOSIO_API_KEY                  (shared with Instagram)
  COMPOSIO_USER_ID                  (shared)
  PINTEREST_CONNECTED_ACCOUNT_ID    (ca_... for Pinterest)
  PINTEREST_USER_ID                 (numeric Pinterest user ID)
  GROQ_API_KEY                      (optional, for SEO-optimized description)

Usage:
  python scripts/publish_pinterest.py --manifest content/<date>-<slot>.json
                                       --history content/history.json
                                       [--board-name "Web Design Tips for Small Business"]
                                       [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_instagram import (  # noqa: E402
    ComposioError,
    ComposioConfigError,
    _sanitize_for_log,
    call_composio,
    load_history,
    record_publication,
)


# ---------------------------------------------------------------------------
# Pinterest-specific constants
# ---------------------------------------------------------------------------

DEFAULT_BOARD_NAME = "Web Design Tips for Small Business"
DEFAULT_BOARD_DESCRIPTION = (
    "Practical web design tips for small business owners. Learn how to get a "
    "website that brings you clients, what websites really cost, and how to "
    "avoid common mistakes. Freelance web designer available for hire — "
    "email visualhookdesign@gmail.com to discuss your project."
)
EMAIL_CTA = (
    "Need a website for your business? Email visualhookdesign@gmail.com "
    "or DM on Instagram. Free consultation."
)

REQUIRED_ENV = [
    "COMPOSIO_API_KEY",
    "COMPOSIO_USER_ID",
    "PINTEREST_CONNECTED_ACCOUNT_ID",
    "PINTEREST_USER_ID",
]


def load_pinterest_config() -> dict[str, str]:
    """Load Composio + Pinterest config from env. Raise clear error if missing."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise ComposioConfigError(
            "Missing required environment variables: " + ", ".join(missing)
            + "\nSet them as GitHub Actions secrets."
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


# ---------------------------------------------------------------------------
# Pinterest actions via Composio
# ---------------------------------------------------------------------------

ACTION_LIST_BOARDS = "PINTEREST_LIST_BOARDS"
ACTION_CREATE_BOARD = "PINTEREST_CREATE_BOARD"
ACTION_CREATE_PIN = "PINTEREST_CREATE_PIN"


def call_pinterest(action: str, arguments: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    """Execute a Composio v3 Pinterest action. Uses the PINTEREST connected
    account ID (not the Instagram one)."""
    # Reuse call_composio but override the connected_account_id at the body level
    # by temporarily patching the config — call_composio reads config["COMPOSIO_CONNECTED_ACCOUNT_ID"]
    pinterest_config = dict(config)
    pinterest_config["COMPOSIO_CONNECTED_ACCOUNT_ID"] = config["PINTEREST_CONNECTED_ACCOUNT_ID"]
    return call_composio(action, arguments, pinterest_config)


def list_boards(config: dict[str, str]) -> list[dict[str, Any]]:
    """List all boards on the connected Pinterest account."""
    data = call_pinterest(ACTION_LIST_BOARDS, arguments={}, config=config)
    # Pinterest API returns {items: [...]} or {data: [...]}
    items = data.get("items") or data.get("data") or []
    return items if isinstance(items, list) else []


def find_or_create_board(config: dict[str, str], board_name: str,
                          board_description: str) -> str:
    """Find a board by name. If not found, create it. Returns the board ID."""
    print(f"[INFO] Looking for board '{board_name}'...")
    boards = list_boards(config)
    print(f"[INFO] Found {len(boards)} boards on account.")

    for board in boards:
        # Board fields: id, name, description, ...
        if board.get("name", "").strip().lower() == board_name.strip().lower():
            board_id = str(board.get("id", ""))
            print(f"[INFO] Board found: {board_id}")
            return board_id

    # Not found — create it
    print(f"[INFO] Board not found. Creating '{board_name}'...")
    data = call_pinterest(
        ACTION_CREATE_BOARD,
        arguments={
            "name": board_name,
            "description": board_description,
            "privacy": "PUBLIC",
        },
        config=config,
    )
    board_id = str(data.get("id", ""))
    if not board_id:
        raise ComposioError(
            f"PINTEREST_CREATE_BOARD did not return an id. Response keys: {list(data.keys())}"
        )
    print(f"[INFO] Board created: {board_id}")
    return board_id


def create_pin(board_id: str, image_url: str, title: str, description: str,
               alt_text: str, link: str | None, config: dict[str, str]) -> str:
    """Create a Pinterest pin. Returns the pin ID."""
    arguments: dict[str, Any] = {
        "board_id": str(board_id),
        "media_source": {
            "source_type": "image_url",
            "url": image_url,
        },
        "title": title[:100],  # Pinterest max 100 chars
        "description": description[:800],  # Pinterest max 800 chars
        "alt_text": alt_text[:500],  # Pinterest max 500 chars
    }
    if link:
        arguments["link"] = link[:2048]

    data = call_pinterest(ACTION_CREATE_PIN, arguments=arguments, config=config)
    pin_id = str(data.get("id", ""))
    if not pin_id:
        raise ComposioError(
            f"PINTEREST_CREATE_PIN did not return an id. Response keys: {list(data.keys())}"
        )
    return pin_id


# ---------------------------------------------------------------------------
# Pinterest-optimized description via Groq
# ---------------------------------------------------------------------------

def generate_pinterest_description_with_groq(topic: str, seed_fact: str,
                                              image_headline: str) -> str | None:
    """Use Groq to generate a Pinterest-optimized (SEO-rich, longer) description.

    Pinterest descriptions should be:
    - 200-500 chars (longer than Instagram captions)
    - Keyword-rich (people search Pinterest like Google)
    - End with a clear CTA to email visualhookdesign@gmail.com or DM on Instagram
    - No hashtags (Pinterest doesn't use them like Instagram)
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        return None
    model = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"

    system_prompt = (
        "You write Pinterest pin descriptions for a freelance web designer's account. "
        "Pinterest is a SEARCH ENGINE — people search for solutions. Descriptions must be "
        "keyword-rich, natural-sounding, and end with a clear CTA to email "
        "visualhookdesign@gmail.com or DM on Instagram. No hashtags. No emojis. Plain text only."
    )
    user_prompt = f"""Topic: {topic}
Tip/headline: {image_headline}
Seed fact: {seed_fact}

Write a Pinterest pin description that:
1. Starts with a hook that a small business owner would search for
2. Explains the tip in 2-3 plain-English sentences (no jargon)
3. Includes 3-5 relevant keywords naturally woven in (e.g. "small business website",
   "web design", "website redesign", "freelance web designer")
4. Ends with: "Need a website for your business? Email visualhookdesign@gmail.com
   or DM on Instagram for a free consultation."
5. Total length: 200-500 characters
6. NO hashtags, NO emojis, NO markdown

Return STRICT JSON: {{"description": "<your description here>"}}
No prose, no markdown, no explanation. Just the JSON object."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 800,
        "temperature": 0.7,
    }

    print(f"[INFO] Calling Groq for Pinterest-optimized description...")
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=body, timeout=45
        )
    except requests.RequestException as exc:
        print(f"[WARN] Groq network error: {exc}. Using fallback description.")
        return None

    if resp.status_code != 200:
        print(f"[WARN] Groq returned HTTP {resp.status_code}. Using fallback description.")
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        print(f"[WARN] Groq response unparseable: {exc}. Using fallback.")
        return None

    # Strip reasoning blocks + markdown fences
    content = content.strip()
    content = re.sub(r"<reasoning>.*?</reasoning>", "", content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"<reasoning>.*$", "", content, flags=re.IGNORECASE | re.DOTALL)
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        content = content[first_brace : last_brace + 1]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"[WARN] Groq returned non-JSON: {exc}. Using fallback.")
        return None

    desc = parsed.get("description")
    if not isinstance(desc, str) or len(desc.strip()) < 50:
        print("[WARN] Groq description malformed. Using fallback.")
        return None

    print(f"[INFO] Pinterest description generated ({len(desc)} chars).")
    return desc.strip()


def build_fallback_description(manifest: dict[str, Any]) -> str:
    """Fallback Pinterest description when Groq is unavailable."""
    topic = manifest.get("topic", "Web Design Tip")
    fact = manifest.get("seed_fact", "")
    headline = manifest.get("image_headline", "")
    return (
        f"{headline}. {fact} "
        f"This is part of our {topic} series for small business owners. "
        f"{EMAIL_CTA}"
    )[:800]


# ---------------------------------------------------------------------------
# History helpers — reuse publish_instagram's record_publication but add
# pinterest_pin_id field
# ---------------------------------------------------------------------------

def update_history_with_pinterest(history_path: Path, content_id: str, pin_id: str,
                                    board_id: str) -> None:
    """Find the publication entry with the given content_id and add
    pinterest_pin_id + pinterest_board_id fields. Idempotent."""
    history = load_history(history_path)
    for entry in history.get("publications", []):
        if entry.get("id") == content_id:
            entry["pinterest_pin_id"] = pin_id
            entry["pinterest_board_id"] = board_id
            entry["pinterest_published_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            break
    with history_path.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)
    print(f"[INFO] History updated with pinterest_pin_id={pin_id}")


def is_already_pinned(history_path: Path, content_id: str) -> bool:
    """Check if this content_id already has a pinterest_pin_id in history."""
    history = load_history(history_path)
    for entry in history.get("publications", []):
        if entry.get("id") == content_id and entry.get("pinterest_pin_id"):
            return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a content manifest to Pinterest via Composio.")
    parser.add_argument("--manifest", required=True, help="Path to the content manifest JSON.")
    parser.add_argument("--history", default="content/history.json", help="Path to history.json.")
    parser.add_argument("--board-name", default=DEFAULT_BOARD_NAME, help="Pinterest board name.")
    parser.add_argument("--link", default=None, help="Optional destination URL for the pin.")
    parser.add_argument("--dry-run", action="store_true", help="Skip the actual Composio call.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    print(f"[INFO] Loaded manifest: {manifest_path}")
    print(f"[INFO] Content ID: {manifest.get('id')}")

    history_path = Path(args.history).resolve()

    # Idempotency: skip if already pinned
    if is_already_pinned(history_path, manifest["id"]):
        print(f"[INFO] Content {manifest['id']} was already pinned to Pinterest. Skipping (idempotency).")
        return 0

    image_url = manifest.get("image_url")
    if not image_url:
        print(f"[ERROR] Manifest is missing image_url. Run the Instagram publish workflow first "
              f"to get a public image URL.", file=sys.stderr)
        return 1

    image_headline = manifest.get("image_headline", manifest.get("topic", "Web Design Tip"))
    topic = manifest.get("topic", "Web Design Tip")
    seed_fact = manifest.get("seed_fact", "")

    print(f"[INFO] Image URL: {image_url}")
    print(f"[INFO] Headline: {image_headline}")
    print(f"[INFO] Topic: {topic}")

    # Generate Pinterest description (Groq preferred, fallback to deterministic)
    pin_description = generate_pinterest_description_with_groq(topic, seed_fact, image_headline)
    if pin_description is None:
        pin_description = build_fallback_description(manifest)
        print(f"[INFO] Using fallback description ({len(pin_description)} chars).")

    if args.dry_run:
        print("[INFO] Dry-run mode: skipping Composio calls.")
        print(f"[INFO] Board name: {args.board_name}")
        print(f"[INFO] Title: {image_headline[:100]}")
        print(f"[INFO] Description preview: {pin_description[:200]}...")
        return 0

    config = load_pinterest_config()

    # 1. Find or create the board
    try:
        board_id = find_or_create_board(config, args.board_name, DEFAULT_BOARD_DESCRIPTION)
    except ComposioError as exc:
        print(f"[ERROR] Board setup failed: {exc}", file=sys.stderr)
        return 1

    # 2. Create the pin
    try:
        pin_id = create_pin(
            board_id=board_id,
            image_url=image_url,
            title=image_headline,
            description=pin_description,
            alt_text=image_headline,
            link=args.link,
            config=config,
        )
    except ComposioError as exc:
        print(f"[ERROR] Pin creation failed: {exc}", file=sys.stderr)
        return 1

    print(f"[INFO] Pin published! Pin ID: {pin_id}")
    print(f"[INFO] Board ID: {board_id}")

    # 3. Update history.json with the pin ID
    update_history_with_pinterest(history_path, manifest["id"], pin_id, board_id)
    print("[INFO] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
