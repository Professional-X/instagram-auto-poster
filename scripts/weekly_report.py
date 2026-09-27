"""
weekly_report.py
-----------------
Weekly job (runs every Sunday 9pm IST) that generates a markdown performance
report and commits it to reports/<year>-W<week>.md.

The report includes:
  - Posts published this week (count + topics breakdown)
  - Total impressions, reach, saves, profile visits across all posts
  - Top 3 posts by engagement (saves + shares + profile visits)
  - Top topic by total reach
  - Top image style by total reach
  - Top time slot by total reach
  - Hashtag performance ranking (which hashtags correlate with high impressions)
  - Comments received + auto-replies sent + DMs sent + leads detected
  - Follower growth (start of week vs end of week)
  - Groq-generated 2-paragraph "what worked this week" summary

Usage:
  python scripts/weekly_report.py [--history content/history.json]
                                    [--analytics content/analytics.json]
                                    [--replied content/replied_comments.json]
                                    [--dm-replied content/dm_replied.json]
                                    [--reports-dir reports]
                                    [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def get_week_range() -> tuple[dt.date, dt.date, str]:
    """Return (monday, sunday, week_label) for the current week.
    week_label format: '2026-W38'
    """
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    sunday = monday + dt.timedelta(days=6)
    iso_year, iso_week, _ = monday.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"
    return monday, sunday, week_label


def posts_this_week(history: dict[str, Any], monday: dt.date, sunday: dt.date) -> list[dict[str, Any]]:
    """Return posts published between monday and sunday (inclusive)."""
    posts = []
    for entry in history.get("publications", []):
        if entry.get("status") != "published":
            continue
        recorded_at = entry.get("recorded_at", "")
        try:
            ts = time.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ")
            post_date = dt.date(ts.tm_year, ts.tm_mon, ts.tm_mday)
            if monday <= post_date <= sunday:
                posts.append(entry)
        except (ValueError, TypeError):
            continue
    return posts


def aggregate_metrics(posts: list[dict[str, Any]], analytics: dict[str, Any]) -> dict[str, Any]:
    """Aggregate insights for the given posts from analytics.json."""
    post_insights = analytics.get("post_insights", {})
    total = {"impressions": 0, "reach": 0, "saved": 0, "profile_activity": 0,
             "likes": 0, "comments": 0, "shares": 0}
    per_post: list[dict[str, Any]] = []

    for post in posts:
        post_id = post.get("id", "")
        insights = post_insights.get(post_id, {}).get("metrics", {})
        metrics = {
            "post_id": post_id,
            "topic": post.get("topic", ""),
            "image_style": post.get("image_style", ""),
            "slot": post.get("slot"),
            "hashtags": post.get("hashtags", []),
            "image_headline": post.get("image_headline", ""),
            "impressions": insights.get("impressions", 0),
            "reach": insights.get("reach", 0),
            "saved": insights.get("saved", 0),
            "profile_activity": insights.get("profile_activity", 0),
            "likes": insights.get("likes", 0),
            "comments": insights.get("comments", 0),
            "shares": insights.get("shares", 0),
        }
        for k, v in metrics.items():
            if isinstance(v, int) and k in total:
                total[k] += v
        per_post.append(metrics)

    return {"total": total, "per_post": per_post}


def rank_hashtags(per_post: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """Rank hashtags by total impressions. Returns list of (hashtag, total_impressions, post_count)."""
    hashtag_impressions: dict[str, list[int]] = defaultdict(list)
    for post in per_post:
        for tag in post.get("hashtags", []):
            hashtag_impressions[tag].append(post.get("impressions", 0))
    ranked = []
    for tag, impressions_list in hashtag_impressions.items():
        total_imp = sum(impressions_list)
        post_count = len(impressions_list)
        ranked.append((tag, total_imp, post_count))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def rank_by_field(per_post: list[dict[str, Any]], field: str) -> list[tuple[str, int, int]]:
    """Rank posts by a field (topic, image_style, slot). Returns [(value, total_reach, post_count)]."""
    field_reach: dict[str, list[int]] = defaultdict(list)
    for post in per_post:
        val = str(post.get(field, "unknown"))
        field_reach[val].append(post.get("reach", 0))
    ranked = []
    for val, reach_list in field_reach.items():
        ranked.append((val, sum(reach_list), len(reach_list)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def follower_growth_this_week(analytics: dict[str, Any], monday: dt.date, sunday: dt.date) -> dict[str, Any]:
    """Find follower count at start and end of week."""
    growth = analytics.get("follower_growth", {})
    # Find the entry on or just before monday, and on or just before sunday
    start_count = None
    end_count = None
    start_date = None
    end_date = None
    for date_str, info in sorted(growth.items()):
        try:
            d = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if d <= monday:
            start_count = info.get("followers", 0)
            start_date = date_str
        if d <= sunday:
            end_count = info.get("followers", 0)
            end_date = date_str
    return {
        "start_count": start_count,
        "end_count": end_count,
        "start_date": start_date,
        "end_date": end_date,
        "delta": (end_count - start_count) if start_count and end_count else None,
    }


def generate_groq_summary(per_post: list[dict[str, Any]], total: dict[str, int],
                           growth: dict[str, Any]) -> str:
    """Use Groq to generate a 2-paragraph 'what worked this week' summary."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        return ""
    model = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"

    # Build a compact data summary for Groq
    top_posts = sorted(per_post, key=lambda p: p.get("saved", 0) + p.get("shares", 0), reverse=True)[:3]
    data_summary = f"""Posts this week: {len(per_post)}
Total impressions: {total.get('impressions', 0)}
Total reach: {total.get('reach', 0)}
Total saves: {total.get('saved', 0)}
Total profile visits: {total.get('profile_activity', 0)}
Follower growth: {growth.get('delta', 'N/A')} new followers

Top 3 posts (by saves + shares):
"""
    for i, p in enumerate(top_posts, 1):
        data_summary += f"  {i}. Topic='{p.get('topic','?')}' Style='{p.get('image_style','?')}' Slot={p.get('slot','?')} Saves={p.get('saved',0)} Reach={p.get('reach',0)} Headline='{p.get('image_headline','?')[:60]}'\n"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You write concise, insightful weekly performance summaries for a freelance web designer's Instagram account. 2 paragraphs, plain text, no markdown, no emojis. First paragraph: what worked. Second paragraph: what to try next week."},
            {"role": "user", "content": f"Write a 2-paragraph weekly summary based on this data:\n\n{data_summary}"},
        ],
        "max_tokens": 500,
        "temperature": 0.7,
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                             headers=headers, json=body, timeout=30)
    except requests.RequestException:
        return ""
    if resp.status_code != 200:
        return ""
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL).strip()
        content = re.sub(r"<reasoning>.*$", "", content, flags=re.DOTALL).strip()
        return content.strip()
    except (ValueError, KeyError, IndexError):
        return ""


