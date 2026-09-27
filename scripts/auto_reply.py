"""
auto_reply.py
-------------
Hourly job that polls comments on recently-published Instagram posts and
auto-replies with sentiment-aware, DM-inviting messages. Also:
  - Sends a private DM to each commenter (comment-to-DM handoff)
  - Detects buying-intent comments and creates GitHub Issues as lead alerts
  - Uses Groq to classify comment sentiment (question / compliment /
    buying-intent / complaint) and picks the right reply tone

Flow:
  1. Read content/history.json -> find posts published in last 48h with media_id
  2. Read content/replied_comments.json -> set of comment IDs already handled
  3. For each post:
     a. Call Composio INSTAGRAM_GET_POST_COMMENTS
     b. For each new comment:
        - Classify sentiment (Groq if available, else keyword-based fallback)
        - Pick reply template based on sentiment
        - Call INSTAGRAM_REPLY_TO_COMMENT (public reply)
        - Call INSTAGRAM_SEND_TEXT_MESSAGE (private DM handoff)
        - If buying-intent: create GitHub Issue as lead alert
        - Record comment_id in replied_comments.json
  4. Save replied_comments.json

Idempotent: never replies/DMs the same comment twice.

Usage:
  python scripts/auto_reply.py [--history content/history.json]
                                 [--replied content/replied_comments.json]
                                 [--hours 48] [--dry-run]
                                 [--no-dm]  (skip comment-to-DM handoff)
                                 [--no-lead-alerts]  (skip GitHub Issue creation)
"""

from __future__ import annotations

import argparse
import hashlib
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
# Sentiment-specific reply templates
# ---------------------------------------------------------------------------

REPLY_TEMPLATES: dict[str, list[str]] = {
    "buying_intent": [
        "Great question! Just sent you a DM with details — check your inbox.",
        "Thanks for asking! I've DM'd you the info. Let's talk!",
        "Sent you a DM! Check your messages so we can discuss your project.",
        "Awesome — I just DM'd you the next steps. Talk soon!",
    ],
    "question": [
        "Great question! DM us and we'll walk you through it.",
        "Good ask — send us a DM and we'll explain in detail.",
        "Happy to help with that! DM us so we can give you a proper answer.",
        "Let's take this to DMs so we can go deeper — send us a message!",
    ],
    "compliment": [
        "Thanks so much! DM us if you need a website like this for your business.",
        "Appreciate that! Drop us a DM if you ever need web design help.",
        "Glad you liked it! DM us to chat about your own website project.",
        "Thanks! We'd love to help your business too — DM us anytime.",
    ],
    "complaint": [
        "Sorry to hear that — please DM us so we can make it right.",
        "We want to fix this for you. Send us a DM with the details.",
        "Apologies for the trouble. DM us so we can resolve this properly.",
        "Noted — please DM us so we can address your concern directly.",
    ],
    "generic": [
        "Thanks for the comment! DM us to discuss your website project.",
        "Appreciate you reaching out! Send us a DM and we'll talk about your site.",
        "Thanks! Want a website like this for your business? DM us to chat.",
        "Glad you liked it! Drop us a DM if you need a website that converts.",
    ],
}

# DM handoff messages (sent privately to the commenter)
DM_TEMPLATES: dict[str, list[str]] = {
    "buying_intent": [
        "Hey! Thanks for your comment on my post. I'd love to help you with your website. "
        "To give you an accurate quote, can you tell me: (1) what's your current website "
        "(if any)? (2) what's your budget range? (3) when do you need it ready? "
        "Looking forward to hearing from you!",
    ],
    "question": [
        "Hey! Thanks for engaging with my post. I saw your question and wanted to "
        "answer you personally. What would you like to know about websites or web "
        "design? I'm happy to help — no obligation. Just reply here and we'll chat.",
    ],
    "compliment": [
        "Hey! Thanks for the kind words on my post. If you ever need a website "
        "for your business — or just want to nerd out about web design — feel "
        "free to reach out. I work with small businesses like yours all the time. "
        "No pressure, just here to help!",
    ],
    "complaint": [
        "Hey, I saw your comment and I want to make things right. Can you tell "
        "me more about what went wrong? I take feedback seriously and want to "
        "fix this for you. Please reply with the details.",
    ],
    "generic": [
        "Hey! Thanks for commenting on my post. I'm a freelance web designer "
        "helping small businesses get websites that actually bring in clients. "
        "If you ever need help with your site — or just have questions — feel "
        "free to ask. Happy to help!",
    ],
}


