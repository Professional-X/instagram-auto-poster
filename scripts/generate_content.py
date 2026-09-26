"""
generate_content.py
-------------------
Deterministic + AI-assisted daily content generator for a web design agency's
Instagram account.

Pipeline:
  1. Pick today's topic from config/topics.yaml (rotates by day-of-year).
  2. Pick a seed fact for that topic (rotates by SHA-256 of date).
  3. If GROQ_API_KEY is set:
       - Call Groq (OpenAI-compatible /openai/v1/chat/completions endpoint)
       - Get back a JSON object: { caption, hashtags, image_headline }
       - On any error (403, network, JSON parse) → fall back to deterministic
         content built from the seed fact.
     If GROQ_API_KEY is NOT set:
       - Use the seed fact verbatim as image_headline and build a default caption.
  4. Render a 1080x1080 PNG (gradient + topic title + image_headline).
  5. Write content/<date>.json manifest.

Idempotency:
  - If content/<date>.json already exists AND has ai_generated=true, the script
    reuses it (skips the Groq call) — re-running the workflow for the same date
    won't burn Groq credits or produce a different post.

Usage:
  GROQ_API_KEY=... python scripts/generate_content.py [--date YYYY-MM-DD] [--out-dir content]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError as exc:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore
    _PILLOW_ERR = str(exc)
else:
    _PILLOW_ERR = None


# ---------------------------------------------------------------------------
# Topic rotation
# ---------------------------------------------------------------------------

DEFAULT_TOPICS: list[dict[str, Any]] = [
    {
        "title": "UX Principle",
        "facts": [
            "Users scan pages in an F-pattern, not read word-by-word — design for scanning, not reading.",
            "Hick's Law: more choices = slower decisions. Limit navigation to 5-7 top-level items.",
            "The 3-click rule is a myth, but the principle holds: don't make users hunt for the next step.",
            "Average attention span on a homepage is 5-8 seconds. Your hero must communicate value instantly.",
            "Forms with fewer fields convert better. Remove every field that isn't strictly required.",
            "Error messages should explain what went wrong and how to fix it — never just say 'Invalid input'.",
        ],
        "hashtags": ["#uxdesign", "#userexperience", "#webdesign"],
    },
    {
        "title": "Conversion Tip",
        "facts": [
            "A single, prominent CTA button outperforms multiple competing CTAs by up to 371%.",
            "Page load time under 2 seconds lifts conversion rates by 15-20% on mobile.",
            "Trust signals near the CTA reduce bounce and lift form completion.",
            "Above-the-fold content drives 80% of first impressions — don't waste it on carousels.",
            "Whitespace around a CTA increases click-through rate by roughly 20%.",
            "Social proof above the fold increases form submissions by 15-30%.",
        ],
        "hashtags": ["#conversion", "#cro", "#webdesign"],
    },
]


def load_topics() -> list[dict[str, Any]]:
    topics_path = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
    if not topics_path.exists():
        return DEFAULT_TOPICS
    try:
        import yaml  # type: ignore
    except ImportError:
        print("[INFO] config/topics.yaml exists but PyYAML is not installed; using built-in topics")
        return DEFAULT_TOPICS
    with topics_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not data or "topics" not in data:
        print("[WARN] config/topics.yaml has no 'topics' key; using built-in topics")
        return DEFAULT_TOPICS
    return data["topics"]


def pick_topic_and_fact(date: dt.date) -> tuple[dict[str, Any], str, int]:
    topics = load_topics()
    if not topics:
        raise RuntimeError("No topics available")
    day_of_year = date.timetuple().tm_yday
    topic_index = day_of_year % len(topics)
    topic = topics[topic_index]
    date_hash = int(hashlib.sha256(date.isoformat().encode()).hexdigest(), 16)
    facts = topic.get("facts") or []
    if not facts:
        raise RuntimeError(f"Topic '{topic.get('title')}' has no facts")
    fact_index = date_hash % len(facts)
    return topic, facts[fact_index], fact_index


# ---------------------------------------------------------------------------
# Groq AI integration (OpenAI-compatible /openai/v1/chat/completions endpoint)
# ---------------------------------------------------------------------------

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
# Default model — verified available on the user's Groq account Sept 26, 2026.
# Groq has fully migrated to gpt-oss + qwen + llama-prompt-guard lineups;
# legacy llama-3.x models are deprecated.
# Override with the GROQ_MODEL env var if you want a different model.
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GROQ_TIMEOUT_SECONDS = 45  # gpt-oss-120b is larger; allow more time


def _list_groq_models(api_key: str) -> list[str] | None:
    """Call GET /v1/models. Returns sorted list of model IDs, or None on error.

    Used for diagnostics when a chat completion returns 404 (model not found).
    """
    try:
        resp = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        return sorted(m["id"] for m in data.get("data", []) if "id" in m)
    except (ValueError, KeyError):
        return None


def generate_with_groq(topic_title: str, seed_fact: str) -> dict[str, Any] | None:
    """Call Groq to expand a seed fact into an Instagram-ready package.

    Returns { caption, hashtags, image_headline } on success, or None on any
    failure (network, 4xx/5xx, JSON parse). Caller must fall back gracefully.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        return None

    # Allow override; fall back to default
    model = os.environ.get("GROQ_MODEL") or GROQ_DEFAULT_MODEL

    system_prompt = (
        "You are the social media manager for a freelance web designer. Your job is to "
        "turn a short fact about websites / web design / SEO into an Instagram post that "
        "EDUCATES POTENTIAL CLIENTS (small business owners, not other designers) and "
        "DRIVES THEM TO DM THE ACCOUNT FOR WEBSITE WORK. Tone: friendly expert, not salesy. "
        "Talk to business owners like a helpful advisor, not a designer showing off. "
        "Every post should make a small business owner think 'I should DM this person "
        "about my website.'"
    )
    user_prompt = f"""Topic: {topic_title}
Seed fact: {seed_fact}

Write an Instagram post for a freelance web designer's account. The audience is
POTENTIAL CLIENTS (small business owners), not other designers. The goal is to
educate them AND drive DMs for new website projects.

Return STRICT JSON with these keys (and no others):

{{
  "image_headline": "A short punchy headline (4-8 words) to render on the image itself. Must speak to a business owner's pain or goal. No emojis. No hashtags. Plain text only. Example: 'Your website is losing customers.'",
  "caption": "An Instagram caption (150-280 chars). Start with a hook line that grabs a business owner's attention. Then explain the tip in 1-2 plain-English sentences (no jargon). End with a soft CTA that invites a DM, e.g. 'DM me to audit your current site' or 'Need a website that converts? DM me.' No emojis. Hashtags go separately.",
  "hashtags": ["4 to 6 relevant hashtags mixing broad (#webdesign, #smallbusiness) and niche (#freelancewebdesigner, #websiteredesign). Each starts with #, lowercase, no spaces."]
}}

Rules:
- The image_headline must be a different phrasing from the seed fact (shorter, punchier, client-facing).
- The caption must NOT repeat the image_headline verbatim.
- Speak to business owners, NOT to other designers. No jargon like 'CSS', 'WCAG', 'Core Web Vitals' unless explained.
- The CTA must invite a DM. Variations: 'DM me to...', 'DM for...', 'Need a...? DM me.'
- No emoji anywhere. No mention of 'AI' or 'generated'.
- Pure JSON only. No markdown fences. No prose before or after."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Llama 4 models may not support response_format json_object. Try with it
    # first; if 400, retry without it (we'll parse JSON from content ourselves).
    for use_json_mode in (True, False):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 600,
            "temperature": 0.7,
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}

        print(f"[INFO] Calling Groq (model={model}, json_mode={use_json_mode})...")
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=body, timeout=GROQ_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            print(f"[WARN] Groq network error: {exc}. Falling back to deterministic content.")
            return None

        if resp.status_code == 400 and use_json_mode:
            # Some models don't support response_format. Retry without it.
            print("[INFO] Groq rejected json_mode (400). Retrying without response_format...")
            continue
        break

    if resp.status_code != 200:
        print(f"[WARN] Groq returned HTTP {resp.status_code}. Falling back to deterministic content.")
        if resp.status_code == 403:
            print("[INFO] HTTP 403 = key invalid/expired OR IP blocked. Update GROQ_API_KEY secret.")
        elif resp.status_code == 404:
            print(f"[INFO] HTTP 404 = model '{model}' not found or deprecated.")
            # List available models for diagnostics
            available = _list_groq_models(api_key)
            if available:
                print(f"[INFO] Available models on your account ({len(available)}): {', '.join(available[:15])}")
                print("[INFO] Set the GROQ_MODEL env var / secret to one of the above.")
            else:
                print("[INFO] Could not list available models (key may be invalid).")
        elif resp.status_code == 429:
            print("[INFO] HTTP 429 = rate limit. Will retry on next run.")
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        print(f"[WARN] Groq response was unparseable: {exc}. Falling back.")
        return None

    # Strip markdown fences if model didn't honor json_mode
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"[WARN] Groq returned non-JSON content: {exc}. Falling back.")
        return None

    required_keys = {"image_headline", "caption", "hashtags"}
    if not required_keys.issubset(parsed.keys()):
        print(f"[WARN] Groq JSON missing keys. Got: {list(parsed.keys())}. Falling back.")
        return None
    if not isinstance(parsed["hashtags"], list) or not all(isinstance(h, str) for h in parsed["hashtags"]):
        print("[WARN] Groq hashtags field is malformed. Falling back.")
        return None

    print(f"[INFO] Groq generation OK (model={model})")
    return {
        "image_headline": str(parsed["image_headline"]).strip(),
        "caption": str(parsed["caption"]).strip(),
        "hashtags": [str(h).strip() for h in parsed["hashtags"]][:5],
    }


# ---------------------------------------------------------------------------
# Deterministic fallback (used when Groq is unavailable or fails)
# ---------------------------------------------------------------------------

def build_deterministic_caption(topic: dict[str, Any], fact: str) -> dict[str, Any]:
    """Build image_headline + caption + hashtags from the seed fact directly.
    Used when Groq is unavailable. The caption is intentionally simple and
    ends with a DM-driving CTA so it still serves the lead-gen purpose.
    """
    title = topic.get("title", "Website Tip")
    # Headline: take the first 6-8 words of the fact for the image overlay
    words = fact.replace("—", " ").split()
    headline = " ".join(words[:7]) + ("..." if len(words) > 7 else "")
    caption = (
        f"{title}\n\n{fact}\n\n"
        f"Need a website that actually brings you clients? DM me — let's talk about your project."
    )
    hashtags = topic.get("hashtags") or ["#webdesign", "#freelancewebdesigner", "#smallbusiness"]
    return {
        "image_headline": headline,
        "caption": caption,
        "hashtags": list(hashtags)[:5],
    }


# ---------------------------------------------------------------------------
# Image rendering (unchanged from v1 — 1080x1080 gradient + title + headline)
# ---------------------------------------------------------------------------

TOPIC_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "UX Principle":         ((20, 30, 80), (80, 30, 130)),
    "Conversion Tip":       ((10, 80, 60), (40, 160, 120)),
    "Typography Tip":       ((80, 20, 80), (180, 30, 120)),
    "Color & Visual Design":((40, 20, 80), (120, 60, 180)),
    "Page Speed":           ((30, 30, 50), (60, 100, 180)),
    "Mobile-First":         ((20, 60, 80), (60, 140, 160)),
    "SEO Foundation":       ((60, 10, 30), (140, 30, 60)),
}


def _find_font(size: int) -> Any:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    if ImageFont is None:
        return None
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: Any, draw: Any, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        try:
            bbox = draw.textbbox((0, 0), trial, font=font)
            width = bbox[2] - bbox[0]
        except Exception:
            width = len(trial) * (font.size if hasattr(font, "size") else 10) * 0.55
        if width <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_image(out_path: Path, topic: dict[str, Any], headline: str, date: dt.date) -> None:
    if Image is None:
        raise RuntimeError(f"Pillow is required to render images: {_PILLOW_ERR}")

    size = 1080
    title = topic.get("title", "Daily Tip")
    color_pair = TOPIC_COLORS.get(title, ((30, 30, 60), (60, 60, 120)))
    top_color, bottom_color = color_pair

    img = Image.new("RGB", (size, size), top_color)
    pixels = img.load()
    for y in range(size):
        t = y / (size - 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        for x in range(size):
            pixels[x, y] = (r, g, b)

    draw = ImageDraw.Draw(img)
    title_font = _find_font(64)
    title_x = 80
    title_y = 120
    draw.text((title_x, title_y), title, font=title_font, fill=(255, 255, 255))
    draw.rectangle([(title_x, title_y + 90), (title_x + 200, title_y + 96)], fill=(255, 255, 255))

    # Headline (wrapped, centered)
    headline_font = _find_font(56)
    max_text_width = size - 160
    lines = _wrap_text(headline, headline_font, draw, max_text_width)
    line_height = 76
    total_height = line_height * len(lines)
    y_start = (size - total_height) // 2 + 60
    for i, line in enumerate(lines):
        draw.text((title_x, y_start + i * line_height), line, font=headline_font, fill=(240, 240, 240))

    footer_font = _find_font(32)
    footer_text = f"{date.isoformat()}  -  DM for website work"
    draw.text((title_x, size - 100), footer_text, font=footer_font, fill=(220, 220, 220))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def content_id(date: dt.date) -> str:
    return date.isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily Instagram content for a web design agency.")
    parser.add_argument("--date", help="Override date as YYYY-MM-DD (defaults to today UTC).")
    parser.add_argument("--out-dir", default="content", help="Output directory (default: content)")
    args = parser.parse_args()

    if args.date:
        try:
            date = dt.date.fromisoformat(args.date)
        except ValueError as exc:
            print(f"[ERROR] Invalid --date: {exc}", file=sys.stderr)
            return 2
    else:
        date = dt.date.today()

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir = out_dir / "images"
    images_dir.mkdir(exist_ok=True)

    manifest_path = out_dir / f"{date.isoformat()}.json"

    # IDEMPOTENCY: if manifest already exists and was AI-generated, reuse it
    # (avoids burning Groq credits on workflow re-runs)
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("id") == content_id(date) and existing.get("ai_generated"):
                print(f"[INFO] Manifest {manifest_path} already exists and is AI-generated. Reusing (idempotency).")
                image_path = images_dir / existing.get("image_filename", f"{date.isoformat()}.png")
                if image_path.exists():
                    print(f"[INFO] Image already rendered: {image_path}")
                    return 0
                # Image missing but manifest present — re-render from manifest
                render_image(image_path, {"title": existing.get("topic", "")},
                             existing.get("image_headline", existing.get("fact", "")), date)
                print(f"[INFO] Image re-rendered: {image_path}")
                return 0
        except (json.JSONDecodeError, OSError):
            pass  # fall through to fresh generation

    print(f"[INFO] Generating content for {date.isoformat()}")
    topic, fact, fact_index = pick_topic_and_fact(date)
    print(f"[INFO] Topic: {topic['title']} (fact #{fact_index + 1})")
    print(f"[INFO] Seed fact: {fact}")

    # Try Groq; fall back to deterministic content on any error
    ai_result = generate_with_groq(topic["title"], fact)
    if ai_result is not None:
        image_headline = ai_result["image_headline"]
        caption = ai_result["caption"]
        hashtags = ai_result["hashtags"]
        ai_generated = True
        full_caption = caption + "\n\n" + " ".join(hashtags)
    else:
        det = build_deterministic_caption(topic, fact)
        image_headline = det["image_headline"]
        caption = det["caption"]
        hashtags = det["hashtags"]
        ai_generated = False
        full_caption = caption + "\n\n" + " ".join(hashtags)

    image_filename = f"{date.isoformat()}.png"
    image_path = images_dir / image_filename
    render_image(image_path, topic, image_headline, date)
    print(f"[INFO] Image written: {image_path} ({image_path.stat().st_size} bytes)")

    manifest = {
        "id": content_id(date),
        "date": date.isoformat(),
        "topic": topic["title"],
        "seed_fact": fact,
        "image_headline": image_headline,
        "caption": full_caption,
        "hashtags": hashtags,
        "image_file": f"content/images/{image_filename}",
        "image_filename": image_filename,
        "ai_generated": ai_generated,
        "generator": "groq" if ai_generated else "deterministic-fallback",
    }

    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"[INFO] Manifest written: {manifest_path}")
    print(f"[INFO] Generator: {manifest['generator']}")
    print(f"[INFO] Image headline: {image_headline}")
    print(f"[INFO] Caption preview:\n{full_caption}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
