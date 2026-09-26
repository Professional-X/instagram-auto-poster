"""
generate_content.py
-------------------
Daily content generator for a freelance web designer's Instagram account.

Runs 4 times per day (slots 1-4: morning / lunch / evening / night IST).
Each slot picks a different topic + different visual style, so the daily feed
shows 4 visually distinct posts that all look like actual web design work
(browser mockups, phone mockups, before/after, palette cards, etc.) —
NOT generic quote cards.

Pipeline:
  1. Pick topic by (day_of_year * 4 + slot) % len(topics) — each slot gets a
     different topic on the same day.
  2. Pick seed fact by (date_hash + slot) % len(facts) — each slot gets a
     different fact within the topic.
  3. If GROQ_API_KEY is set: call Groq to expand seed fact into a polished
     Instagram caption + image headline + hashtags. Falls back gracefully.
  4. Render a 1080x1080 PNG using one of 6 "real web design work" styles
     (browser mockup, phone mockup, before/after, palette card, stats callout,
     component grid). Style is picked by (date_hash + slot) % 6 so each slot
     on the same day gets a different style.
  5. Write content/<date>-<slot>.json manifest.

Idempotency:
  - If content/<date>-<slot>.json already exists AND has ai_generated=true,
    reuse it (skip the Groq call) — re-running the workflow for the same slot
    won't burn Groq credits or produce a different post.

Usage:
  GROQ_API_KEY=... python scripts/generate_content.py [--date YYYY-MM-DD] [--slot 1-4] [--out-dir content]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
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
        "title": "Why You Need A Website",
        "facts": [
            "70% of consumers research a business online before visiting or contacting them. No website = invisible.",
        ],
        "hashtags": ["#websitedesign", "#smallbusiness", "#freelancer"],
    },
]


def load_topics() -> list[dict[str, Any]]:
    topics_path = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
    if not topics_path.exists():
        return DEFAULT_TOPICS
    try:
        import yaml  # type: ignore
    except ImportError:
        return DEFAULT_TOPICS
    with topics_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not data or "topics" not in data:
        return DEFAULT_TOPICS
    return data["topics"]


def pick_topic_and_fact(date: dt.date, slot: int) -> tuple[dict[str, Any], str, int]:
    """Rotate topics by (day_of_year * 4 + slot) so each slot on the same day
    gets a different topic. Within a topic, pick a fact by (date_hash + slot).
    """
    topics = load_topics()
    if not topics:
        raise RuntimeError("No topics available")
    day_of_year = date.timetuple().tm_yday
    topic_index = (day_of_year * 4 + (slot - 1)) % len(topics)
    topic = topics[topic_index]
    date_hash = int(hashlib.sha256(date.isoformat().encode()).hexdigest(), 16)
    facts = topic.get("facts") or []
    if not facts:
        raise RuntimeError(f"Topic '{topic.get('title')}' has no facts")
    fact_index = (date_hash + slot) % len(facts)
    return topic, facts[fact_index], fact_index


# ---------------------------------------------------------------------------
# Groq AI integration (OpenAI-compatible /openai/v1/chat/completions)
# ---------------------------------------------------------------------------

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GROQ_TIMEOUT_SECONDS = 45


def _list_groq_models(api_key: str) -> list[str] | None:
    try:
        resp = requests.get(GROQ_MODELS_URL,
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=15)
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
    failure. Caller must fall back gracefully.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key or not api_key.strip():
        return None
    model = os.environ.get("GROQ_MODEL") or GROQ_DEFAULT_MODEL

    system_prompt = (
        "You are the social media manager for a freelance web designer. Your job is to "
        "turn a short fact about websites / web design / SEO into an Instagram post that "
        "EDUCATES POTENTIAL CLIENTS (small business owners, not other designers) and "
        "DRIVES THEM TO DM THE ACCOUNT FOR WEBSITE WORK. Tone: friendly expert, not salesy. "
        "Talk to business owners like a helpful advisor. Every post should make a small "
        "business owner think 'I should DM this person about my website.'"
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
- Speak to business owners, NOT to other designers. No jargon unless explained.
- The CTA must invite a DM.
- No emoji anywhere. No mention of 'AI' or 'generated'.
- Pure JSON only. No markdown fences. No prose before or after."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for use_json_mode in (True, False):
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 2000,
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
            print("[INFO] Groq rejected json_mode (400). Retrying without response_format...")
            continue
        break

    if resp.status_code != 200:
        print(f"[WARN] Groq returned HTTP {resp.status_code}. Falling back to deterministic content.")
        if resp.status_code == 403:
            print("[INFO] HTTP 403 = key invalid/expired OR IP blocked. Update GROQ_API_KEY secret.")
        elif resp.status_code == 404:
            print(f"[INFO] HTTP 404 = model '{model}' not found or deprecated.")
            available = _list_groq_models(api_key)
            if available:
                print(f"[INFO] Available models ({len(available)}): {', '.join(available[:15])}")
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

    # gpt-oss models emit <reasoning>...</reasoning> blocks before the answer
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
        print(f"[WARN] Groq returned non-JSON content: {exc}. Falling back.")
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
# Deterministic fallback
# ---------------------------------------------------------------------------

def build_deterministic_caption(topic: dict[str, Any], fact: str) -> dict[str, Any]:
    title = topic.get("title", "Website Tip")
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
# Image rendering — 6 "real web design work" styles
# ---------------------------------------------------------------------------
# IMPORTANT: These styles are designed to make the account look like an actual
# web designer's portfolio, not a generic quote-card account. Each style
# visually represents real design work:
#   1. browser_hero      — a browser window showing a full website hero
#   2. phone_mobile      — a phone frame showing a mobile website view
#   3. before_after      — side-by-side ugly-cluttered vs clean-modern
#   4. palette_typography — color swatches + font pairing showcase
#   5. stats_callout     — big bold stat + supporting text (infographic)
#   6. component_grid    — UI components (buttons, cards, form fields) grid
# ---------------------------------------------------------------------------

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


# Industry vibes per topic — used to make each browser mockup feel like a
# different real client website (not the same template over and over).
TOPIC_INDUSTRY: dict[str, dict[str, str]] = {
    "Why You Need A Website":           {"brand": "Bloom Cafe",       "url": "bloomcafe.com",      "cta": "Order Now"},
    "Website Mistakes Losing You Clients": {"brand": "Apex Fitness",  "url": "apexfit.com",        "cta": "Join Today"},
    "What A Website Really Costs":      {"brand": "Lawson Law",       "url": "lawsonlaw.com",      "cta": "Free Consult"},
    "Signs You Need A Redesign":        {"brand": "Urban Stays",      "url": "urbanstays.com",     "cta": "Book Stay"},
    "WordPress vs Wix vs Custom":       {"brand": "Maker Goods",      "url": "makergoods.com",     "cta": "Shop Now"},
    "How Long A Website Takes":         {"brand": "Bright Smile Dental", "url": "brightsmile.com", "cta": "Book Visit"},
    "Local SEO For Small Business":     {"brand": "Rivera Plumbing",  "url": "riveraplumbing.com", "cta": "Call Now"},
}

DEFAULT_INDUSTRY = {"brand": "Your Business", "url": "yourbusiness.com", "cta": "Get Quote"}


def _industry_for(topic_title: str) -> dict[str, str]:
    return TOPIC_INDUSTRY.get(topic_title, DEFAULT_INDUSTRY)


# ---------------------------------------------------------------------------
# Style 1: browser_hero — A browser window showing a full website hero.
# The "tip" headline becomes the hero text of a believable client website.
# This is the primary style — it directly signals "I design websites."
# ---------------------------------------------------------------------------

def _render_browser_hero(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")
    industry = _industry_for(title)

    # Outer background (light cream)
    draw.rectangle([(0, 0), (size, size)], fill=(248, 246, 242))

    # Outer caption strip (top) — topic label + DM CTA
    label_font = _load_font(22, "mono-bold")
    label = f"  {title.upper()}  ·  DM FOR WEBSITE WORK  ·  {date.isoformat()}"
    draw.rectangle([(0, 0), (size, 50)], fill=palette["dark"])
    draw.text((40, 14), label, font=label_font, fill=palette["accent"])

    # Browser frame
    bx, by = 50, 80
    bw, bh = size - 100, size - 130

    # Drop shadow
    draw.rounded_rectangle([bx + 6, by + 6, bx + bw + 6, by + bh + 6], radius=8, fill=(220, 220, 220))
    # Browser body
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8, fill=(255, 255, 255))

    # Browser chrome (top bar)
    chrome_h = 56
    draw.rectangle([bx, by, bx + bw, by + chrome_h], fill=(245, 245, 248))
    draw.line([(bx, by + chrome_h), (bx + bw, by + chrome_h)], fill=(220, 220, 225), width=1)

    # Traffic lights
    light_y = by + chrome_h // 2
    draw.ellipse([bx + 20, light_y - 7, bx + 34, light_y + 7], fill=(255, 95, 86))
    draw.ellipse([bx + 42, light_y - 7, bx + 56, light_y + 7], fill=(255, 189, 46))
    draw.ellipse([bx + 64, light_y - 7, bx + 78, light_y + 7], fill=(39, 201, 63))

    # URL bar
    url_x = bx + 100
    url_w = bw - 130
    draw.rounded_rectangle([url_x, light_y - 13, url_x + url_w, light_y + 13], radius=6, fill=(230, 230, 235))
    url_font = _load_font(18, "mono-regular")
    # Lock icon (small green circle)
    draw.ellipse([url_x + 12, light_y - 4, url_x + 20, light_y + 4], fill=(60, 160, 80))
    draw.text((url_x + 28, light_y - 9), industry["url"], font=url_font, fill=(110, 110, 120))

    # Website content area
    cx = bx + 2
    cy = by + chrome_h + 2
    cw = bw - 4
    ch = bh - chrome_h - 4

    # --- Sticky nav ---
    nav_h = 56
    draw.rectangle([cx, cy, cx + cw, cy + nav_h], fill=palette["primary"])
    # Logo (square + brand name)
    draw.rectangle([cx + 30, cy + 18, cx + 58, cy + 38], fill=palette["accent"])
    brand_font = _load_font(20, "serif-bold")
    draw.text((cx + 66, cy + 17), industry["brand"], font=brand_font, fill=palette["light"])
    # Nav items (right-aligned)
    nav_font = _load_font(15, "sans-regular")
    nav_items = ["Home", "About", "Services", "Contact"]
    nx = cx + cw - 200
    for item in nav_items:
        draw.text((nx, cy + 20), item, font=nav_font, fill=palette["light"])
        nx += 45
    # CTA button
    cta_w = 90
    draw.rounded_rectangle([cx + cw - cta_w - 20, cy + 14, cx + cw - 20, cy + 42], radius=5, fill=palette["accent"])
    cta_font = _load_font(13, "sans-bold")
    cta_label = industry["cta"]
    cta_lw = _text_width(draw, cta_label, cta_font)
    draw.text((cx + cw - 20 - cta_w // 2 - cta_lw // 2, cy + 20), cta_label, font=cta_font, fill=palette["dark"])

    # --- Hero section ---
    hero_y = cy + nav_h
    hero_h = 320
    # Hero left column (text)
    hero_left_w = int(cw * 0.55)
    headline_font = _load_font(38, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, hero_left_w - 70)
    line_h = 46
    text_y = hero_y + 50
    for line in lines:
        draw.text((cx + 30, text_y), line, font=headline_font, fill=palette["dark"])
        text_y += line_h
    # Subheadline
    sub_font = _load_font(16, "sans-regular")
    sub_y = text_y + 16
    sub_lines = _wrap_text("Professional websites that turn visitors into paying customers. Built fast, designed to convert.",
                           sub_font, draw, hero_left_w - 70)
    for sl in sub_lines[:2]:
        draw.text((cx + 30, sub_y), sl, font=sub_font, fill=(110, 110, 120))
        sub_y += 22
    # Two CTAs
    btn_y = sub_y + 24
    draw.rounded_rectangle([cx + 30, btn_y, cx + 200, btn_y + 44], radius=6, fill=palette["primary"])
    draw.text((cx + 60, btn_y + 14), industry["cta"], font=cta_font, fill=palette["light"])
    draw.rounded_rectangle([cx + 220, btn_y, cx + 380, btn_y + 44], radius=6, outline=palette["primary"], width=2)
    draw.text((cx + 250, btn_y + 14), "Learn More", font=cta_font, fill=palette["primary"])

    # Hero right column (image placeholder block)
    hr_x = cx + hero_left_w + 10
    hr_y = hero_y + 30
    hr_w = cw - hero_left_w - 40
    hr_h = 240
    draw.rounded_rectangle([hr_x, hr_y, hr_x + hr_w, hr_y + hr_h], radius=10, fill=(235, 235, 240))
    # Inner "image" — gradient-feel block with accent
    draw.rounded_rectangle([hr_x + 15, hr_y + 15, hr_x + hr_w - 15, hr_y + hr_h - 15], radius=6, fill=palette["accent"])
    # Decorative circle (faux photo subject)
    draw.ellipse([hr_x + hr_w // 2 - 50, hr_y + hr_h // 2 - 50, hr_x + hr_w // 2 + 50, hr_y + hr_h // 2 + 50], fill=palette["primary"])

    # --- Features row (3 cards) ---
    feat_y = hero_y + hero_h + 20
    feat_h = 140
    card_w = (cw - 80) // 3
    features = [
        ("Fast", "Loads under 2s"),
        ("Mobile", "Looks great on phone"),
        ("SEO", "Ranks on Google"),
    ]
    for i, (ftitle, fdesc) in enumerate(features):
        card_x = cx + 20 + i * (card_w + 20)
        draw.rounded_rectangle([card_x, feat_y, card_x + card_w, feat_y + feat_h], radius=6, fill=(250, 250, 252))
        # Top accent bar
        draw.rectangle([card_x, feat_y, card_x + card_w, feat_y + 4], fill=palette["accent"])
        # Icon circle
        draw.ellipse([card_x + 20, feat_y + 22, card_x + 60, feat_y + 62], fill=palette["primary"])
        # Title
        title_font = _load_font(16, "sans-bold")
        draw.text((card_x + 20, feat_y + 75), ftitle, font=title_font, fill=palette["dark"])
        # Desc
        draw.text((card_x + 20, feat_y + 100), fdesc, font=sub_font, fill=(120, 120, 130))

    # --- Footer strip ---
    foot_y = feat_y + feat_h + 16
    draw.rectangle([cx, foot_y, cx + cw, foot_y + 36], fill=palette["dark"])
    foot_font = _load_font(13, "sans-regular")
    draw.text((cx + 30, foot_y + 11), f"\u00A9 2026 {industry['brand']}  \u00B7  Designed by your freelancer",
              font=foot_font, fill=(180, 180, 190))


# ---------------------------------------------------------------------------
# Style 2: phone_mobile — A phone frame showing a mobile website view.
# ---------------------------------------------------------------------------

def _render_phone_mobile(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")
    industry = _industry_for(title)

    # Background: palette primary, darkened slightly
    bg = tuple(int(c * 0.9) for c in palette["primary"])
    draw.rectangle([(0, 0), (size, size)], fill=bg)

    # Topic label (top)
    label_font = _load_font(24, "mono-bold")
    label = title.upper()
    lw = _text_width(draw, label, label_font)
    draw.text(((size - lw) // 2, 50), label, font=label_font, fill=palette["accent"])

    # Date + CTA (bottom)
    footer_font = _load_font(20, "mono-regular")
    footer = f"{date.isoformat()}  \u00B7  DM for website work"
    fw = _text_width(draw, footer, footer_font)
    draw.text(((size - fw) // 2, size - 60), footer, font=footer_font, fill=palette["light"])

    # Phone frame (centered)
    pw, ph = 440, 880
    px = (size - pw) // 2
    py = 100

    # Phone shadow
    draw.rounded_rectangle([px + 8, py + 8, px + pw + 8, py + ph + 8], radius=40, fill=(0, 0, 0))
    # Phone body (dark bezel)
    draw.rounded_rectangle([px, py, px + pw, py + ph], radius=40, fill=(20, 20, 25))
    # Screen (white)
    sx, sy = px + 12, py + 12
    sw, sh = pw - 24, ph - 24
    draw.rounded_rectangle([sx, sy, sx + sw, sy + sh], radius=30, fill=(255, 255, 255))
    # Notch (top center)
    notch_w = 140
    draw.rounded_rectangle([(sx + sw // 2 - notch_w // 2, sy), (sx + sw // 2 + notch_w // 2, sy + 28)], radius=14, fill=(20, 20, 25))

    # Status bar (faux)
    status_font = _load_font(14, "sans-bold")
    draw.text((sx + 24, sy + 36), "9:41", font=status_font, fill=palette["dark"])
    # Battery icon (small)
    draw.rounded_rectangle([sx + sw - 60, sy + 38, sx + sw - 24, sy + 52], radius=2, fill=palette["dark"])
    draw.rectangle([sx + sw - 24, sy + 42, sx + sw - 20, sy + 48], fill=palette["dark"])

    # Mobile site header (compact)
    hdr_y = sy + 70
    draw.rectangle([sx, hdr_y, sx + sw, hdr_y + 50], fill=palette["primary"])
    draw.rectangle([sx + 20, hdr_y + 18, sx + 40, hdr_y + 32], fill=palette["accent"])  # logo
    brand_font = _load_font(16, "serif-bold")
    draw.text((sx + 50, hdr_y + 16), industry["brand"], font=brand_font, fill=palette["light"])
    # Hamburger icon
    draw.line([(sx + sw - 30, hdr_y + 18), (sx + sw - 14, hdr_y + 18)], fill=palette["light"], width=2)
    draw.line([(sx + sw - 30, hdr_y + 25), (sx + sw - 14, hdr_y + 25)], fill=palette["light"], width=2)
    draw.line([(sx + sw - 30, hdr_y + 32), (sx + sw - 14, hdr_y + 32)], fill=palette["light"], width=2)

    # Mobile hero (headline)
    hero_y = hdr_y + 60
    headline_font = _load_font(28, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, sw - 50)
    line_h = 34
    text_y = hero_y
    for line in lines[:4]:
        draw.text((sx + 20, text_y), line, font=headline_font, fill=palette["dark"])
        text_y += line_h

    # Sub
    sub_font = _load_font(13, "sans-regular")
    sub_y = text_y + 10
    sub_lines = _wrap_text("Get a website that brings you customers.", sub_font, draw, sw - 50)
    for sl in sub_lines[:2]:
        draw.text((sx + 20, sub_y), sl, font=sub_font, fill=(110, 110, 120))
        sub_y += 18

    # CTA button (full width)
    btn_y = sub_y + 20
    draw.rounded_rectangle([sx + 20, btn_y, sx + sw - 20, btn_y + 46], radius=8, fill=palette["primary"])
    cta_font = _load_font(15, "sans-bold")
    cta_label = industry["cta"]
    cta_lw = _text_width(draw, cta_label, cta_font)
    draw.text((sx + (sw - cta_lw) // 2, btn_y + 14), cta_label, font=cta_font, fill=palette["light"])

    # Image placeholder block
    img_y = btn_y + 60
    draw.rounded_rectangle([sx + 20, img_y, sx + sw - 20, img_y + 140], radius=8, fill=(235, 235, 240))
    draw.rounded_rectangle([sx + 35, img_y + 15, sx + sw - 35, img_y + 125], radius=4, fill=palette["accent"])

    # Mobile feature cards (2 stacked)
    card1_y = img_y + 160
    for i, (ftitle, fdesc) in enumerate([("Fast Loading", "Under 2s"), ("Mobile First", "Looks great")]):
        cy_ = card1_y + i * 60
        draw.rounded_rectangle([sx + 20, cy_, sx + sw - 20, cy_ + 50], radius=6, fill=(250, 250, 252))
        draw.ellipse([sx + 30, cy_ + 12, sx + 60, cy_ + 42], fill=palette["primary"])
        title_font = _load_font(14, "sans-bold")
        draw.text((sx + 75, cy_ + 10), ftitle, font=title_font, fill=palette["dark"])
        draw.text((sx + 75, cy_ + 28), fdesc, font=sub_font, fill=(120, 120, 130))


# ---------------------------------------------------------------------------
# Style 3: before_after — Side-by-side ugly-cluttered vs clean-modern.
# Visually demonstrates "I fix bad websites."
# ---------------------------------------------------------------------------

def _render_before_after(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")

    # Background: dark
    draw.rectangle([(0, 0), (size, size)], fill=(28, 28, 32))

    # Top label strip
    label_font = _load_font(22, "mono-bold")
    label = f"  {title.upper()}  \u00B7  {date.isoformat()}  \u00B7  DM TO REDESIGN YOURS"
    draw.rectangle([(0, 0), (size, 50)], fill=palette["accent"])
    draw.text((40, 14), label, font=label_font, fill=palette["dark"])

    # Headline (centered, top)
    headline_font = _load_font(40, "sans-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 80)
    line_h = 50
    total_h = line_h * len(lines)
    y_start = 70 + (180 - total_h) // 2
    for i, line in enumerate(lines):
        lw = _text_width(draw, line, headline_font)
        draw.text(((size - lw) // 2, y_start + i * line_h), line, font=headline_font, fill=palette["light"])

    # Two panels
    panel_y = 260
    panel_h = 640
    panel_w = (size - 80) // 2 - 10
    left_x = 30
    right_x = size // 2 + 10

    # --- LEFT: BEFORE (ugly/cluttered) ---
    # Background: cream
    draw.rounded_rectangle([left_x, panel_y, left_x + panel_w, panel_y + panel_h], radius=10, fill=(245, 240, 230))
    # "BEFORE" label
    before_font = _load_font(28, "sans-bold")
    draw.text((left_x + 20, panel_y + 20), "BEFORE", font=before_font, fill=(180, 60, 60))

    # Ugly mockup: Comic-Sans-feel serif, cluttered text, random colors
    # Bad logo
    bad_font = _load_font(22, "serif-italic")
    draw.text((left_x + 20, panel_y + 70), "My Busine$$ Site!!!", font=bad_font, fill=(255, 0, 150))
    # Bad nav (rainbow)
    nav_y = panel_y + 110
    nav_items = ["HOME", "AboutUs", "STUFF", "Buy!!", "Email"]
    nx = left_x + 20
    bad_colors = [(255, 0, 0), (0, 200, 0), (0, 0, 255), (255, 200, 0), (200, 0, 255)]
    for i, item in enumerate(nav_items):
        nf = _load_font(14, "serif-italic")
        draw.text((nx, nav_y), item, font=nf, fill=bad_colors[i])
        nx += _text_width(draw, item, nf) + 12
    # Horizontal rule
    draw.line([(left_x + 20, nav_y + 30), (left_x + panel_w - 20, nav_y + 30)], fill=(150, 150, 150), width=2)

    # Bad hero: huge red text
    bad_h_font = _load_font(26, "serif-italic")
    draw.text((left_x + 20, nav_y + 50), "WELCOME TO MY", font=bad_h_font, fill=(255, 0, 0))
    draw.text((left_x + 20, nav_y + 80), "WEBSITE!!!", font=bad_h_font, fill=(0, 150, 0))
    # Body wall of text
    bad_body = _load_font(11, "serif-regular")
    body_text = "We are the best company ever!!! We do everything you need and more. Click here NOW for special deals!!! Don't miss out on our amazing offers. We have been in business since 1999 and we are the greatest. Please buy our products!!!"
    bad_lines = _wrap_text(body_text, bad_body, draw, panel_w - 40)
    by = nav_y + 130
    for bl in bad_lines[:10]:
        draw.text((left_x + 20, by), bl, font=bad_body, fill=(60, 60, 60))
        by += 14
    # Marquee-style blinking "NEW!" badges
    for i in range(3):
        bx = left_x + 30 + i * 90
        draw.rectangle([bx, by + 10, bx + 70, by + 50], fill=bad_colors[i])
        draw.text((bx + 12, by + 20), "NEW!", font=_load_font(14, "sans-bold"), fill=(255, 255, 255))
    # Random image placeholder
    draw.rectangle([left_x + 20, by + 70, left_x + panel_w - 20, by + 200], fill=(200, 200, 200))
    draw.text((left_x + 40, by + 130), "[broken image]", font=bad_body, fill=(100, 100, 100))

    # --- RIGHT: AFTER (clean/modern) ---
    draw.rounded_rectangle([right_x, panel_y, right_x + panel_w, panel_y + panel_h], radius=10, fill=(255, 255, 255))
    # "AFTER" label
    draw.text((right_x + 20, panel_y + 20), "AFTER", font=before_font, fill=palette["primary"])
    # Top accent bar
    draw.rectangle([right_x, panel_y, right_x + panel_w, panel_y + 4], fill=palette["primary"])

    # Clean nav
    clean_nav_font = _load_font(14, "sans-regular")
    draw.rectangle([right_x + 20, panel_y + 70, right_x + 40, panel_y + 90], fill=palette["accent"])
    draw.text((right_x + 50, panel_y + 73), "Brand", font=_load_font(16, "serif-bold"), fill=palette["dark"])
    nx = right_x + panel_w - 200
    for item in ["Home", "About", "Contact"]:
        draw.text((nx, panel_y + 75), item, font=clean_nav_font, fill=(120, 120, 130))
        nx += 60
    draw.rounded_rectangle([right_x + panel_w - 60, panel_y + 70, right_x + panel_w - 20, panel_y + 94], radius=4, fill=palette["primary"])
    draw.text((right_x + panel_w - 52, panel_y + 74), "Buy", font=_load_font(12, "sans-bold"), fill=palette["light"])

    # Clean hero: serif headline
    clean_h_font = _load_font(24, "serif-bold")
    clean_lines = _wrap_text("Your business, beautifully presented.", clean_h_font, draw, panel_w - 40)
    chy = panel_y + 120
    for cl in clean_lines[:3]:
        draw.text((right_x + 20, chy), cl, font=clean_h_font, fill=palette["dark"])
        chy += 30
    # Subtext
    clean_sub = _load_font(12, "sans-regular")
    sub_lines = _wrap_text("A clear message. A confident design. A website that converts.", clean_sub, draw, panel_w - 40)
    sy_ = chy + 10
    for sl in sub_lines[:2]:
        draw.text((right_x + 20, sy_), sl, font=clean_sub, fill=(140, 140, 150))
        sy_ += 16
    # CTA button
    draw.rounded_rectangle([right_x + 20, sy_ + 16, right_x + 180, sy_ + 56], radius=6, fill=palette["primary"])
    draw.text((right_x + 50, sy_ + 28), "Get Started", font=_load_font(13, "sans-bold"), fill=palette["light"])

    # Clean image placeholder (modern aspect)
    imy = sy_ + 80
    draw.rounded_rectangle([right_x + 20, imy, right_x + panel_w - 20, imy + 180], radius=8, fill=(240, 240, 245))
    draw.rounded_rectangle([right_x + 35, imy + 15, right_x + panel_w - 35, imy + 165], radius=4, fill=palette["accent"])

    # Bottom: clean footer
    draw.text((right_x + 20, panel_y + panel_h - 30), "\u00A9 2026 Brand  \u00B7  Redesigned by your freelancer",
              font=_load_font(11, "sans-regular"), fill=(160, 160, 170))


# ---------------------------------------------------------------------------
# Style 4: palette_typography — Color swatches with hex codes + font pairing.
# Looks like a designer's reference card. Positions you as a designer who
# thinks about color and type, not just slaps text on a background.
# ---------------------------------------------------------------------------

def _render_palette_typography(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")

    # Background: warm cream (designer's notebook feel)
    draw.rectangle([(0, 0), (size, size)], fill=(250, 247, 240))

    # Top label strip
    label_font = _load_font(22, "mono-bold")
    label = f"  {title.upper()}  \u00B7  {date.isoformat()}  \u00B7  DM FOR WEBSITE WORK"
    draw.rectangle([(0, 0), (size, 50)], fill=palette["primary"])
    draw.text((40, 14), label, font=label_font, fill=palette["accent"])

    # Headline (top, large serif)
    headline_font = _load_font(48, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 100)
    y = 80
    for line in lines[:3]:
        draw.text((50, y), line, font=headline_font, fill=palette["dark"])
        y += 56

    # --- Color palette section ---
    section_y = y + 30
    section_label_font = _load_font(20, "mono-bold")
    draw.text((50, section_y), "COLOR PALETTE", font=section_label_font, fill=palette["primary"])
    draw.line([(50, section_y + 32), (220, section_y + 32)], fill=palette["accent"], width=3)

    # 5 swatches in a row
    swatch_y = section_y + 60
    swatch_w = 180
    swatch_h = 180
    swatch_gap = 15
    swatches = [
        ("Primary",  palette["primary"], _rgb_to_hex(palette["primary"])),
        ("Accent",   palette["accent"],  _rgb_to_hex(palette["accent"])),
        ("Dark",     palette["dark"],    _rgb_to_hex(palette["dark"])),
        ("Light",    palette["light"],   _rgb_to_hex(palette["light"])),
        ("Mid Gray", (140, 140, 150),    "#8C8C96"),
    ]
    for i, (name, rgb, hexcode) in enumerate(swatches):
        sx = 50 + i * (swatch_w + swatch_gap)
        # Swatch (color block)
        draw.rounded_rectangle([sx, swatch_y, sx + swatch_w, swatch_y + swatch_h], radius=8, fill=rgb)
        # Name label below
        name_font = _load_font(16, "sans-bold")
        draw.text((sx, swatch_y + swatch_h + 12), name, font=name_font, fill=palette["dark"])
        # Hex code
        hex_font = _load_font(14, "mono-regular")
        draw.text((sx, swatch_y + swatch_h + 34), hexcode, font=hex_font, fill=(120, 120, 130))

    # --- Typography pairing section ---
    type_y = swatch_y + swatch_h + 80
    draw.text((50, type_y), "TYPOGRAPHY PAIRING", font=section_label_font, fill=palette["primary"])
    draw.line([(50, type_y + 32), (260, type_y + 32)], fill=palette["accent"], width=3)

    # Heading sample (serif)
    head_sample_font = _load_font(44, "serif-bold")
    draw.text((50, type_y + 60), "Elegant Heading", font=head_sample_font, fill=palette["dark"])
    head_label_font = _load_font(14, "mono-regular")
    draw.text((50, type_y + 110), "SERIF BOLD  \u00B7  for headlines", font=head_label_font, fill=(120, 120, 130))

    # Body sample (sans)
    body_sample_font = _load_font(18, "sans-regular")
    body_text = "Body text uses a clean sans-serif for readability. Pair a serif headline with a sans body for timeless, professional results."
    body_lines = _wrap_text(body_text, body_sample_font, draw, size - 100)
    by = type_y + 140
    for bl in body_lines[:3]:
        draw.text((50, by), bl, font=body_sample_font, fill=palette["dark"])
        by += 24
    draw.text((50, by + 10), "SANS REGULAR  \u00B7  for body", font=head_label_font, fill=(120, 120, 130))

    # Bottom accent bar
    draw.rectangle([(0, size - 30), (size, size)], fill=palette["accent"])


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


# ---------------------------------------------------------------------------
# Style 5: stats_callout — Big bold statistic + supporting text.
# Infographic feel. Good for "shock value" facts that grab attention.
# ---------------------------------------------------------------------------

def _render_stats_callout(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")

    # Background: palette primary
    draw.rectangle([(0, 0), (size, size)], fill=palette["primary"])

    # Top label
    label_font = _load_font(22, "mono-bold")
    label = f"  {title.upper()}"
    draw.rectangle([(0, 0), (size, 50)], fill=palette["dark"])
    draw.text((40, 14), label, font=label_font, fill=palette["accent"])

    # Try to extract a number from the headline to make it huge
    # If no number, fall back to displaying the headline as-is (smaller)
    import re as _re
    num_match = _re.search(r"(\d+(?:\.\d+)?%?)", headline)
    if num_match:
        big_num = num_match.group(1)
        # Replace the number in the headline with a placeholder for the supporting text
        supporting = headline.replace(big_num, "").strip(" .,-")
    else:
        big_num = None
        supporting = headline

    if big_num:
        # Huge number (centered, top half)
        big_font = _load_font(280, "sans-bold")
        bw = _text_width(draw, big_num, big_font)
        draw.text(((size - bw) // 2, 120), big_num, font=big_font, fill=palette["accent"])

        # Supporting text (below the number)
        if supporting:
            sup_font = _load_font(32, "sans-bold")
            sup_lines = _wrap_text(supporting, sup_font, draw, size - 120)
            sy = 440
            for sl in sup_lines[:3]:
                slw = _text_width(draw, sl, sup_font)
                draw.text(((size - slw) // 2, sy), sl, font=sup_font, fill=palette["light"])
                sy += 40
    else:
        # No number — just headline large
        h_font = _load_font(64, "sans-bold")
        h_lines = _wrap_text(headline, h_font, draw, size - 120)
        total_h = 80 * len(h_lines)
        sy = (size - total_h) // 2
        for line in h_lines:
            lw = _text_width(draw, line, h_font)
            draw.text(((size - lw) // 2, sy), line, font=h_font, fill=palette["accent"])
            sy += 80

    # Bottom CTA strip
    cta_y = size - 130
    draw.rectangle([(0, cta_y), (size, size)], fill=palette["dark"])
    cta_font = _load_font(28, "sans-bold")
    cta_text = "DM me to fix your website"
    ctw = _text_width(draw, cta_text, cta_font)
    draw.text(((size - ctw) // 2, cta_y + 30), cta_text, font=cta_font, fill=palette["accent"])
    date_font = _load_font(18, "mono-regular")
    dw = _text_width(draw, date.isoformat(), date_font)
    draw.text(((size - dw) // 2, cta_y + 75), date.isoformat(), font=date_font, fill=palette["light"])


# ---------------------------------------------------------------------------
# Style 6: component_grid — UI components arranged in a grid.
# Buttons, form fields, cards, badges. Shows you think about design systems.
# ---------------------------------------------------------------------------

def _render_component_grid(img, draw, topic, headline, date, palette):
    size = 1080
    title = topic.get("title", "Daily Tip")

    # Background: light
    draw.rectangle([(0, 0), (size, size)], fill=(250, 250, 252))

    # Top label strip
    label_font = _load_font(22, "mono-bold")
    label = f"  {title.upper()}  \u00B7  UI COMPONENTS  \u00B7  {date.isoformat()}"
    draw.rectangle([(0, 0), (size, 50)], fill=palette["primary"])
    draw.text((40, 14), label, font=label_font, fill=palette["accent"])

    # Headline (top, centered)
    headline_font = _load_font(38, "serif-bold")
    lines = _wrap_text(headline, headline_font, draw, size - 100)
    total_h = 46 * len(lines)
    y = 70 + (140 - total_h) // 2
    for line in lines[:3]:
        lw = _text_width(draw, line, headline_font)
        draw.text(((size - lw) // 2, y), line, font=headline_font, fill=palette["dark"])
        y += 46

    # Grid: 2 columns x 3 rows of component cards
    grid_y = 230
    grid_h = 700
    col_w = (size - 120) // 2 - 15
    row_h = (grid_h - 30) // 3 - 15

    # Card 1: Buttons (top-left)
    c1x, c1y = 40, grid_y
    draw.rounded_rectangle([c1x, c1y, c1x + col_w, c1y + row_h], radius=10, fill=(255, 255, 255))
    # Card title
    title_font = _load_font(16, "mono-bold")
    draw.text((c1x + 20, c1y + 18), "BUTTONS", font=title_font, fill=palette["primary"])
    # Primary button
    draw.rounded_rectangle([c1x + 20, c1y + 60, c1x + 220, c1y + 105], radius=8, fill=palette["primary"])
    draw.text((c1x + 60, c1y + 73), "Primary", font=_load_font(15, "sans-bold"), fill=palette["light"])
    # Secondary (outline)
    draw.rounded_rectangle([c1x + 20, c1y + 120, c1x + 220, c1y + 165], radius=8, outline=palette["primary"], width=2)
    draw.text((c1x + 55, c1y + 133), "Secondary", font=_load_font(15, "sans-bold"), fill=palette["primary"])
    # Accent button
    draw.rounded_rectangle([c1x + 20, c1y + 180, c1x + 220, c1y + 225], radius=8, fill=palette["accent"])
    draw.text((c1x + 70, c1y + 193), "Accent", font=_load_font(15, "sans-bold"), fill=palette["dark"])

    # Card 2: Form fields (top-right)
    c2x = size // 2 + 20
    c2y = grid_y
    draw.rounded_rectangle([c2x, c2y, c2x + col_w, c2y + row_h], radius=10, fill=(255, 255, 255))
    draw.text((c2x + 20, c2y + 18), "FORM FIELDS", font=title_font, fill=palette["primary"])
    # Field 1 (with label)
    lbl_font = _load_font(12, "sans-bold")
    draw.text((c2x + 20, c2y + 55), "EMAIL", font=lbl_font, fill=(120, 120, 130))
    draw.rounded_rectangle([c2x + 20, c2y + 75, c2x + col_w - 20, c2y + 110], radius=6, outline=(200, 200, 210), width=2)
    draw.text((c2x + 32, c2y + 84), "you@example.com", font=_load_font(14, "sans-regular"), fill=(170, 170, 180))
    # Field 2 (focused - accent border)
    draw.text((c2x + 20, c2y + 130), "PHONE", font=lbl_font, fill=(120, 120, 130))
    draw.rounded_rectangle([c2x + 20, c2y + 150, c2x + col_w - 20, c2y + 185], radius=6, outline=palette["primary"], width=2)
    draw.text((c2x + 32, c2y + 159), "+1 555 0100", font=_load_font(14, "sans-regular"), fill=palette["dark"])
    # Submit button
    draw.rounded_rectangle([c2x + 20, c2y + 205, c2x + col_w - 20, c2y + 245], radius=6, fill=palette["primary"])
    sw = _text_width(draw, "Submit", _load_font(14, "sans-bold"))
    draw.text((c2x + (col_w - sw) // 2, c2y + 219), "Submit", font=_load_font(14, "sans-bold"), fill=palette["light"])

    # Card 3: Cards (middle-left)
    c3x, c3y = 40, grid_y + row_h + 15
    draw.rounded_rectangle([c3x, c3y, c3x + col_w, c3y + row_h], radius=10, fill=(255, 255, 255))
    draw.text((c3x + 20, c3y + 18), "CARDS", font=title_font, fill=palette["primary"])
    # Mini card 1 (with image block + title + body)
    draw.rounded_rectangle([c3x + 20, c3y + 50, c3x + col_w - 20, c3y + 130], radius=8, fill=(245, 245, 250))
    draw.rounded_rectangle([c3x + 30, c3y + 60, c3x + 110, c3y + 120], radius=4, fill=palette["accent"])
    draw.text((c3x + 125, c3y + 65), "Project Title", font=_load_font(13, "sans-bold"), fill=palette["dark"])
    draw.text((c3x + 125, c3y + 85), "Short description here", font=_load_font(11, "sans-regular"), fill=(140, 140, 150))
    draw.text((c3x + 125, c3y + 105), "$1,200", font=_load_font(13, "sans-bold"), fill=palette["primary"])
    # Mini card 2
    draw.rounded_rectangle([c3x + 20, c3y + 145, c3x + col_w - 20, c3y + 225], radius=8, fill=(245, 245, 250))
    draw.rounded_rectangle([c3x + 30, c3y + 155, c3x + 110, c3y + 215], radius=4, fill=palette["primary"])
    draw.text((c3x + 125, c3y + 160), "Another Project", font=_load_font(13, "sans-bold"), fill=palette["dark"])
    draw.text((c3x + 125, c3y + 180), "Short description here", font=_load_font(11, "sans-regular"), fill=(140, 140, 150))
    draw.text((c3x + 125, c3y + 200), "$850", font=_load_font(13, "sans-bold"), fill=palette["primary"])

    # Card 4: Badges (middle-right)
    c4x = size // 2 + 20
    c4y = grid_y + row_h + 15
    draw.rounded_rectangle([c4x, c4y, c4x + col_w, c4y + row_h], radius=10, fill=(255, 255, 255))
    draw.text((c4x + 20, c4y + 18), "BADGES", font=title_font, fill=palette["primary"])
    # Badge row 1
    draw.rounded_rectangle([c4x + 20, c4y + 55, c4x + 130, c4y + 85], radius=12, fill=palette["primary"])
    draw.text((c4x + 38, c4y + 64), "New", font=_load_font(13, "sans-bold"), fill=palette["light"])
    draw.rounded_rectangle([c4x + 145, c4y + 55, c4x + 280, c4y + 85], radius=12, fill=palette["accent"])
    draw.text((c4x + 165, c4y + 64), "Featured", font=_load_font(13, "sans-bold"), fill=palette["dark"])
    # Badge row 2 (outline)
    draw.rounded_rectangle([c4x + 20, c4y + 100, c4x + 150, c4y + 130], radius=12, outline=palette["primary"], width=2)
    draw.text((c4x + 42, c4y + 109), "Sale", font=_load_font(13, "sans-bold"), fill=palette["primary"])
    draw.rounded_rectangle([c4x + 165, c4y + 100, c4x + 295, c4y + 130], radius=12, outline=palette["accent"], width=2)
    draw.text((c4x + 188, c4y + 109), "Limited", font=_load_font(13, "sans-bold"), fill=palette["accent"])
    # Badge row 3 (small dots + labels)
    draw.ellipse([c4x + 20, c4y + 155, c4x + 36, c4y + 171], fill=(60, 180, 100))
    draw.text((c4x + 45, c4y + 158), "Active", font=_load_font(13, "sans-regular"), fill=palette["dark"])
    draw.ellipse([c4x + 150, c4y + 155, c4x + 166, c4y + 171], fill=(220, 80, 80))
    draw.text((c4x + 175, c4y + 158), "Closed", font=_load_font(13, "sans-regular"), fill=palette["dark"])
    draw.ellipse([c4x + 20, c4y + 185, c4x + 36, c4y + 201], fill=palette["accent"])
    draw.text((c4x + 45, c4y + 188), "Pending", font=_load_font(13, "sans-regular"), fill=palette["dark"])

    # Card 5: Nav bar (bottom-left)
    c5x, c5y = 40, grid_y + 2 * (row_h + 15)
    draw.rounded_rectangle([c5x, c5y, c5x + col_w, c5y + row_h], radius=10, fill=(255, 255, 255))
    draw.text((c5x + 20, c5y + 18), "NAV BAR", font=title_font, fill=palette["primary"])
    # Sample nav bar
    draw.rectangle([c5x + 20, c5y + 55, c5x + col_w - 20, c5y + 105], fill=palette["primary"])
    draw.rectangle([c5x + 35, c5y + 70, c5x + 55, c5y + 90], fill=palette["accent"])
    draw.text((c5x + 65, c5y + 72), "Brand", font=_load_font(14, "serif-bold"), fill=palette["light"])
    draw.text((c5x + 200, c5y + 75), "Home", font=_load_font(12, "sans-regular"), fill=palette["light"])
    draw.text((c5x + 250, c5y + 75), "About", font=_load_font(12, "sans-regular"), fill=palette["light"])
    draw.text((c5x + 305, c5y + 75), "Contact", font=_load_font(12, "sans-regular"), fill=palette["light"])
    draw.rounded_rectangle([c5x + col_w - 110, c5y + 68, c5x + col_w - 35, c5y + 92], radius=4, fill=palette["accent"])
    draw.text((c5x + col_w - 95, c5y + 73), "CTA", font=_load_font(11, "sans-bold"), fill=palette["dark"])

    # Card 6: Typography scale (bottom-right)
    c6x = size // 2 + 20
    c6y = grid_y + 2 * (row_h + 15)
    draw.rounded_rectangle([c6x, c6y, c6x + col_w, c6y + row_h], radius=10, fill=(255, 255, 255))
    draw.text((c6x + 20, c6y + 18), "TYPE SCALE", font=title_font, fill=palette["primary"])
    draw.text((c6x + 20, c6y + 55), "Heading 1", font=_load_font(28, "serif-bold"), fill=palette["dark"])
    draw.text((c6x + 20, c6y + 95), "Heading 2", font=_load_font(22, "serif-bold"), fill=palette["dark"])
    draw.text((c6x + 20, c6y + 130), "Heading 3", font=_load_font(18, "serif-bold"), fill=palette["dark"])
    draw.text((c6x + 20, c6y + 160), "Body text", font=_load_font(14, "sans-regular"), fill=palette["dark"])
    draw.text((c6x + 20, c6y + 185), "Caption text", font=_load_font(11, "sans-regular"), fill=(140, 140, 150))


# ---------------------------------------------------------------------------
# Style registry + dispatcher
# ---------------------------------------------------------------------------

STYLES: list[tuple[str, Any]] = [
    ("browser_hero",       _render_browser_hero),
    ("phone_mobile",       _render_phone_mobile),
    ("before_after",       _render_before_after),
    ("palette_typography", _render_palette_typography),
    ("stats_callout",      _render_stats_callout),
    ("component_grid",     _render_component_grid),
]


def pick_style_index(date: dt.date, slot: int) -> int:
    """Rotate styles by (date_hash + slot) so each slot on the same day
    gets a different style. Same (date, slot) = same style (idempotent).
    """
    h = int(hashlib.sha256(date.isoformat().encode()).hexdigest(), 16)
    return (h + slot) % len(STYLES)


def render_image(out_path: Path, topic: dict[str, Any], headline: str,
                 date: dt.date, slot: int, style_index: int | None = None) -> str:
    """Render the day's image. Returns the style name used."""
    if Image is None:
        raise RuntimeError(f"Pillow is required to render images: {_PILLOW_ERR}")

    size = 1080
    title = topic.get("title", "Daily Tip")
    palette = _palette_for(title)

    if style_index is None:
        style_index = pick_style_index(date, slot)
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