def pick_reply(sentiment: str, comment_id: str) -> str:
    templates = REPLY_TEMPLATES.get(sentiment, REPLY_TEMPLATES["generic"])
    h = int(hashlib.sha256(comment_id.encode()).hexdigest(), 16)
    return templates[h % len(templates)]


def pick_dm(sentiment: str, comment_id: str) -> str:
    templates = DM_TEMPLATES.get(sentiment, DM_TEMPLATES["generic"])
    h = int(hashlib.sha256((comment_id + "dm").encode()).hexdigest(), 16)
    return templates[h % len(templates)]


# ---------------------------------------------------------------------------
# Keyword-based sentiment classification (fallback when Groq is unavailable)
# ---------------------------------------------------------------------------

BUYING_INTENT_KEYWORDS = [
    "how much", "price", "cost", "quote", "pricing", "rate", "fee", "charge",
    "budget", "afford", "cheap", "expensive", "deal", "discount", "package",
    "hire", "available", "booking", "book you", "work with", "project",
    "need a website", "want a website", "looking for", "interested",
    "dm me", "message me", "contact", "reach you", "email",
    "when can", "how soon", "timeline", "deadline",
]

QUESTION_KEYWORDS = ["?", "how", "what", "why", "when", "where", "which", "can you", "do you", "are you"]

COMPLAINT_KEYWORDS = ["bad", "terrible", "awful", "hate", "worst", "scam", "rip off", "ripoff",
                       "broken", "doesn't work", "didnt work", "failed", "error", "wrong",
                       "disappointed", "unhappy", "frustrated", "angry", "refund"]

COMPLIMENT_KEYWORDS = ["great", "awesome", "love", "amazing", "beautiful", "nice", "cool",
                        "good", "excellent", "perfect", "wow", "fantastic", "brilliant",
                        "helpful", "useful", "thanks", "thank you", "appreciate"]


def classify_sentiment_keywords(text: str) -> str:
    text_lower = text.lower()
    for kw in BUYING_INTENT_KEYWORDS:
        if kw in text_lower:
            return "buying_intent"
    for kw in COMPLAINT_KEYWORDS:
        if kw in text_lower:
            return "complaint"
    for kw in QUESTION_KEYWORDS:
        if kw in text_lower:
            return "question"
    for kw in COMPLIMENT_KEYWORDS:
        if kw in text_lower:
            return "compliment"
    return "generic"


def classify_sentiment_groq(text: str) -> str | None:
    """Use Groq to classify comment sentiment. Returns one of:
    buying_intent, question, compliment, complaint, generic. Or None on failure.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        return None
    model = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You classify Instagram comments for a freelance web designer's account. Return ONE word: buying_intent, question, compliment, complaint, or generic."},
            {"role": "user", "content": f"Classify this comment (reply with ONE word only, no explanation):\n\n{text}"},
        ],
        "max_tokens": 20,
        "temperature": 0,
    }
    try:
        resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                             headers=headers, json=body, timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"].strip().lower()
        # Strip reasoning blocks if present
        content = re.sub(r"<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL).strip()
        content = re.sub(r"<reasoning>.*$", "", content, flags=re.DOTALL).strip()
        # Take first word
        first_word = content.split()[0] if content else "generic"
        valid = {"buying_intent", "question", "compliment", "complaint", "generic"}
        if first_word in valid:
            return first_word
        # Partial match
        for v in valid:
            if v in first_word:
                return v
    except (ValueError, KeyError, IndexError):
        pass
    return None


def classify_sentiment(text: str) -> tuple[str, str]:
    """Returns (sentiment, method_used). Tries Groq first, falls back to keywords."""
    result = classify_sentiment_groq(text)
    if result:
        return result, "groq"
    return classify_sentiment_keywords(text), "keywords"


# ---------------------------------------------------------------------------
# GitHub Issue creation for lead alerts
# ---------------------------------------------------------------------------

def create_lead_alert_issue(comment: dict[str, Any], post: dict[str, Any],
                             sentiment: str, reply_sent: str) -> bool:
    """Create a GitHub Issue as a lead alert. Returns True on success.
    Uses GH_PAT env var (set in workflow) + GitHub Issues API.
    """
    pat = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  # format: owner/repo
    if not pat or not repo:
        print("[INFO] No GH_PAT/GITHUB_REPOSITORY set — skipping lead alert Issue creation.")
        return False

    comment_id = str(comment.get("id", "?"))
    comment_text = comment.get("text", "")
    comment_user = comment.get("username") or comment.get("from", {}).get("username", "?")
    post_id = post.get("id", "?")
    media_id = post.get("media_id", "?")
    topic = post.get("topic", "?")

    title = f"🔥 Hot lead: @{comment_user} asked about pricing on '{topic}'"
    body = f"""## 🔥 Buying-Intent Lead Alert

