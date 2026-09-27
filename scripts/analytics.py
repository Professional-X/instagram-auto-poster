"""
analytics.py
-------------
Daily job that pulls Instagram insights for recently-published posts and
records follower growth. Data is appended to content/analytics.json so the
weekly report can aggregate it.

Two operations:
  1. POST INSIGHTS: For each post published 24-48h ago (that doesn't have
     insights yet), call Composio INSTAGRAM_GET_POST_INSIGHTS to fetch
     impressions, reach, saves, profile_visits, engagement. Record in
     analytics.json under post_insights[post_id].
  2. USER INSIGHTS: Call Composio INSTAGRAM_GET_USER_INFO to fetch current
     follower count, media count, etc. Record in analytics.json under
     follower_growth[date] so we can plot growth over time.

Usage:
  python scripts/analytics.py [--history content/history.json]
                                [--analytics content/analytics.json]
                                [--dry-run]
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

ACTION_GET_POST_INSIGHTS = "INSTAGRAM_GET_POST_INSIGHTS"
ACTION_GET_USER_INFO = "INSTAGRAM_GET_USER_INFO"


def load_analytics(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"post_insights": {}, "follower_growth": {}}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"post_insights": {}, "follower_growth": {}}
        data.setdefault("post_insights", {})
        data.setdefault("follower_growth", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"post_insights": {}, "follower_growth": {}}


def save_analytics(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def load_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"publications": []}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"publications": []}


def find_posts_needing_insights(history: dict[str, Any], analytics: dict[str, Any],
                                 min_age_hours: int = 24, max_age_hours: int = 168) -> list[dict[str, Any]]:
    """Find posts published between min_age_hours and max_age_hours ago that
    don't have insights recorded yet. (We wait 24h so IG has data; we cap at
    7 days because insights may expire.)
    """
    now = time.time()
    cutoff_min = now - max_age_hours * 3600
    cutoff_max = now - min_age_hours * 3600
    existing_insights = analytics.get("post_insights", {})
    posts = []
    for entry in history.get("publications", []):
        if entry.get("status") != "published" or not entry.get("media_id"):
            continue
        post_id = entry.get("id", "")
        if post_id in existing_insights:
            continue  # already have insights
        recorded_at = entry.get("recorded_at", "")
        try:
            ts = time.mktime(time.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ"))
            if cutoff_min <= ts <= cutoff_max:
                posts.append(entry)
        except (ValueError, TypeError):
            continue
    return posts


def fetch_post_insights(media_id: str, config: dict[str, str]) -> dict[str, Any]:
    """Fetch insights for a single post. Returns dict with metrics."""
    data = call_composio(
        ACTION_GET_POST_INSIGHTS,
        arguments={
            "ig_post_id": str(media_id),
            "metric": ["impressions", "reach", "saved", "profile_activity", "likes", "comments", "shares"],
        },
        config=config,
    )
    # IG Graph API returns: {data: [{name: 'impressions', values: [{value: N}]}, ...]}
    metrics: dict[str, Any] = {}
    raw_data = data.get("data") or []
    if isinstance(raw_data, list):
        for item in raw_data:
            name = item.get("name", "")
            values = item.get("values") or []
            if values and isinstance(values, list):
                metrics[name] = values[0].get("value", 0)
    return metrics


def fetch_user_info(config: dict[str, str]) -> dict[str, Any]:
    """Fetch current user info (follower count, media count, etc.)."""
    data = call_composio(
        ACTION_GET_USER_INFO,
        arguments={},
        config=config,
    )
    # IG Graph API returns: {id, username, followers_count, media_count, ...}
    # Composio wraps it — extract the fields we care about
    return {
        "followers_count": data.get("followers_count") or data.get("follower_count") or 0,
        "media_count": data.get("media_count") or 0,
        "username": data.get("username", ""),
        "ig_user_id": data.get("id", ""),
        "profile_picture_url": data.get("profile_picture_url", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Instagram insights for recent posts + follower growth.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--analytics", default="content/analytics.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    analytics_path = Path(args.analytics).resolve()

    print(f"[INFO] Analytics job starting (dry_run={args.dry_run})")

    history = load_history(history_path)
    analytics = load_analytics(analytics_path)

    posts_needing_insights = find_posts_needing_insights(history, analytics)
    print(f"[INFO] Found {len(posts_needing_insights)} posts needing insights (24h-7d old, no data yet).")

    if args.dry_run:
        print("[INFO] Dry-run mode: skipping Composio calls.")
        for p in posts_needing_insights:
            print(f"  Would fetch insights for {p.get('id')} (media_id={p.get('media_id')})")
        print("  Would fetch user info (follower count).")
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

    # 1. Fetch post insights
    insights_ok = 0
    insights_err = 0
    for post in posts_needing_insights:
        post_id = post.get("id", "?")
        media_id = post.get("media_id")
        if not media_id:
            continue
        print(f"[INFO] Fetching insights for {post_id} (media_id={media_id})...")
        try:
            metrics = fetch_post_insights(str(media_id), config)
            analytics["post_insights"][post_id] = {
                "media_id": str(media_id),
                "topic": post.get("topic", ""),
                "image_style": post.get("image_style", ""),
                "image_headline": post.get("image_headline", ""),
                "hashtags": post.get("hashtags", []),
                "slot": post.get("slot"),
                "published_at": post.get("recorded_at", ""),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "metrics": metrics,
            }
            insights_ok += 1
            print(f"[INFO] Insights for {post_id}: {metrics}")
            time.sleep(1.0)  # be polite
        except ComposioError as exc:
            print(f"[WARN] Failed to fetch insights for {post_id}: {exc}")
            insights_err += 1
            time.sleep(2.0)

    # 2. Fetch user info (follower growth)
    print("[INFO] Fetching current user info (follower count)...")
    try:
        user_info = fetch_user_info(config)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        analytics["follower_growth"][today] = {
            "followers": user_info["followers_count"],
            "media_count": user_info["media_count"],
            "username": user_info["username"],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        print(f"[INFO] Follower count today: {user_info['followers_count']}")
    except ComposioError as exc:
        print(f"[WARN] Failed to fetch user info: {exc}")

    save_analytics(analytics_path, analytics)
    print(f"[INFO] Analytics saved. Post insights: +{insights_ok} OK, {insights_err} errors.")
    # Exit 0 even if some posts failed — partial data is still useful
    return 0


if __name__ == "__main__":
    sys.exit(main())
