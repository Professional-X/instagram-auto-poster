"""
backfill_pinterest.py
---------------------
Backfill script: publishes all already-published Instagram posts to Pinterest.
Reads content/history.json, finds entries with status=published and media_id
but no pinterest_pin_id, and publishes each one to Pinterest.

Usage:
  python scripts/backfill_pinterest.py [--history content/history.json]
                                         [--board-name "Web Design Tips for Small Business"]
                                         [--dry-run]
                                         [--limit 10]
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
    PORTFOLIO_LINK,
    find_or_create_board,
    create_pin,
    generate_pinterest_title_and_description,
    build_fallback_title,
    build_fallback_description,
    load_pinterest_config,
    update_history_with_pinterest,
    is_already_pinned,
    load_history,
)
from publish_instagram import ComposioError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Pinterest pins for all published Instagram posts.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--board-name", default=DEFAULT_BOARD_NAME)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Max posts to backfill (0 = all)")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    history = load_history(history_path)

    # Find published Instagram posts that don't have a pinterest_pin_id yet
    # AND have an image_url (so we can pin them)
    to_backfill = []
    for entry in history.get("publications", []):
        if entry.get("status") != "published":
            continue
        if not entry.get("media_id"):
            continue
        if entry.get("pinterest_pin_id"):
            continue  # already pinned
        if not entry.get("image_url"):
            # Try to find the image URL from the manifest file
            content_id = entry.get("id", "")
            manifest_path = history_path.parent / f"{content_id}.json"
            if manifest_path.exists():
                try:
                    with manifest_path.open() as fh:
                        manifest = json.load(fh)
                    if manifest.get("image_url"):
                        entry["_manifest"] = manifest
                        to_backfill.append(entry)
                        continue
                except (json.JSONDecodeError, OSError):
                    pass
            print(f"[WARN] {content_id}: no image_url in history or manifest. Skipping.")
            continue
        to_backfill.append(entry)

    print(f"[INFO] Found {len(to_backfill)} published posts to backfill to Pinterest.")
    if args.limit > 0:
        to_backfill = to_backfill[:args.limit]
        print(f"[INFO] Limited to first {len(to_backfill)} posts.")

    if not to_backfill:
        print("[INFO] Nothing to backfill. Done.")
        return 0

    if args.dry_run:
        print("[INFO] Dry-run mode. Would backfill:")
        for entry in to_backfill:
            print(f"  - {entry.get('id')}  topic={entry.get('topic','?')[:40]}")
        return 0

    config = load_pinterest_config()

    # Ensure the board exists
    try:
        board_id = find_or_create_board(config, args.board_name,
                                         "Practical web design tips for small business owners. "
                                         "Freelance web designer — email visualhookdesign@gmail.com")
    except ComposioError as exc:
        print(f"[ERROR] Board setup failed: {exc}", file=sys.stderr)
        return 1

    success = 0
    errors = 0
    for entry in to_backfill:
        content_id = entry.get("id", "?")
        image_url = entry.get("image_url")
        topic = entry.get("topic", "Web Design Tip")
        image_headline = entry.get("image_headline", topic)
        seed_fact = entry.get("seed_fact", "")

        # If we loaded a manifest, prefer its values
        manifest = entry.get("_manifest", {})
        if manifest:
            image_url = manifest.get("image_url", image_url)
            image_headline = manifest.get("image_headline", image_headline)
            seed_fact = manifest.get("seed_fact", seed_fact)

        if not image_url:
            print(f"[WARN] {content_id}: no image_url. Skipping.")
            errors += 1
            continue

        print(f"\n[INFO] Backfilling {content_id} -> Pinterest")
        print(f"  Topic: {topic}")
        print(f"  Headline: {image_headline}")
        print(f"  Image URL: {image_url}")

        # Generate Pinterest-optimized title + description
        pin_title, pin_description = generate_pinterest_title_and_description(topic, seed_fact, image_headline)
        fake_manifest = {
            "topic": topic,
            "seed_fact": seed_fact,
            "image_headline": image_headline,
        }
        if pin_title is None:
            pin_title = build_fallback_title(fake_manifest)
            print(f"  [INFO] Using fallback title: {pin_title[:60]}...")
        if pin_description is None:
            pin_description = build_fallback_description(fake_manifest)
            print(f"  [INFO] Using fallback description ({len(pin_description)} chars).")

        try:
            pin_id = create_pin(
                board_id=board_id,
                image_url=image_url,
                title=pin_title,
                description=pin_description,
                alt_text=pin_title,
                link=PORTFOLIO_LINK,  # always link to portfolio
                config=config,
            )
            print(f"  [OK] Pin published! Pin ID: {pin_id}")
            print(f"       Link: {PORTFOLIO_LINK}")
            update_history_with_pinterest(history_path, content_id, pin_id, board_id)
            success += 1
            # Be polite to the API
            time.sleep(3.0)
        except ComposioError as exc:
            print(f"  [FAIL] {exc}")
            errors += 1
            time.sleep(5.0)

    print(f"\n[INFO] Backfill complete. Success: {success}, Errors: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