A comment with buying intent was detected on your Instagram post. Follow up ASAP!

### Comment details
- **User:** @{comment_user}
- **Comment:** "{comment_text}"
- **Comment ID:** `{comment_id}`
- **Sentiment:** `{sentiment}`

### Post details
- **Post ID:** `{post_id}`
- **Topic:** {topic}
- **Media ID:** `{media_id}`
- **Published:** {post.get('recorded_at', '?')}

### Auto-reply sent
> {reply_sent}

### Action needed
- [ ] Check your Instagram DMs — the system also sent a private DM with intake questions
- [ ] Reply personally within 2 hours (leads go cold fast)
- [ ] Close this issue when you've made contact

### Link
- View on Instagram: https://www.instagram.com/

---
*This issue was auto-created by the Instagram auto-reply system.*
"""
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"title": title, "body": body, "labels": ["lead", "auto-generated"]}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 201:
            issue_url = resp.json().get("html_url", "?")
            print(f"[INFO] 🎉 Lead alert Issue created: {issue_url}")
            return True
        else:
            print(f"[WARN] GitHub Issue creation failed: HTTP {resp.status_code}")
            return False
    except requests.RequestException as exc:
        print(f"[WARN] GitHub Issue creation network error: {exc}")
        return False


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
    cutoff = time.time() - hours * 3600
    posts = []
    for entry in history.get("publications", []):
        if entry.get("status") != "published":
            continue
        if not entry.get("media_id"):
            continue
        recorded_at = entry.get("recorded_at", "")
        try:
            ts = time.mktime(time.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ"))
            if ts >= cutoff:
                posts.append(entry)
        except (ValueError, TypeError):
            posts.append(entry)
    return posts


# ---------------------------------------------------------------------------
# Composio comment operations
# ---------------------------------------------------------------------------

ACTION_GET_COMMENTS = "INSTAGRAM_GET_POST_COMMENTS"
ACTION_REPLY_TO_COMMENT = "INSTAGRAM_REPLY_TO_COMMENT"
ACTION_SEND_TEXT_MESSAGE = "INSTAGRAM_SEND_TEXT_MESSAGE"


def get_post_comments(media_id: str, config: dict[str, str], limit: int = 50) -> list[dict[str, Any]]:
    data = call_composio(
        ACTION_GET_COMMENTS,
        arguments={"ig_post_id": str(media_id), "limit": limit},
        config=config,
    )
    comments = data.get("data") or []
    if not isinstance(comments, list):
        return []
    return comments


def reply_to_comment(comment_id: str, message: str, config: dict[str, str]) -> dict[str, Any]:
    return call_composio(
        ACTION_REPLY_TO_COMMENT,
        arguments={"ig_comment_id": str(comment_id), "message": message},
        config=config,
    )


def send_dm(recipient_id: str, message: str, config: dict[str, str]) -> dict[str, Any]:
    """Send a private DM to a user. recipient_id is the Instagram-scoped PSID
    (available from the comment's 'from.id' field).
    """
    return call_composio(
        ACTION_SEND_TEXT_MESSAGE,
        arguments={"recipient_id": str(recipient_id), "text": message},
        config=config,
    )


def extract_recipient_id(comment: dict[str, Any]) -> str | None:
    """Extract the Instagram-scoped PSID from a comment object.
    IG Graph API comment structure: {from: {id, username}, ...}
    """
    from_obj = comment.get("from") or {}
    return from_obj.get("id") or comment.get("from_id")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-reply to IG comments with sentiment routing + DM handoff + lead alerts.")
    parser.add_argument("--history", default="content/history.json")
    parser.add_argument("--replied", default="content/replied_comments.json")
    parser.add_argument("--hours", type=int, default=48)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-dm", action="store_true", help="Skip comment-to-DM handoff")
    parser.add_argument("--no-lead-alerts", action="store_true", help="Skip GitHub Issue creation for buying-intent leads")
    args = parser.parse_args()

    history_path = Path(args.history).resolve()
    replied_path = Path(args.replied).resolve()

    print(f"[INFO] Auto-reply job starting (last {args.hours}h, dry_run={args.dry_run}, "
          f"dm_handoff={not args.no_dm}, lead_alerts={not args.no_lead_alerts})")

    history = load_history(history_path)
    recent_posts = find_recent_posts(history, hours=args.hours)
    print(f"[INFO] Found {len(recent_posts)} recently-published posts to scan.")

    if not recent_posts:
        print("[INFO] No posts to scan. Done.")
        replied_data = load_replied(replied_path)
        replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_replied(replied_path, replied_data)
        return 0

    replied_data = load_replied(replied_path)
    replied_map: dict[str, Any] = replied_data.setdefault("replied", {})

    if args.dry_run:
        print("[INFO] Dry-run mode: skipping Composio calls.")
        for post in recent_posts:
            print(f"  Would scan post {post.get('id')} (media_id={post.get('media_id')})")
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

    total_replied = 0
    total_dms = 0
    total_leads = 0
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
            if comment_id in replied_map:
                total_skipped += 1
                continue

            comment_text = comment.get("text") or ""
            comment_user = comment.get("username") or comment.get("from", {}).get("username", "?")

            # Classify sentiment
            sentiment, method = classify_sentiment(comment_text)
            print(f"[INFO] Comment {comment_id} (user={comment_user}, sentiment={sentiment} via {method}): "
                  f"text='{comment_text[:60]}'")

            reply_msg = pick_reply(sentiment, comment_id)

            # 1. Public reply
            try:
                reply_to_comment(comment_id, reply_msg, config)
                total_replied += 1
                print(f"[INFO]   ↳ Public reply sent: \"{reply_msg[:60]}...\"")
            except ComposioError as exc:
                print(f"[WARN]   ↳ Public reply failed: {exc}")
                total_errors += 1
                time.sleep(2.0)
                # Still mark as replied so we don't retry endlessly
                replied_map[comment_id] = {
                    "post_id": post_id, "media_id": media_id,
                    "comment_text": comment_text[:80], "comment_user": comment_user,
                    "sentiment": sentiment, "reply": reply_msg, "dm_sent": False,
                    "error": str(exc)[:200],
                    "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                continue

            time.sleep(1.0)

            # 2. Comment-to-DM handoff (private message with stronger CTA)
            dm_sent = False
            if not args.no_dm:
                recipient_id = extract_recipient_id(comment)
                if recipient_id:
                    dm_msg = pick_dm(sentiment, comment_id)
                    try:
                        send_dm(recipient_id, dm_msg, config)
                        dm_sent = True
                        total_dms += 1
                        print(f"[INFO]   ↳ DM sent to {comment_user} (psid={recipient_id})")
                    except ComposioError as exc:
                        print(f"[WARN]   ↳ DM failed: {exc}")
                        # Non-fatal — public reply already went out
                    time.sleep(1.0)

            # 3. Lead alert: create GitHub Issue for buying-intent comments
            lead_issue_url = None
            if sentiment == "buying_intent" and not args.no_lead_alerts:
                issue_created = create_lead_alert_issue(comment, post, sentiment, reply_msg)
                if issue_created:
                    total_leads += 1

            # Record in replied_comments.json
            replied_map[comment_id] = {
                "post_id": post_id,
                "media_id": media_id,
                "comment_text": comment_text[:80],
                "comment_user": comment_user,
                "sentiment": sentiment,
                "sentiment_method": method,
                "reply": reply_msg,
                "dm_sent": dm_sent,
                "lead_alert": sentiment == "buying_intent",
                "replied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

    replied_data["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_replied(replied_path, replied_data)

    print(f"[INFO] Done. Replies: {total_replied}, DMs: {total_dms}, "
          f"Leads: {total_leads}, Skipped: {total_skipped}, Errors: {total_errors}")

    successful_posts = len(recent_posts) - total_errors
    if total_errors > 0 and successful_posts == 0:
        print(f"[ERROR] All {total_errors} posts failed. Exiting 1.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
