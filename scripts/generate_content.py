"""
generate_content.py
-------------------
Deterministic daily content generator for the Instagram auto-poster.

Pipeline:
  1. Pick today's topic from config/topics.yaml (rotates by day-of-year).
  2. Build a caption + a short list of hashtags from a built-in fact library
     for that topic. (No external AI API required for v1; an AI_API_KEY hook
     is provided for future upgrade.)
  3. Render a deterministic 1080x1080 PNG using Pillow (gradient background +
     topic title + a one-line fact). The same date always yields the same image,
     so a workflow re-run is idempotent.
  4. Write content/<date>.json with {id, date, topic, caption, hashtags, image_file}.

Usage:
  python scripts/generate_content.py [--date YYYY-MM-DD] [--out-dir content]

The script only writes to local disk -- it never talks to the network, never
prints secrets, and is safe to run locally for testing.
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

# --- Optional dependency: Pillow. We import lazily so the script still loads
# --- even if Pillow is missing, but rendering will raise a clear error.
try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError as exc:  # pragma: no cover - exercised in CI
    Image = ImageDraw = ImageFont = None  # type: ignore
    _PILLOW_ERR = str(exc)
else:
    _PILLOW_ERR = None


# ---------------------------------------------------------------------------
# Topic rotation
# ---------------------------------------------------------------------------

# Built-in topic catalogue. Each topic has:
#   - title: short label
#   - facts: list of one-liner facts; we rotate by date so the same fact is
#            returned for a given (topic, day_index)
DEFAULT_TOPICS: list[dict[str, Any]] = [
    {
        "title": "Useful Tech Fact",
        "facts": [
            "The first webcam was invented at Cambridge University in 1991 to monitor a coffee pot.",
            "HTTP/3 runs over QUIC, which is built on UDP instead of TCP.",
            "The average modern smartphone has more computing power than the computers used for the Apollo 11 moon landing.",
            "SSDs can read data at over 7,000 MB/s, roughly 30x faster than a spinning hard drive.",
            "USB-C cables can carry up to 240W of power with Power Delivery 3.1.",
            "The '404' error code is named after room 404 at CERN, where the original web team worked.",
        ],
    },
    {
        "title": "Android Tip",
        "facts": [
            "Long-pressing a notification on Android lets you silence or customize it per-app.",
            "Gboard's clipboard manager keeps text you copy for up to 1 hour -- enable it from the toolbar.",
            "Android 14's Flash Notifications setting can blink your camera flash for incoming calls.",
            "You can run two copies of the same app with Android's 'Dual Messenger' / 'App Cloner' features.",
            "Developer Options > 'Smallest width' lets you fit more content on screen by tweaking DPI.",
            "Android's 'Nearby Share' works offline using Bluetooth + Wi-Fi Direct, no internet required.",
        ],
    },
    {
        "title": "AI Tool Highlight",
        "facts": [
            "Whisper by OpenAI can transcribe 1 hour of audio in under 2 minutes on a modern GPU.",
            "Stable Diffusion can run entirely offline on a 6GB VRAM GPU once the model is downloaded.",
            "LocalAI lets you serve OpenAI-compatible APIs from your own hardware, with no data leaving your machine.",
            "Ollama can run Llama 3.1 8B locally with as little as 8GB of RAM using 4-bit quantization.",
            "sentence-transformers can index 1M documents for semantic search in under 1GB of RAM.",
            "Hugging Face Hub hosts over 1 million open models -- you can clone any of them with git-lfs.",
        ],
    },
    {
        "title": "Programming Fact",
        "facts": [
            "Python's `else` clause runs after a `for` loop finishes without hitting `break`.",
            "In JavaScript, `typeof null === 'object'` is a long-standing bug that can't be fixed without breaking the web.",
            "Rust's borrow checker prevents data races at compile time, with zero runtime cost.",
            "Git was created by Linus Torvalds in 2005; he named it after himself ('git' is British slang for an unpleasant person).",
            "The `null` reference was called 'my billion-dollar mistake' by its inventor, Tony Hoare.",
            "UTF-8 was designed by Ken Thompson and Rob Pike in a single evening on a placemat over dinner.",
        ],
    },
    {
        "title": "Cybersecurity Tip",
        "facts": [
            "A password manager eliminates reuse -- pick one long master password and let it generate the rest.",
            "Enable hardware-based 2FA (a security key) for email and password manager accounts; SMS codes can be SIM-swapped.",
            "Check `haveibeenpwned.com` to see if your email appears in known data breaches.",
            "Update your router firmware at least twice a year -- many home routers never receive auto-updates.",
            "DNS-over-HTTPS (DoH) prevents your ISP from seeing which domains you look up.",
            "Disable macro execution by default in office apps; most document-based malware needs macros enabled.",
        ],
    },
    {
        "title": "Useful Website",
        "facts": [
            "archive.org has snapshots of the web going back to 1996 -- type any URL to see its history.",
            "wikipedia.org's 'Random Article' button is the rabbit hole of all rabbit holes.",
            "oa.mg lets you search 250M+ open-access research papers for free.",
            "news.ycombinator.com surfaces tech discussions before they reach mainstream media.",
            "remove.bg strips image backgrounds in one click, no signup needed for low-res output.",
            "excalidraw.com is a free hand-drawn-style whiteboard that runs in the browser.",
        ],
    },
    {
        "title": "Developer Trick",
        "facts": [
            "In VS Code, Ctrl+Shift+P opens the Command Palette -- almost every action is reachable from there.",
            "Git's `reflog` records every HEAD move; even a bad `reset --hard` is usually recoverable.",
            "`jq` can transform JSON in pipes: `curl -s url | jq '.data[] | .name'`.",
            "Python's `if __name__ == '__main__':` lets a file be both importable and runnable.",
            "SSH config file (~/.ssh/config) lets you alias hosts so `ssh prod` just works.",
            "Docker's `--restart unless-stopped` policy auto-restarts containers after reboots but not after manual stops.",
        ],
    },
]


def load_topics() -> list[dict[str, Any]]:
    """Load topics from config/topics.yaml if present, else use DEFAULT_TOPICS.

    The YAML file is optional -- we ship it as topics.example.yaml and copy it
    on first run. This keeps the script self-contained.
    """
    topics_path = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"
    if not topics_path.exists():
        return DEFAULT_TOPICS

    try:
        import yaml  # type: ignore
    except ImportError:
        # Fall back to defaults if PyYAML is missing
        print("[INFO] config/topics.yaml exists but PyYAML is not installed; using built-in topics")
        return DEFAULT_TOPICS

    with topics_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not data or "topics" not in data:
        print("[WARN] config/topics.yaml has no 'topics' key; using built-in topics")
        return DEFAULT_TOPICS
    return data["topics"]


def pick_topic_and_fact(date: dt.date) -> tuple[dict[str, Any], str, int]:
    """Rotate topics by day-of-year and pick a fact by hashing the date.

    Returns (topic, fact, fact_index).
    """
    topics = load_topics()
    if not topics:
        raise RuntimeError("No topics available")

    # Rotate topics daily
    day_of_year = date.timetuple().tm_yday
    topic_index = day_of_year % len(topics)
    topic = topics[topic_index]

    # Rotate facts within the topic by a stable hash of the date.
    # The same date always yields the same fact.
    date_hash = int(hashlib.sha256(date.isoformat().encode()).hexdigest(), 16)
    facts = topic.get("facts") or []
    if not facts:
        raise RuntimeError(f"Topic '{topic.get('title')}' has no facts")
    fact_index = date_hash % len(facts)
    return topic, facts[fact_index], fact_index


# ---------------------------------------------------------------------------
# Caption + hashtags
# ---------------------------------------------------------------------------

def build_caption(topic: dict[str, Any], fact: str) -> str:
    """Build a clean, non-spammy caption."""
    title = topic.get("title", "Daily Fact")
    return f"{title}\n\n{fact}\n\nFollow for a new useful fact every day."


def build_hashtags(topic: dict[str, Any]) -> list[str]:
    """3-5 relevant hashtags per post."""
    base = {
        "Useful Tech Fact": ["#technology", "#tech", "#techfacts"],
        "Android Tip": ["#android", "#androidtips", "#techhacks"],
        "AI Tool Highlight": ["#ai", "#aitools", "#machinelearning"],
        "Programming Fact": ["#programming", "#coding", "#developer"],
        "Cybersecurity Tip": ["#cybersecurity", "#infosec", "#securitytips"],
        "Useful Website": ["#websites", "#tools", "#productivity"],
        "Developer Trick": ["#devtips", "#programming", "#tools"],
    }
    title = topic.get("title", "")
    tags = base.get(title, ["#tech", "#daily", "#facts"])
    # Pull tags from the topic if explicitly provided
    if topic.get("hashtags"):
        tags = list(topic["hashtags"])[:5]
    return tags


# ---------------------------------------------------------------------------
# Image rendering (deterministic)
# ---------------------------------------------------------------------------

# Color palette per topic -- used for the gradient background.
# Chosen to be Instagram-friendly (high contrast, not too saturated).
TOPIC_COLORS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "Useful Tech Fact":     ((20, 30, 80), (80, 30, 130)),
    "Android Tip":          ((10, 80, 60), (40, 160, 120)),
    "AI Tool Highlight":    ((80, 20, 80), (180, 30, 120)),
    "Programming Fact":     ((30, 30, 50), (60, 100, 180)),
    "Cybersecurity Tip":    ((60, 10, 30), (140, 30, 60)),
    "Useful Website":       ((20, 60, 80), (60, 140, 160)),
    "Developer Trick":      ((40, 20, 80), (120, 60, 180)),
}


def _find_font(size: int) -> Any:
    """Try to locate a TTF font on the runner; fall back to default bitmap."""
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
    # Last resort: PIL's default (bitmap, ugly but works)
    return ImageFont.load_default()


def _wrap_text(text: str, font: Any, draw: Any, max_width: int) -> list[str]:
    """Greedy word-wrap to fit max_width pixels."""
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


def render_image(out_path: Path, topic: dict[str, Any], fact: str, date: dt.date) -> None:
    """Render a 1080x1080 PNG (Instagram square format).

    Layout:
      - Vertical gradient background
      - Topic title (top, large, bold)
      - Fact text (center, wrapped)
      - Date footer
    """
    if Image is None:
        raise RuntimeError(f"Pillow is required to render images: {_PILLOW_ERR}")

    size = 1080
    title = topic.get("title", "Daily Fact")
    color_pair = TOPIC_COLORS.get(title, ((30, 30, 60), (60, 60, 120)))
    top_color, bottom_color = color_pair

    # Build the gradient by interpolating row-by-row (slow but only 1080 rows)
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

    # Title
    title_font = _find_font(64)
    title_x = 80
    title_y = 120
    draw.text((title_x, title_y), title, font=title_font, fill=(255, 255, 255))

    # Accent line under title
    draw.rectangle([(title_x, title_y + 90), (title_x + 200, title_y + 96)], fill=(255, 255, 255))

    # Fact text (wrapped, centered horizontally, vertically below title)
    fact_font = _find_font(52)
    max_text_width = size - 160  # 80px margin each side
    lines = _wrap_text(fact, fact_font, draw, max_text_width)

    line_height = 70
    total_height = line_height * len(lines)
    y_start = (size - total_height) // 2 + 60  # nudge slightly below center
    for i, line in enumerate(lines):
        draw.text((title_x, y_start + i * line_height), line, font=fact_font, fill=(240, 240, 240))

    # Footer: date + brand
    footer_font = _find_font(32)
    footer_text = f"{date.isoformat()}  -  Daily Auto-Posted Fact"
    draw.text((title_x, size - 100), footer_text, font=footer_font, fill=(220, 220, 220))

    # Save as PNG (lossless, large but always accepted by Instagram)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)


# ---------------------------------------------------------------------------
# Content ID (for idempotency)
# ---------------------------------------------------------------------------

def content_id(date: dt.date) -> str:
    """A deterministic ID for the day's content. The same date always yields the
    same ID, so a workflow retry will detect 'already published' and skip.
    """
    return date.isoformat()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic daily Instagram content.")
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

    print(f"[INFO] Generating content for {date.isoformat()}")

    topic, fact, fact_index = pick_topic_and_fact(date)
    print(f"[INFO] Topic: {topic['title']} (fact #{fact_index + 1})")

    caption = build_caption(topic, fact)
    hashtags = build_hashtags(topic)
    full_caption = caption + "\n\n" + " ".join(hashtags)

    image_filename = f"{date.isoformat()}.png"
    image_path = images_dir / image_filename
    render_image(image_path, topic, fact, date)
    print(f"[INFO] Image written: {image_path} ({image_path.stat().st_size} bytes)")

    manifest = {
        "id": content_id(date),
        "date": date.isoformat(),
        "topic": topic["title"],
        "fact": fact,
        "caption": full_caption,
        "hashtags": hashtags,
        "image_file": str(image_path.relative_to(out_dir.parent)) if out_dir.parent.exists() else str(image_path),
        "image_filename": image_filename,
    }

    manifest_path = out_dir / f"{date.isoformat()}.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
    print(f"[INFO] Manifest written: {manifest_path}")
    print(f"[INFO] Caption preview:\n{full_caption}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
