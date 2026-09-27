"""
publish_instagram.py
--------------------
Publishes today's content to Instagram via Composio's v3 REST API.

Verified Composio v3 flow (Sept 2026, composio-core 0.7.x):
  Step 1: POST /api/v3/tools/execute/INSTAGRAM_CREATE_MEDIA_CONTAINER
          args: { ig_user_id, image_url, caption }
          -> returns { data: { id: <creation_id> } }
  Step 2: POST /api/v3/tools/execute/INSTAGRAM_CREATE_POST
          args: { ig_user_id, creation_id }
          -> returns { data: { id: <media_id> } }

Auth: x-api-key header (Composio API key).
Body wrapper: { arguments, connected_account_id, user_id }

Idempotency:
  Before publishing, we read content/history.json. If today's content ID
  already has a 'published' entry, we skip and exit 0.

The image must be reachable by Instagram's servers (a public URL). We expect
the manifest to contain an `image_url` field; the workflow is responsible
for pushing the generated image to a public location (raw.githubusercontent.com
on a public repo, or another host) and writing that URL into the manifest
before this script runs.

Usage:
  python scripts/publish_instagram.py --manifest content/<date>.json
                                       [--history content/history.json]
                                       [--dry-run]

Required environment variables:
  COMPOSIO_API_KEY             - Composio account API key
  COMPOSIO_CONNECTED_ACCOUNT_ID- ID of the connected Instagram account in Composio
  COMPOSIO_USER_ID             - Composio user ID (often "default")
  INSTAGRAM_USER_ID            - Instagram Business account numeric ID

Optional:
  AI_API_KEY                   - Reserved for future AI caption generation
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

# ---------------------------------------------------------------------------
# Composio v3 REST constants (verified from public usage in live repos)
# ---------------------------------------------------------------------------

COMPOSIO_API_BASE = "https://backend.composio.dev/api/v3"
COMPOSIO_EXECUTE_URL = COMPOSIO_API_BASE + "/tools/execute/{action}"
COMPOSIO_CONNECTED_ACCOUNT_URL = COMPOSIO_API_BASE + "/connected_accounts/{id}"

ACTION_CREATE_CONTAINER = "INSTAGRAM_CREATE_MEDIA_CONTAINER"
ACTION_CREATE_POST = "INSTAGRAM_CREATE_POST"
ACTION_GET_POST_STATUS = "INSTAGRAM_GET_POST_STATUS"

HTTP_TIMEOUT_SECONDS = 90

# Instagram Graph API: after creating a media container, the image is downloaded
# and processed asynchronously. You CANNOT call media_publish until status_code
# == "FINISHED". Typical wait: 3-15 seconds for a small PNG.
READINESS_POLL_INTERVAL_SECONDS = 3
READINESS_MAX_WAIT_SECONDS = 90
READY_STATUS = "FINISHED"
ERROR_STATUSES = {"ERROR", "EXPIRED"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ComposioError(RuntimeError):
    """Raised when Composio returns an explicit unsuccessful response."""

    def __init__(self, message: str, *, action: str | None = None,
                 status: int | None = None, body: Any | None = None) -> None:
        super().__init__(message)
        self.action = action
        self.status = status
        self.body = body


class ComposioConfigError(RuntimeError):
    """Raised when required environment variables are missing."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REQUIRED_ENV = [
    "COMPOSIO_API_KEY",
    "COMPOSIO_CONNECTED_ACCOUNT_ID",
    "COMPOSIO_USER_ID",
    "INSTAGRAM_USER_ID",
]


def load_config() -> dict[str, str]:
    """Read required env vars; raise a clear error if any are missing.

    NEVER prints the values -- only the names.
    """
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise ComposioConfigError(
            "Missing required environment variables: " + ", ".join(missing)
            + "\nSet them as GitHub Actions secrets."
        )
    return {name: os.environ[name] for name in REQUIRED_ENV}


# ---------------------------------------------------------------------------
# Composio transport
# ---------------------------------------------------------------------------