def content_id(date: dt.date, slot: int) -> str:
    return f"{date.isoformat()}-{slot}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily Instagram content (4 slots/day).")
    parser.add_argument("--date", help="Override date as YYYY-MM-DD (defaults to today UTC).")
    parser.add_argument("--slot", type=int, choices=[1, 2, 3, 4],
                        help="Slot 1-4 (1=morning, 2=lunch, 3=evening, 4=night). Default: 1.")
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

    slot = args.slot or 1

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir = out_dir / "images"
    images_dir.mkdir(exist_ok=True)

    manifest_path = out_dir / f"{date.isoformat()}-{slot}.json"

    # IDEMPOTENCY: if manifest already exists and was AI-generated, reuse it
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("id") == content_id(date, slot) and existing.get("ai_generated"):
                print(f"[INFO] Manifest {manifest_path} already exists and is AI-generated. Reusing (idempotency).")
                image_path = images_dir / existing.get("image_filename", f"{date.isoformat()}-{slot}.png")
                if image_path.exists():
                    print(f"[INFO] Image already rendered: {image_path}")
                    return 0
                style_idx = existing.get("style_index")
                style_idx = int(style_idx) if style_idx is not None else None
                render_image(image_path, {"title": existing.get("topic", "")},
                             existing.get("image_headline", existing.get("fact", "")),
                             date, slot, style_index=style_idx)
                print(f"[INFO] Image re-rendered: {image_path}")
                return 0
        except (json.JSONDecodeError, OSError):
            pass

    print(f"[INFO] Generating content for {date.isoformat()} (slot {slot})")
    topic, fact, fact_index = pick_topic_and_fact(date, slot)
    print(f"[INFO] Topic: {topic['title']} (fact #{fact_index + 1})")
    print(f"[INFO] Seed fact: {fact}")

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

    image_filename = f"{date.isoformat()}-{slot}.png"
    image_path = images_dir / image_filename
    style_index = pick_style_index(date, slot)
    style_name = render_image(image_path, topic, image_headline, date, slot, style_index=style_index)
    print(f"[INFO] Image written: {image_path} ({image_path.stat().st_size} bytes)")
    print(f"[INFO] Style: {style_name} (index {style_index})")

    manifest = {
        "id": content_id(date, slot),
        "date": date.isoformat(),
        "slot": slot,
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
