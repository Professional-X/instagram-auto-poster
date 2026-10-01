"""
recreate_pinterest_pins.py
--------------------------
One-time script: deletes and recreates ALL existing Pinterest pins so they
have the portfolio link + SEO-optimized titles/descriptions.

WHY: Pinterest's API doesn't allow editing existing pins (pin_edit is a
restricted feature not available via Composio's managed app). The only way
to add a link to existing pins is to delete them and create new ones.

This script:
  1. Reads history.json, finds all entries with pinterest_pin_id
  2. For each: DELETE the old pin, then CREATE a new one with:
     - Portfolio link (https://professional-x.github.io/visualhk-designs/)
     - Groq-generated SEO title + description
  3. Updates history.json with the new pin ID

WARNING: This destroys old pin IDs. Any saves/comments on old pins will be
lost. But the content (image, topic, description) is preserved on the new pin.

Usage:
  python scripts/recreate_pinterest_pins.py [--history content/history.json]
                                              [--dry-run]
                                              [--skip-groq]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_pinterest import (  # noqa: E402
    DEFAULT_BOARD_NAME,
    DEFAULT_BOARD_DESCRIPTION,
    PORTFOLIO_LINK,
    find_or_create_board,
    create_pin,
    generate_pinterest_title_and_description,
    build_fallback_title,
    build_fallback_description,
    load_pinterest_config,
)
from publish_instagram import ComposioError, call_composio  # noqa: E402

ACTION_DELETE_PIN = "PINTEREST_DELETE_PIN"


def call_pinterest(action: str, arguments: dict, config: dict) -> dict:
    pinterest_config = dict(config)
    pinterest_config["COMPOSIO_CONNECTED_ACCOUNT_ID"] = config["PINTEREST_CONNECTED_ACCOUNT_ID"]
    return call_composio(action, arguments, pinterest_config)


def delete_pin(pin_id: str, config: dict) -> None:
    """Delete a Pinterest pin by ID."""
    call_pinterest(ACTION_DELETE_PIN, {"pin_id": str(pin_id)}, config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete + recreate all Pinterest pins with portfolio link.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-groq", action="store_true")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    if not history_path.exists():
        print(f"[ERROR] History not found: {history_path}", file=sys.stderr)
        return 2

    with history_path.open("r", encoding="utf-8") as fh:
        history = json.load(fh)

    # Find all entries with a pinterest_pin_id
    pins_to_recreate = []
    for entry in history.get("publications", []):
        pin_id = entry.get("pinterest_pin_id")
        if pin_id:
            pins_to_recreate.append(entry)

    print(f"[INFO] Found {len(pins_to_recreate)} pins to recreate with portfolio link.")
    print(f"[INFO] Portfolio link: {PORTFOLIO_LINK}")

    if not pins_to_recreate:
        print("[INFO] No pins to recreate. Done.")
        return 0

    if args.dry_run:
        print("[INFO] Dry-run mode. Would delete + recreate:")
        for entry in pins_to_recreate:
            print(f"  - pin={entry.get('pinterest_pin_id')}  content_id={entry.get('id')}  topic={entry.get('topic','?')[:40]}")
        return 0

    config = load_pinterest_config()

    # Ensure the board exists
    try:
        board_id = find_or_create_board(config, DEFAULT_BOARD_NAME, DEFAULT_BOARD_DESCRIPTION)
    except ComposioError as exc:
        print(f"[ERROR] Board setup failed: {exc}", file=sys.stderr)
        return 1

    success = 0
    errors = 0
    skipped = 0

    for entry in pins_to_recreate:
        old_pin_id = entry.get("pinterest_pin_id")
        content_id = entry.get("id", "?")
        topic = entry.get("topic", "Web Design Tip")
        seed_fact = entry.get("seed_fact", "")
        image_headline = entry.get("image_headline", topic)
        image_url = entry.get("image_url")

        if not image_url:
            print(f"\n[WARN] {content_id}: no image_url. Skipping.")
            skipped += 1
            continue

        print(f"\n[INFO] Recreating pin for {content_id} (old pin={old_pin_id})")
        print(f"  Topic: {topic}")
        print(f"  Headline: {image_headline}")

        # Generate SEO title + description
        pin_title = None
        pin_description = None
        if not args.skip_groq:
            pin_title, pin_description = generate_pinterest_title_and_description(
                topic, seed_fact, image_headline
            )
        fake_manifest = {"topic": topic, "seed_fact": seed_fact, "image_headline": image_headline}
        if pin_title is None:
            pin_title = build_fallback_title(fake_manifest)
            print(f"  [INFO] Using fallback title: {pin_title[:60]}...")
        if pin_description is None:
            pin_description = build_fallback_description(fake_manifest)
            print(f"  [INFO] Using fallback description ({len(pin_description)} chars).")

        # Step 1: Delete old pin
        try:
            print(f"  Deleting old pin {old_pin_id}...")
            delete_pin(str(old_pin_id), config)
            print(f"  [OK] Old pin deleted.")
        except ComposioError as exc:
            print(f"  [WARN] Delete failed (may already be gone): {str(exc)[:100]}")
            # Continue anyway — we still want to create the new pin

        time.sleep(1.0)

        # Step 2: Create new pin with portfolio link
        try:
            new_pin_id = create_pin(
                board_id=board_id,
                image_url=image_url,
                title=pin_title,
                description=pin_description,
                alt_text=pin_title,
                link=PORTFOLIO_LINK,
                config=config,
            )
            print(f"  [OK] New pin created! Pin ID: {new_pin_id}")
            print(f"       Link: {PORTFOLIO_LINK}")
            # Update history.json with the new pin ID
            entry["pinterest_pin_id"] = new_pin_id
            entry["pinterest_pin_recreated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["pinterest_old_pin_id"] = old_pin_id
            success += 1
            time.sleep(3.0)
        except ComposioError as exc:
            print(f"  [FAIL] New pin creation failed: {exc}")
            errors += 1
            time.sleep(5.0)

    # Save updated history.json
    with history_path.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)
    print(f"\n[INFO] Recreate complete. Success: {success}, Errors: {errors}, Skipped: {skipped}")
    print(f"[INFO] history.json updated with new pin IDs.")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