def _sanitize_for_log(text: str, max_len: int = 600) -> str:
    """Trim and redact obvious secret-like fields before logging."""
    if not isinstance(text, str):
        text = str(text)
    # Redact common secret-bearing JSON fields if present
    for key in ("access_token", "api_key", "x-api-key", "token", "refresh_token", "client_secret"):
        if key in text.lower():
            # very coarse: just replace the value after the key
            import re
            text = re.sub(
                rf'("{key}"\s*:\s*")([^"]+)(")',
                r'\1<redacted>\3',
                text,
                flags=re.IGNORECASE,
            )
    if len(text) > max_len:
        text = text[:max_len] + "...<truncated>"
    return text


def call_composio(action: str, arguments: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    """Execute a Composio v3 action. Returns the parsed JSON `data` field on success.

    Raises ComposioError on:
      - HTTP non-2xx
      - HTTP 2xx but `successful == false`
      - Missing `data` field
    """
    url = COMPOSIO_EXECUTE_URL.format(action=action)
    body = {
        "arguments": arguments,
        "connected_account_id": config["COMPOSIO_CONNECTED_ACCOUNT_ID"],
        "user_id": config["COMPOSIO_USER_ID"],
    }
    headers = {
        "x-api-key": config["COMPOSIO_API_KEY"],
        "Content-Type": "application/json",
    }

    print(f"[INFO] Composio: calling {action}")
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ComposioError(
            f"Network error calling {action}: {exc}",
            action=action,
        ) from exc

    if resp.status_code >= 500:
        # Server-side / transient; surface status but don't dump body (may contain token in error)
        raise ComposioError(
            f"Composio returned HTTP {resp.status_code} for {action} (server error, safe to retry)",
            action=action, status=resp.status_code,
        )
    if resp.status_code >= 400:
        # Client-side error: log a redacted snippet to help diagnose
        snippet = _sanitize_for_log(resp.text or "")
        raise ComposioError(
            f"Composio returned HTTP {resp.status_code} for {action}: {snippet}",
            action=action, status=resp.status_code,
        )

    try:
        result = resp.json()
    except ValueError as exc:
        raise ComposioError(
            f"Composio returned non-JSON response for {action}",
            action=action,
        ) from exc

    # Composio v3 response shape: { successful: bool, data: {...}, error?: ... }
    if isinstance(result, dict) and result.get("successful") is False:
        err = result.get("error") or (result.get("data") or {}).get("message") or "Unknown error"
        raise ComposioError(
            f"Composio action {action} was unsuccessful: {_sanitize_for_log(str(err))}",
            action=action, body=result,
        )

    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        raise ComposioError(
            f"Composio action {action} returned malformed response (no `data` object)",
            action=action, body=result,
        )

    print(f"[INFO] Composio: {action} OK")
    return data


def verify_connected_account(config: dict[str, str]) -> None:
    """Smoke-test: GET the connected account. Fails fast on bad creds.

    Does NOT print the response body (may contain token metadata).
    """
    url = COMPOSIO_CONNECTED_ACCOUNT_URL.format(id=config["COMPOSIO_CONNECTED_ACCOUNT_ID"])
    headers = {"x-api-key": config["COMPOSIO_API_KEY"]}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise ComposioError(f"Network error verifying connected account: {exc}") from exc

    if resp.status_code == 404:
        raise ComposioError(
            "Composio connected account not found (404). "
            "Check COMPOSIO_CONNECTED_ACCOUNT_ID and that the Instagram account is connected "
            "in your Composio dashboard."
        )
    if resp.status_code == 401:
        raise ComposioError(
            "Composio API key rejected (401). Verify COMPOSIO_API_KEY is correct."
        )
    if resp.status_code >= 400:
        raise ComposioError(
            f"Composio connected account verification failed (HTTP {resp.status_code})."
        )
    print("[INFO] Composio: connected account verified")


# ---------------------------------------------------------------------------
# Container readiness polling (CRITICAL — Instagram needs time to fetch image)
# ---------------------------------------------------------------------------

def wait_until_ready(creation_id: str, config: dict[str, str]) -> None:
    """Poll INSTAGRAM_GET_POST_STATUS until status_code == 'FINISHED'.

    Instagram Graph API requires this between creating a container and
    publishing it. Without it, you get error 9007 / subcode 2207027:
        "Media ID is not available -- The media is not ready for publishing"

    Raises ComposioError on ERROR/EXPIRED status or timeout.
    """
    deadline = time.monotonic() + READINESS_MAX_WAIT_SECONDS
    attempt = 0
    last_status = None
    while time.monotonic() < deadline:
        attempt += 1
        data = call_composio(
            ACTION_GET_POST_STATUS,
            arguments={"creation_id": str(creation_id)},
            config=config,
        )
        # Composio wraps IG Graph API fields. Try multiple known key names.
        status = (
            data.get("status_code")
            or data.get("status")
            or (data.get("data") or {}).get("status_code")
            or "UNKNOWN"
        )
        last_status = status
        print(f"[INFO] Container {creation_id} status (attempt {attempt}): {status}")
        if status == READY_STATUS:
            print(f"[INFO] Container ready after ~{attempt * READINESS_POLL_INTERVAL_SECONDS}s")
            return
        if status in ERROR_STATUSES:
            raise ComposioError(
                f"Instagram media container {creation_id} entered {status} state. "
                f"Full response: {_sanitize_for_log(json.dumps(data))}",
                action=ACTION_GET_POST_STATUS,
            )
        time.sleep(READINESS_POLL_INTERVAL_SECONDS)
    raise ComposioError(
        f"Timed out after {READINESS_MAX_WAIT_SECONDS}s waiting for container "
        f"{creation_id} to become FINISHED (last status: {last_status})",
        action=ACTION_GET_POST_STATUS,
    )


# ---------------------------------------------------------------------------
# History (idempotency)
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
        # Corrupt history file: don't crash, start fresh but warn
        print(f"[WARN] {path} is unreadable; starting with empty history")
        return {"publications": []}


def is_already_published(history: dict[str, Any], content_id: str) -> bool:
    for entry in history.get("publications", []):
        if entry.get("id") == content_id and entry.get("status") == "published":
            return True
    return False


def record_publication(history: dict[str, Any], path: Path, manifest: dict[str, Any],
                       media_id: str | None, status: str, detail: str = "") -> None:
    entry = {
        "id": manifest["id"],
        "date": manifest["date"],
        "slot": manifest.get("slot"),
        "topic": manifest.get("topic", ""),
        "fact_index": manifest.get("fact_index"),
        "seed_fact": manifest.get("seed_fact", ""),
        "fresh_tip": manifest.get("fresh_tip", False),
        "image_style": manifest.get("image_style", ""),
        "image_headline": manifest.get("image_headline", ""),
        "status": status,
        "media_id": media_id,
        "image_url": manifest.get("image_url"),
        "detail": detail,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    history.setdefault("publications", []).append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(history, fh, indent=2, ensure_ascii=False)
    print(f"[INFO] History updated: {path} ({status})")


# ---------------------------------------------------------------------------
# Publishing flow
# ---------------------------------------------------------------------------

def publish(manifest: dict[str, Any], config: dict[str, str]) -> str:
    """Execute the 2-step Composio publish flow. Returns the published media ID."""
    image_url = manifest.get("image_url")
    caption = manifest.get("caption")
    ig_user_id = config["INSTAGRAM_USER_ID"]

    if not image_url:
        raise ComposioError("Manifest is missing `image_url`. The workflow must publish the "
                            "image to a public URL before calling this script.")
    if not caption:
        raise ComposioError("Manifest is missing `caption`.")

    print(f"[INFO] Image URL: {image_url}")
    print(f"[INFO] Caption length: {len(caption)} chars")
    print(f"[INFO] Instagram user ID: {ig_user_id}")

    # Step 1: create media container
    container_data = call_composio(
        ACTION_CREATE_CONTAINER,
        arguments={
            "ig_user_id": ig_user_id,
            "image_url": image_url,
            "caption": caption,
        },
        config=config,
    )
    creation_id = container_data.get("id") or container_data.get("container_id")
    if not creation_id:
        raise ComposioError(
            f"INSTAGRAM_CREATE_MEDIA_CONTAINER did not return an id. "
            f"Response keys: {list(container_data.keys())}"
        )
    print(f"[INFO] Container created: {creation_id}")

    # CRITICAL: Instagram needs a few seconds to download + process the image
    # before it can be published. Calling INSTAGRAM_CREATE_POST immediately
    # fails with error 9007 ("media is not ready for publishing").
    print("[INFO] Waiting for container to finish processing...")
    wait_until_ready(str(creation_id), config)

    # Step 2: publish (container is now FINISHED)
    publish_data = call_composio(
        ACTION_CREATE_POST,
        arguments={
            "ig_user_id": ig_user_id,
            "creation_id": str(creation_id),
        },
        config=config,
    )
    media_id = publish_data.get("id") or publish_data.get("media_id")
    if not media_id:
        # Some Composio responses nest under different keys -- surface what we got
        raise ComposioError(
            f"INSTAGRAM_CREATE_POST did not return an id. "
            f"Response keys: {list(publish_data.keys())}"
        )
    print(f"[INFO] Published! Media ID: {media_id}")
    return str(media_id)


def publish_story(manifest: dict[str, Any], config: dict[str, str]) -> str | None:
    """Publish the same image as an Instagram Story.

    NOTE: As of Sept 2026, the Instagram Graph API does NOT support publishing
    Stories with media_type=STORY for business accounts via the standard
    /media endpoint. The API returns error 2207023 "Unknown media type".
    Stories can only be published from the mobile app, or via the Story
    specific endpoint which requires additional permissions.

    This function attempts the publish but expects it to fail gracefully.
    If Instagram ever adds Story support, this will start working automatically.
    """
    image_url = manifest.get("image_url")
    ig_user_id = config["INSTAGRAM_USER_ID"]
    if not image_url:
        return None

    print("[INFO] Attempting Story publish (may fail — IG API has limited Story support)...")
    try:
        # Try with media_type=STORY (documented but currently rejected by IG)
        container_data = call_composio(
            ACTION_CREATE_CONTAINER,
            arguments={
                "ig_user_id": ig_user_id,
                "image_url": image_url,
                "media_type": "STORY",
            },
            config=config,
        )
        creation_id = container_data.get("id") or container_data.get("container_id")
        if not creation_id:
            print("[WARN] Story container creation returned no id. Skipping Story.")
            return None

        print(f"[INFO] Story container created: {creation_id}")
        wait_until_ready(str(creation_id), config)

        publish_data = call_composio(
            ACTION_CREATE_POST,
            arguments={
                "ig_user_id": ig_user_id,
                "creation_id": str(creation_id),
            },
            config=config,
        )
        story_media_id = publish_data.get("id") or publish_data.get("media_id")
        if story_media_id:
            print(f"[INFO] Story published! Media ID: {story_media_id}")
            return str(story_media_id)
        return None
    except ComposioError as exc:
        # Expected: Instagram returns "Unknown media type" for STORY.
        # Non-fatal: the feed post already succeeded.
        print(f"[INFO] Story publish skipped (IG API limitation): {str(exc)[:100]}")
        print("[INFO] Stories must be published manually from the Instagram app for now.")
        return None


def publish_carousel(manifest: dict[str, Any], config: dict[str, str],
                     children_image_urls: list[str]) -> str:
    """Publish a carousel post (multiple swipeable images).
    children_image_urls: list of 2-10 public image URLs for the carousel slides.

    Flow:
      1. For each child image: CREATE_MEDIA_CONTAINER with is_carousel_item=true
      2. CREATE_CAROUSEL_CONTAINER with children=[creation_id, ...]
      3. Wait for readiness
      4. CREATE_POST with the carousel container's creation_id
    """
    ig_user_id = config["INSTAGRAM_USER_ID"]
    caption = manifest.get("caption", "")
    if not caption:
        raise ComposioError("Manifest is missing `caption` for carousel.")
    if len(children_image_urls) < 2 or len(children_image_urls) > 10:
        raise ComposioError(f"Carousel needs 2-10 images, got {len(children_image_urls)}.")

    print(f"[INFO] Publishing carousel with {len(children_image_urls)} slides...")

    # Step 1: create a container for each child image
    child_creation_ids: list[str] = []
    for i, img_url in enumerate(children_image_urls):
        print(f"[INFO] Creating carousel child container {i+1}/{len(children_image_urls)}...")
        child_data = call_composio(
            ACTION_CREATE_CONTAINER,
            arguments={
                "ig_user_id": ig_user_id,
                "image_url": img_url,
                "is_carousel_item": True,
            },
            config=config,
        )
        child_id = child_data.get("id") or child_data.get("container_id")
        if not child_id:
            raise ComposioError(f"Carousel child {i+1} container creation returned no id.")
        child_creation_ids.append(str(child_id))
        print(f"[INFO] Child {i+1} container: {child_id}")

    # Step 2: create the carousel container
    print(f"[INFO] Creating carousel container with {len(child_creation_ids)} children...")
    carousel_data = call_composio(
        "INSTAGRAM_CREATE_CAROUSEL_CONTAINER",
        arguments={
            "ig_user_id": ig_user_id,
            "children": child_creation_ids,
            "caption": caption,
        },
        config=config,
    )
    carousel_creation_id = carousel_data.get("id") or carousel_data.get("container_id")
    if not carousel_creation_id:
        raise ComposioError("Carousel container creation returned no id.")
    print(f"[INFO] Carousel container: {carousel_creation_id}")

    # Step 3: wait for readiness
    wait_until_ready(str(carousel_creation_id), config)

    # Step 4: publish
    publish_data = call_composio(
        ACTION_CREATE_POST,
        arguments={
            "ig_user_id": ig_user_id,
            "creation_id": str(carousel_creation_id),
        },
        config=config,
    )
    media_id = publish_data.get("id") or publish_data.get("media_id")
    if not media_id:
        raise ComposioError("Carousel CREATE_POST did not return an id.")
    print(f"[INFO] Carousel published! Media ID: {media_id}")
    return str(media_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a content manifest to Instagram via Composio.")
    parser.add_argument("--manifest", required=True, help="Path to the content manifest JSON.")
    parser.add_argument("--history", default="content/history.json", help="Path to history.json for idempotency.")
    parser.add_argument("--dry-run", action="store_true", help="Skip the actual Composio call; just validate.")
    parser.add_argument("--also-story", action="store_true", default=True,
                        help="Also publish as Instagram Story (default: True).")
    parser.add_argument("--no-story", action="store_true", help="Skip Story publishing.")
    parser.add_argument("--carousel-children", help="Comma-separated list of additional image URLs for carousel mode.")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    with manifest_path.open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    print(f"[INFO] Loaded manifest: {manifest_path}")
    print(f"[INFO] Content ID: {manifest.get('id')}")

    history_path = Path(args.history).resolve()
    history = load_history(history_path)

    if is_already_published(history, manifest["id"]):
        print(f"[INFO] Content {manifest['id']} was already published. Skipping (idempotency).")
        return 0

    if args.dry_run:
        print("[INFO] Dry-run mode: skipping Composio call.")
        print(f"[INFO] image_url: {manifest.get('image_url', '<missing>')}")
        print(f"[INFO] caption preview: {(manifest.get('caption') or '')[:120]}...")
        record_publication(history, history_path, manifest, media_id=None,
                           status="dry_run", detail="Dry-run validation only")
        return 0

    config = load_config()
    verify_connected_account(config)

    # Carousel mode: multiple images
    if args.carousel_children:
        children_urls = [u.strip() for u in args.carousel_children.split(",") if u.strip()]
        # Prepend the manifest's own image_url as slide 1
        all_urls = [manifest["image_url"]] + children_urls
        try:
            media_id = publish_carousel(manifest, config, all_urls)
            story_media_id = None
            if args.also_story and not args.no_story:
                story_media_id = publish_story(manifest, config)
        except ComposioError as exc:
            print(f"[ERROR] Carousel publish failed: {exc}", file=sys.stderr)
            record_publication(history, history_path, manifest, media_id=None,
                               status="failed", detail=str(exc))
            return 1
        record_publication(history, history_path, manifest, media_id=media_id,
                           status="published",
                           detail=f"Carousel ({len(all_urls)} slides) via Composio" +
                                  (f" + Story {story_media_id}" if story_media_id else ""))
        print("[INFO] Done.")
        return 0

    # Standard single-image publish
    try:
        media_id = publish(manifest, config)
    except ComposioError as exc:
        print(f"[ERROR] Publish failed: {exc}", file=sys.stderr)
        record_publication(history, history_path, manifest, media_id=None,
                           status="failed", detail=str(exc))
        return 1

    # Also publish as Story (default behavior)
    story_media_id = None
    if args.also_story and not args.no_story:
        story_media_id = publish_story(manifest, config)

    detail = "Published via Composio"
    if story_media_id:
        detail += f" + Story {story_media_id}"
    record_publication(history, history_path, manifest, media_id=media_id,
                       status="published", detail=detail)
    print("[INFO] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
