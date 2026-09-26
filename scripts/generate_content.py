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
    # gpt-oss models ALSO emit </think>reasoning blocks before the answer, so we need
    # generous max_tokens to fit both the reasoning + the final JSON.
    for use_json_mode in (True, False):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 2000,  # gpt-oss needs room for </think> + JSON
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

    # gpt-oss models emit <think>...</think> reasoning blocks before the
    # actual answer. Strip them. Also strip markdown fences if present.
    content = content.strip()

    # Remove <think>...</think> blocks (case-insensitive, multiline, non-greedy)
    import re
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    # If there's an unclosed <think> tag, drop everything from it to the end
    # (some models emit a partial block when cut off by max_tokens)
    content = re.sub(r"<think>.*$", "", content, flags=re.IGNORECASE | re.DOTALL)

    # Strip markdown fences (```json ... ```)
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()

    # If the model emitted any prose before the JSON, find the first '{' and
    # cut everything before it. Same for trailing text after the last '}'.
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        content = content[first_brace : last_brace + 1]

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"[WARN] Groq returned non-JSON content: {exc}. Falling back.")
        # Print a redacted snippet for debugging (helps tune the prompt)
        snippet = content[:300] + ("...<truncated>" if len(content) > 300 else "")
        print(f"[INFO] Raw content snippet: {snippet}")
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
# Image rendering — 6 distinct visual styles, rotated daily
# ---------------------------------------------------------------------------

# Per-topic color palette: primary / accent / dark / light.
# Each topic gets a unique palette so the brand color shifts through the week.
TOPIC_PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "Why You Need A Website": {
        "primary": (20, 30, 80), "accent": (255, 196, 0),
        "dark": (15, 20, 50), "light": (245, 245, 250),
    },
    "Website Mistakes Losing You Clients": {
        "primary": (140, 30, 60), "accent": (255, 220, 100),
        "dark": (60, 10, 30), "light": (250, 240, 240),
    },
    "What A Website Really Costs": {
        "primary": (10, 80, 60), "accent": (240, 200, 80),
        "dark": (5, 40, 30), "light": (240, 250, 245),
    },
    "Signs You Need A Redesign": {
        "primary": (80, 30, 130), "accent": (255, 180, 80),
        "dark": (40, 15, 70), "light": (245, 240, 250),
    },
    "WordPress vs Wix vs Custom": {
        "primary": (180, 60, 30), "accent": (60, 80, 160),
        "dark": (80, 30, 15), "light": (252, 245, 240),
    },
    "How Long A Website Takes": {
        "primary": (20, 70, 110), "accent": (255, 100, 80),
        "dark": (10, 35, 55), "light": (240, 248, 252),
    },
    "Local SEO For Small Business": {
        "primary": (50, 70, 50), "accent": (240, 180, 60),
        "dark": (25, 35, 25), "light": (245, 248, 240),
    },
}

DEFAULT_PALETTE: dict[str, tuple[int, int, int]] = {
    "primary": (30, 30, 60), "accent": (255, 196, 0),
    "dark": (15, 15, 30), "light": (245, 245, 250),
}


def _palette_for(topic_title: str) -> dict[str, tuple[int, int, int]]:
    return TOPIC_PALETTES.get(topic_title, DEFAULT_PALETTE)


def _load_font(size: int, family: str = "sans-bold") -> Any:
    """Load a font by family. Falls back gracefully.

    family options: sans-bold, sans-regular, sans-italic, serif-bold,
    serif-regular, serif-italic, mono-bold, mono-regular.
    """
    candidates = {
        "sans-bold":     ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"],
        "sans-regular":  ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSans.ttf"],
        "sans-italic":   ["/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf"],
        "serif-bold":    ["/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf"],
        "serif-regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSerif.ttf"],
        "serif-italic":  ["/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf"],
        "mono-bold":     ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf"],
        "mono-regular":  ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                          "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
                          "/usr/share/fonts/truetype/freefont/FreeMono.ttf"],
    }
    if ImageFont is None:
        return None
    for path in candidates.get(family, candidates["sans-bold"]):
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


def _text_width(draw: Any, text: str, font: Any) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * (font.size if hasattr(font, "size") else 10) * 0.55


def _vertical_gradient(img: Any, top_color: tuple, bottom_color: tuple) -> None:
    """Paint a vertical gradient onto img (in-place)."""
    h = img.size[1]
    pixels = img.load()
    for y in range(h):
        t = y / (h - 1)
        r = int(top_color[0] + (bottom_color[0] - top_color[0]) * t)
        g = int(top_color[1] + (bottom_color[1] - top_color[1]) * t)
        b = int(top_color[2] + (bottom_color[2] - top_color[2]) * t)
        for x in range(img.size[0]):
            pixels[x, y] = (r, g, b)


