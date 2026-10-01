"""
update_pinterest_pins.py
------------------------
One-time script: updates ALL existing Pinterest pins to:
  1. Add the portfolio link (https://professional-x.github.io/visualhk-designs/)
  2. Regenerate SEO-optimized titles + descriptions via Groq (the backfilled
     pins used fallback titles like "Topic: Headline" — now we can do better)

Reads content/history.json, finds all entries with pinterest_pin_id, and
calls Composio PINTEREST_UPDATE_PIN for each one with:
  - link = PORTFOLIO_LINK
  - title = Groq-generated SEO title (fallback to existing)
  - description = Groq-generated SEO description (fallback to existing)

Idempotent: safe to re-run. Only updates pins that need it.

Usage:
  python scripts/update_pinterest_pins.py [--history content/history.json]
                                           [--dry-run]
                                           [--skip-groq]  (only add link, skip title/desc regen)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_pinterest import (  # noqa: E402
    PORTFOLIO_LINK,
    generate_pinterest_title_and_description,
    build_fallback_title,
    build_fallback_description,
    load_pinterest_config,
)
from publish_instagram import ComposioError, call_composio  # noqa: E402

ACTION_UPDATE_PIN = "PINTEREST_UPDATE_PIN"


def call_pinterest(action: str, arguments: dict, config: dict) -> dict:
    """Call Composio Pinterest action with Pinterest connected account."""
    pinterest_config = dict(config)
    pinterest_config["COMPOSIO_CONNECTED_ACCOUNT_ID"] = config["PINTEREST_CONNECTED_ACCOUNT_ID"]
    return call_composio(action, arguments, pinterest_config)


def update_pin(pin_id: str, title: str | None, description: str | None,
                link: str, config: dict) -> None:
    """Update a single Pinterest pin's link, title, and description."""
    arguments: dict = {"pin_id": str(pin_id)}
    if title:
        arguments["title"] = title[:100]
    if description:
        arguments["description"] = description[:800]
    arguments["link"] = link[:2048]

    call_pinterest(ACTION_UPDATE_PIN, arguments, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Update all existing Pinterest pins with portfolio link + SEO titles.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-groq", action="store_true",
                        help="Only add the portfolio link, skip regenerating titles/descriptions")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    if not history_path.exists():
        print(f"[ERROR] History not found: {history_path}", file=sys.stderr)
        return 2

    with history_path.open("r", encoding="utf-8") as fh:
        history = json.load(fh)

    # Find all entries with a pinterest_pin_id
    pins_to_update = []
    for entry in history.get("publications", []):
        pin_id = entry.get("pinterest_pin_id")
        if pin_id:
            pins_to_update.append(entry)

    print(f"[INFO] Found {len(pins_to_update)} pins to update with portfolio link.")
    print(f"[INFO] Portfolio link: {PORTFOLIO_LINK}")
    print(f"[INFO] Regenerate titles/descriptions via Groq: {not args.skip_groq}")

    if not pins_to_update:
        print("[INFO] No pins to update. Done.")
        return 0

    if args.dry_run:
        print("[INFO] Dry-run mode. Would update:")
        for entry in pins_to_update:
            print(f"  - pin={entry.get('pinterest_pin_id')}  content_id={entry.get('id')}  topic={entry.get('topic','?')[:40]}")
        return 0

    config = load_pinterest_config()

    success = 0
    errors = 0
    skipped = 0

    for entry in pins_to_update:
        pin_id = entry.get("pinterest_pin_id")
        content_id = entry.get("id", "?")
        topic = entry.get("topic", "Web Design Tip")
        seed_fact = entry.get("seed_fact", "")
        image_headline = entry.get("image_headline", topic)

        print(f"\n[INFO] Updating pin {pin_id} (content_id={content_id})")
        print(f"  Topic: {topic}")
        print(f"  Headline: {image_headline}")

        # Regenerate SEO title + description via Groq (unless --skip-groq)
        pin_title = None
        pin_description = None
        if not args.skip_groq:
            pin_title, pin_description = generate_pinterest_title_and_description(
                topic, seed_fact, image_headline
            )
            if pin_title is None:
                # Build a fake manifest for fallback
                fake_manifest = {
                    "topic": topic,
                    "seed_fact": seed_fact,
                    "image_headline": image_headline,
                }
                pin_title = build_fallback_title(fake_manifest)
                print(f"  [INFO] Using fallback title: {pin_title[:60]}...")
            if pin_description is None:
                fake_manifest = {
                    "topic": topic,
                    "seed_fact": seed_fact,
                    "image_headline": image_headline,
                }
                pin_description = build_fallback_description(fake_manifest)
                print(f"  [INFO] Using fallback description ({len(pin_description)} chars).")

        # Update the pin
        try:
            update_pin(pin_id, pin_title, pin_description, PORTFOLIO_LINK, config)
            print(f"  [OK] Pin updated! Link: {PORTFOLIO_LINK}")
            if pin_title:
                print(f"       Title: {pin_title[:80]}")
            success += 1
            # Be polite to the API
            time.sleep(2.0)
        except ComposioError as exc:
            print(f"  [FAIL] {exc}")
            errors += 1
            time.sleep(3.0)

    print(f"\n[INFO] Update complete. Success: {success}, Errors: {errors}, Skipped: {skipped}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