def render_report(week_label: str, monday: dt.date, sunday: dt.date,
                  posts: list[dict[str, Any]], agg: dict[str, Any],
                  hashtag_ranking: list[tuple[str, int, int]],
                  topic_ranking: list[tuple[str, int, int]],
                  style_ranking: list[tuple[str, int, int]],
                  slot_ranking: list[tuple[str, int, int]],
                  growth: dict[str, Any],
                  replied_data: dict[str, Any],
                  dm_data: dict[str, Any],
                  groq_summary: str) -> str:
    """Render the full markdown report."""
    total = agg["total"]
    per_post = agg["per_post"]

    # Count replies/DMs/leads from replied_comments.json
    replied_map = replied_data.get("replied", {})
    replies_sent = sum(1 for v in replied_map.values() if v.get("reply"))
    dms_sent = sum(1 for v in replied_map.values() if v.get("dm_sent"))
    leads = sum(1 for v in replied_map.values() if v.get("lead_alert"))

    dm_replied_map = dm_data.get("replied", {})
    dm_intakes = sum(1 for v in dm_replied_map.values() if v.get("status") == "intake_sent")

    # Top 3 posts by engagement (saves + shares + profile visits)
    top3 = sorted(per_post, key=lambda p: p.get("saved", 0) + p.get("shares", 0) + p.get("profile_activity", 0), reverse=True)[:3]

    lines = [
        f"# Instagram Weekly Report — {week_label}",
        f"**Period:** {monday.isoformat()} to {sunday.isoformat()}",
        "",
        "## 📊 Headline Metrics",
        f"- **Posts published:** {len(posts)}",
        f"- **Total impressions:** {total['impressions']:,}",
        f"- **Total reach:** {total['reach']:,}",
        f"- **Total saves:** {total['saved']:,}",
        f"- **Total profile visits:** {total['profile_activity']:,}",
        f"- **Total likes:** {total['likes']:,}",
        f"- **Total comments:** {total['comments']:,}",
        f"- **Total shares:** {total['shares']:,}",
        "",
        "## 📈 Follower Growth",
    ]
    if growth["delta"] is not None:
        lines.append(f"- **Start of week:** {growth['start_count']:,} followers ({growth['start_date']})")
        lines.append(f"- **End of week:** {growth['end_count']:,} followers ({growth['end_date']})")
        delta = growth["delta"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"- **Net growth:** {sign}{delta:,} followers")
    else:
        lines.append("- Follower growth data not available for this week.")

    lines.extend([
        "",
        "## 🏆 Top 3 Posts This Week (by engagement)",
    ])
    if top3:
        for i, p in enumerate(top3, 1):
            eng = p.get("saved", 0) + p.get("shares", 0) + p.get("profile_activity", 0)
            lines.append(f"### {i}. {p.get('topic', '?')} (Slot {p.get('slot', '?')}) — {eng:,} engagement")
            lines.append(f"- **Headline:** {p.get('image_headline', '?')}")
            lines.append(f"- **Style:** {p.get('image_style', '?')}")
            lines.append(f"- **Hashtags:** {' '.join(p.get('hashtags', []))}")
            lines.append(f"- **Impressions:** {p.get('impressions', 0):,} | **Reach:** {p.get('reach', 0):,} | **Saves:** {p.get('saved', 0):,} | **Profile visits:** {p.get('profile_activity', 0):,}")
            lines.append("")
    else:
        lines.append("_No posts with insights data yet._")
        lines.append("")

    lines.extend([
        "## 🎯 Topic Performance (by total reach)",
        "| Topic | Total Reach | Posts |",
        "|-------|------------|-------|",
    ])
    for topic, reach, count in topic_ranking[:7]:
        lines.append(f"| {topic} | {reach:,} | {count} |")

    lines.extend([
        "",
        "## 🎨 Image Style Performance (by total reach)",
        "| Style | Total Reach | Posts |",
        "|-------|------------|-------|",
    ])
    for style, reach, count in style_ranking[:6]:
        lines.append(f"| {style} | {reach:,} | {count} |")

    lines.extend([
        "",
        "## ⏰ Time Slot Performance (by total reach)",
        "| Slot | Total Reach | Posts |",
        "|------|------------|-------|",
    ])
    slot_names = {"1": "Morning (08:00 IST)", "2": "Lunch (12:30 IST)", "3": "Evening (18:00 IST)", "4": "Night (20:30 IST)"}
    for slot, reach, count in slot_ranking[:4]:
        name = slot_names.get(slot, slot)
        lines.append(f"| {name} | {reach:,} | {count} |")

    lines.extend([
        "",
        "## #️⃣ Hashtag Performance (by total impressions)",
        "| Hashtag | Total Impressions | Posts Using It |",
        "|---------|-------------------|----------------|",
    ])
    for tag, imp, count in hashtag_ranking[:15]:
        lines.append(f"| {tag} | {imp:,} | {count} |")

    lines.extend([
        "",
        "## 💬 Engagement Summary",
        f"- **Public replies sent:** {replies_sent}",
        f"- **DMs sent (comment-to-DM handoff):** {dms_sent}",
        f"- **DM intake messages sent:** {dm_intakes}",
        f"- **Hot leads detected (buying intent):** {leads}",
        "",
    ])

    if groq_summary:
        lines.extend([
            "## 🤖 AI Weekly Summary",
            groq_summary,
            "",
        ])

    lines.extend([
        "---",
        f"*Auto-generated by `scripts/weekly_report.py` on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}*",
    ])

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate weekly Instagram performance report.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--analytics", default="content/analytics.json")
    parser.add_argument("--replied", default="content/replied_comments.json")
    parser.add_argument("--dm-replied", default="content/dm_replied.json")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    history = load_json(Path(args.history))
    analytics = load_json(Path(args.analytics))
    replied_data = load_json(Path(args.replied))
    dm_data = load_json(Path(args.dm_replied))

    monday, sunday, week_label = get_week_range()
    print(f"[INFO] Generating report for {week_label} ({monday} to {sunday})")

    posts = posts_this_week(history, monday, sunday)
    print(f"[INFO] Found {len(posts)} posts published this week.")

    if not posts:
        print("[INFO] No posts this week. Skipping report.")
        return 0

    agg = aggregate_metrics(posts, analytics)
    print(f"[INFO] Total impressions: {agg['total']['impressions']:,}")
    print(f"[INFO] Total reach: {agg['total']['reach']:,}")

    hashtag_ranking = rank_hashtags(agg["per_post"])
    topic_ranking = rank_by_field(agg["per_post"], "topic")
    style_ranking = rank_by_field(agg["per_post"], "image_style")
    slot_ranking = rank_by_field(agg["per_post"], "slot")
    growth = follower_growth_this_week(analytics, monday, sunday)

    print("[INFO] Generating AI summary with Groq...")
    groq_summary = generate_groq_summary(agg["per_post"], agg["total"], growth)

    report_md = render_report(
        week_label, monday, sunday, posts, agg,
        hashtag_ranking, topic_ranking, style_ranking, slot_ranking,
        growth, replied_data, dm_data, groq_summary
    )

    if args.dry_run:
        print("[INFO] Dry-run mode. Report preview:")
        print(report_md[:2000] + ("\n...<truncated>" if len(report_md) > 2000 else ""))
        return 0

    reports_dir = Path(args.reports_dir).resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{week_label}.md"
    with report_path.open("w", encoding="utf-8") as fh:
        fh.write(report_md)
    print(f"[INFO] Report written: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