# --- Style 1: gradient_centered --- vertical gradient, large centered headline
def _render_gradient_centered(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    _vertical_gradient(img, palette["primary"], palette["dark"])
    title_font = _load_font(60, "sans-bold")
    draw.text((80, 110), title, font=title_font, fill=palette["light"])
    draw.rectangle([(80, 195), (300, 200)], fill=palette["accent"])
    headline_font = _load_font(64, "sans-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 160)
    line_h = 80
    y_start = (size - line_h * len(lines)) // 2 + 50
    for i, line in enumerate(lines):
        draw.text((80, y_start + i * line_h), line, font=headline_font, fill=palette["light"])
    footer_font = _load_font(28, "mono-regular")
    draw.text((80, size - 90), f"{date.isoformat()}  ·  DM for website work",
              font=footer_font, fill=palette["light"])


# --- Style 2: split_block --- left color panel + vertical topic label,
#     right white panel with serif headline. Editorial feel.
def _render_split_block(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    draw.rectangle([(0, 0), (size, size)], fill=palette["light"])
    split_x = int(size * 0.40)
    draw.rectangle([(0, 0), (split_x, size)], fill=palette["primary"])
    # Vertical topic label (one char per line)
    label_font = _load_font(40, "sans-bold")
    char_y = 90
    for ch in title.upper():
        draw.text((split_x // 2 - 18, char_y), ch, font=label_font, fill=palette["accent"])
        char_y += 50
    draw.rectangle([(split_x // 2 - 4, size - 280), (split_x // 2 + 4, size - 80)],
                   fill=palette["accent"])
    headline_font = _load_font(58, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, size - split_x - 100)
    line_h = 72
    y_start = (size - line_h * len(lines)) // 2
    for i, line in enumerate(lines):
        draw.text((split_x + 60, y_start + i * line_h), line,
                  font=headline_font, fill=palette["dark"])
    footer_font = _load_font(24, "mono-regular")
    draw.text((split_x + 60, size - 90), f"{date.isoformat()}  ·  DM for website work",
              font=footer_font, fill=palette["primary"])


# --- Style 3: minimalist_white --- mostly white, thin accent bar,
#     small uppercase topic label, large centered headline. Apple-keynote feel.
def _render_minimalist_white(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    draw.rectangle([(0, 0), (size, size)], fill=(252, 252, 250))
    draw.rectangle([(0, 0), (size, 10)], fill=palette["primary"])
    draw.rectangle([(80, 80), (110, 110)], fill=palette["accent"])
    label_font = _load_font(28, "sans-bold")
    draw.text((130, 84), title.upper(), font=label_font, fill=palette["primary"])
    headline_font = _load_font(72, "sans-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 200)
    line_h = 88
    y_start = (size - line_h * len(lines)) // 2
    for i, line in enumerate(lines):
        lw = _text_width(draw, line, headline_font)
        draw.text(((size - lw) // 2, y_start + i * line_h), line,
                  font=headline_font, fill=palette["dark"])
    div_y = y_start + line_h * len(lines) + 40
    draw.rectangle([(size // 2 - 60, div_y), (size // 2 + 60, div_y + 3)],
                   fill=palette["accent"])
    footer_font = _load_font(24, "mono-regular")
    footer = f"{date.isoformat()}   ·   DM for website work"
    fw = _text_width(draw, footer, footer_font)
    draw.text(((size - fw) // 2, size - 80), footer,
              font=footer_font, fill=palette["primary"])


# --- Style 4: dark_neon --- black bg, neon accent headline (uppercase).
#     Edgy / tech feel.
def _render_dark_neon(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    bg = tuple(min(255, c // 12) for c in palette["primary"])
    draw.rectangle([(0, 0), (size, size)], fill=bg)
    label_font = _load_font(32, "mono-bold")
    draw.text((80, 100), title.upper(), font=label_font, fill=palette["accent"])
    draw.rectangle([(80, 150), (180, 153)], fill=palette["accent"])
    headline_font = _load_font(78, "sans-bold")
    lines = _wrap_text(headline.upper(), headline_font, draw, size - 160)
    line_h = 92
    y_start = (size - line_h * len(lines)) // 2 + 30
    for i, line in enumerate(lines):
        draw.text((80, y_start + i * line_h), line,
                  font=headline_font, fill=palette["accent"])
    underline_y = y_start + line_h * len(lines) + 20
    draw.rectangle([(80, underline_y), (300, underline_y + 4)], fill=palette["accent"])
    footer_font = _load_font(24, "mono-regular")
    draw.text((80, size - 80), f"{date.isoformat()}  ·  DM for website work",
              font=footer_font, fill=(180, 180, 180))


# --- Style 5: magazine_cover --- top color bar with topic, big serif headline,
#     bottom color block with date. Print-magazine feel.
def _render_magazine_cover(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    draw.rectangle([(0, 0), (size, size)], fill=(250, 246, 240))
    bar_h = 180
    draw.rectangle([(0, 0), (size, bar_h)], fill=palette["primary"])
    title_font = _load_font(58, "serif-bold")
    draw.text((80, 60), title, font=title_font, fill=palette["light"])
    issue_font = _load_font(22, "mono-regular")
    issue_text = f"ISSUE  ·  {date.strftime('%Y-%m')}"
    iw = _text_width(draw, issue_text, issue_font)
    draw.text((size - iw - 80, 70), issue_text, font=issue_font, fill=palette["accent"])
    # Decorative big quote mark
    quote_font = _load_font(180, "serif-bold")
    draw.text((60, bar_h - 40), "\u201C", font=quote_font, fill=palette["accent"])
    headline_font = _load_font(68, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 160)
    line_h = 84
    y_start = bar_h + 80
    for i, line in enumerate(lines):
        draw.text((80, y_start + i * line_h), line,
                  font=headline_font, fill=palette["dark"])
    bottom_h = 110
    draw.rectangle([(0, size - bottom_h), (size, size)], fill=palette["primary"])
    footer_font = _load_font(26, "mono-bold")
    draw.text((80, size - bottom_h + 40), f"{date.isoformat()}  ·  DM FOR WEBSITE WORK",
              font=footer_font, fill=palette["accent"])
    page_font = _load_font(26, "mono-bold")
    page_text = "01"
    pw = _text_width(draw, page_text, page_font)
    draw.text((size - pw - 80, size - bottom_h + 40), page_text,
              font=page_font, fill=palette["accent"])


# --- Style 6: quote_card --- big italic serif headline as a "quote",
#     decorative quotation marks, accent line. Linkedin-quote feel.
def _render_quote_card(img, draw, topic, headline, date, palette):
    size = img.size[0]
    title = topic.get("title", "Daily Tip")
    bg = tuple(int(c * 0.85) for c in palette["primary"])
    draw.rectangle([(0, 0), (size, size)], fill=bg)
    quote_font = _load_font(280, "serif-bold")
    draw.text((50, -40), "\u201C", font=quote_font, fill=palette["accent"])
    headline_font = _load_font(62, "serif-italic")
    lines = _wrap_text(headline, headline_font, draw, size - 200)
    line_h = 80
    y_start = (size - line_h * len(lines)) // 2 + 40
    for i, line in enumerate(lines):
        lw = _text_width(draw, line, headline_font)
        draw.text(((size - lw) // 2, y_start + i * line_h), line,
                  font=headline_font, fill=palette["light"])
    line_y = y_start + line_h * len(lines) + 30
    draw.rectangle([(size // 2 - 80, line_y), (size // 2 + 80, line_y + 3)],
                   fill=palette["accent"])
    label_font = _load_font(26, "sans-bold")
    label = title.upper()
    lw = _text_width(draw, label, label_font)
    draw.text(((size - lw) // 2, line_y + 30), label,
              font=label_font, fill=palette["accent"])
    footer_font = _load_font(22, "mono-regular")
    footer = f"{date.isoformat()}   ·   DM for website work"
    fw = _text_width(draw, footer, footer_font)
    draw.text(((size - fw) // 2, size - 70), footer,
              font=footer_font, fill=palette["light"])


# --- Style registry + dispatcher ---
STYLES: list[tuple[str, Any]] = [
    ("gradient_centered",  _render_gradient_centered),
    ("split_block",        _render_split_block),
    ("minimalist_white",   _render_minimalist_white),
    ("dark_neon",          _render_dark_neon),
    ("magazine_cover",     _render_magazine_cover),
    ("quote_card",         _render_quote_card),
]


def pick_style_index(date: dt.date) -> int:
    """Rotate styles daily by hashing the date. Same date = same style."""
    h = int(hashlib.sha256(date.isoformat().encode()).hexdigest(), 16)
    return h % len(STYLES)


def render_image(out_path: Path, topic: dict[str, Any], headline: str,
                 date: dt.date, style_index: int | None = None) -> str:
    """Render the day's image. Returns the style name used.

    style_index: if provided, use that style; otherwise pick by date hash.
    """
    if Image is None:
        raise RuntimeError(f"Pillow is required to render images: {_PILLOW_ERR}")

    size = 1080
    title = topic.get("title", "Daily Tip")
    palette = _palette_for(title)

    if style_index is None:
        style_index = pick_style_index(date)
    style_index = style_index % len(STYLES)
    style_name, style_fn = STYLES[style_index]

    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    style_fn(img, draw, topic, headline, date, palette)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return style_name


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
                # Use the style recorded in the manifest if present, else pick by date
                style_idx = existing.get("style_index")
                style_idx = int(style_idx) if style_idx is not None else None
                render_image(image_path, {"title": existing.get("topic", "")},
                             existing.get("image_headline", existing.get("fact", "")),
                             date, style_index=style_idx)
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
    style_index = pick_style_index(date)
    style_name = render_image(image_path, topic, image_headline, date, style_index=style_index)
    print(f"[INFO] Image written: {image_path} ({image_path.stat().st_size} bytes)")
    print(f"[INFO] Style: {style_name} (index {style_index})")

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
        "image_style": style_name,
        "style_index": style_index,
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
