# Instagram Auto Poster

GitHub Actions automatically generates and publishes daily Instagram content using Composio.

---

## What it does

Once configured, this repository runs on a schedule (default: **12:30 UTC every day**) and:

1. Picks today's topic from `config/topics.yaml` (rotates daily across 7 topics).
2. Generates a deterministic 1080x1080 PNG image (gradient + topic title + a one-line fact) using Pillow.
3. Builds a short, useful caption with 3 relevant hashtags.
4. Commits the image to the repo so it's reachable at a public raw URL.
5. Calls **Composio's v3 Instagram API** to publish the image:
   - `INSTAGRAM_CREATE_MEDIA_CONTAINER` → returns `creation_id`
   - `INSTAGRAM_CREATE_POST` → returns published media ID
6. Records the publication in `content/history.json` (idempotency — re-runs don't double-post).

The workflow also supports **manual execution** with optional `date_override` and `dry_run` inputs.

---

## Architecture

```
GitHub Actions (cron 12:30 UTC + workflow_dispatch)
        │
        ▼
scripts/generate_content.py
   • Pick topic by day-of-year
   • Pick fact by SHA-256 of date
   • Render 1080x1080 PNG (Pillow)
   • Write content/<date>.json manifest
        │
        ▼
git commit content/images/<date>.png  →  public raw URL
        │
        ▼
scripts/publish_instagram.py
   • Read manifest + content/history.json
   • Skip if already published (idempotency)
   • POST https://backend.composio.dev/api/v3/tools/execute/INSTAGRAM_CREATE_MEDIA_CONTAINER
   • POST https://backend.composio.dev/api/v3/tools/execute/INSTAGRAM_CREATE_POST
   • Record result in history.json
        │
        ▼
git commit content/history.json + content/<date>.json
        │
        ▼
Instagram post is live
```

---

## One-time setup

### 1. GitHub (already done)

The repository `instagram-auto-poster` is public (so Instagram can fetch images from `raw.githubusercontent.com`). The workflow file `.github/workflows/instagram.yml` is already in place.

### 2. Composio account

1. Sign up at <https://composio.dev> (free tier is enough for one daily post).
2. From the dashboard, copy your **API key** — this becomes the `COMPOSIO_API_KEY` secret.
3. Note your **user ID** — usually `"default"` for personal accounts. This becomes `COMPOSIO_USER_ID`.

### 3. Connect your Instagram account to Composio

Composio's Instagram toolkit uses the **Instagram Graph API**, which requires:

- An **Instagram Business or Creator account** (personal accounts are NOT supported).
- A **Facebook Page** linked to that Instagram account.
- A Composio-managed OAuth connection.

To connect:

1. In Composio, go to **Connected Accounts → Add → Instagram**.
2. Follow the OAuth flow (Facebook login → select Page → grant `instagram_basic`, `instagram_content_publish`, `pages_show_list`).
3. After completion, copy the **connected account ID** from the URL or list — this becomes `COMPOSIO_CONNECTED_ACCOUNT_ID`.

### 4. Get your Instagram Business account ID

This is the **numeric Instagram user ID** of the Business/Creator account (NOT your username). To find it:

- Open the Instagram Graph API Explorer or
- Use Composio's `INSTAGRAM_GET_BUSINESS_ACCOUNT` action against your Facebook Page, or
- Look in your Facebook Page's linked Instagram section.

This becomes the `INSTAGRAM_USER_ID` secret.

### 5. Configure GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Required | Example | Notes |
|---|---|---|---|
| `COMPOSIO_API_KEY` | ✅ | `csk_...` | From Composio dashboard |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | ✅ | `ca_abc123...` | From Composio → Connected Accounts |
| `COMPOSIO_USER_ID` | ✅ | `default` | Often `"default"` for personal accounts |
| `INSTAGRAM_USER_ID` | ✅ | `17841405822329911` | Numeric IG Business account ID |
| `AI_API_KEY` | ❌ | — | Reserved for future AI caption generation |

**Never** put these values in `instagram.yml`, `*.json`, `*.py`, or any committed file. The workflow references them as `${{ secrets.NAME }}` so they're injected at runtime and masked in logs.

### 6. Workflow schedule

The default cron is `30 12 * * *` (12:30 UTC daily). Edit `.github/workflows/instagram.yml` to change it. GitHub Actions cron is **best-effort** and can run late during high load — that's expected.

---

## Manual run

GitHub → **Actions** tab → **Instagram Automation** → **Run workflow** → optionally set `date_override` (YYYY-MM-DD) or `dry_run` (true to validate without publishing) → click **Run workflow**.

---

## Configuration reference

### Environment variables (set as GitHub Secrets)

| Variable | Purpose |
|---|---|
| `COMPOSIO_API_KEY` | Authenticates calls to `backend.composio.dev/api/v3` |
| `COMPOSIO_CONNECTED_ACCOUNT_ID` | Identifies which IG account to publish to |
| `COMPOSIO_USER_ID` | Composio-side user ID (usually `"default"`) |
| `INSTAGRAM_USER_ID` | Instagram Business account numeric ID (passed to Graph API) |
| `AI_API_KEY` | Reserved — not used in v1 |

### Repo files you might want to edit

| File | Purpose |
|---|---|
| `config/topics.yaml` | Add/edit topics, facts, hashtags |
| `.github/workflows/instagram.yml` | Schedule, dry-run, date override |
| `requirements.txt` | Python deps (keep small) |

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| `Missing required environment variables` | A secret is not set | Go to Settings → Secrets and add the missing one |
| `Composio API key rejected (401)` | Wrong `COMPOSIO_API_KEY` | Re-copy from Composio dashboard |
| `Composio connected account not found (404)` | Wrong `COMPOSIO_CONNECTED_ACCOUNT_ID` or IG account disconnected | Re-connect in Composio dashboard, update secret |
| `HTTP 400 ... image_url` | Image not publicly reachable or invalid format | Confirm `raw.githubusercontent.com/.../<date>.png` opens in a browser; ensure image is 1080x1080 PNG/JPEG |
| `HTTP 400 ... ig_user_id` | Wrong `INSTAGRAM_USER_ID` or account is not Business | Convert IG account to Business in the IG app settings; re-fetch the numeric ID |
| `INSTAGRAM_CREATE_MEDIA_CONTAINER did not return an id` | Composio response shape changed | Open the workflow log and check the `data` keys; update `publish_instagram.py` accordingly |
| Workflow runs but no post appears | Account not connected or insufficient permissions | Re-do Composio OAuth; ensure `instagram_content_publish` scope was granted |
| `Image unchanged; no commit needed` | Same date already ran | Expected on retry — history.json will prevent double-publishing |
| Workflow fails after push but image is in repo | CDN delay on raw URL | The workflow already waits 20s; if it persists, increase the `sleep 20` in `instagram.yml` |

---

## Security

- ✅ All credentials live in **GitHub Secrets**, never in files.
- ✅ The PAT used to set up the repo is not stored anywhere in the repo.
- ✅ The workflow only uses the auto-provisioned `GITHUB_TOKEN` (not your PAT) for `git push`.
- ✅ `.gitignore` blocks `.env`, `*.key`, `*.pem`, `secrets/`, `credentials/`.
- ✅ `publish_instagram.py` redacts `access_token` / `api_key` / `token` / `refresh_token` / `client_secret` from any error snippet before logging.
- ✅ Secrets are passed via the `env:` block, so GitHub masks them in logs automatically.
- ❌ Never run the workflow with `ACTIONS_STEP_DEBUG=true` if you suspect a secret could leak via verbose library logging.

---

## Idempotency

`content/history.json` records every publication attempt. Before publishing, `publish_instagram.py` checks whether today's content ID (`YYYY-MM-DD`) already has a `"published"` entry. If yes, it logs `[INFO] Content ... was already published. Skipping (idempotency).` and exits 0. This means:

- Re-running a failed workflow the next day will NOT re-publish yesterday's content (good).
- Re-running the same day's workflow will NOT double-publish (good).
- To force a re-publish, manually delete the corresponding entry from `content/history.json` first.

---

## Stage rollout (recommended)

This repo is built to be rolled out safely:

1. **Stage 1 — dry run**: Manually trigger the workflow with `dry_run=true`. Verify image generation, manifest, and history recording work. No post will be made.
2. **Stage 2 — first real post**: Trigger with `dry_run=false`. Watch the logs — if `INSTAGRAM_CREATE_POST` succeeds, check your Instagram feed.
3. **Stage 3 — schedule on**: The cron is already configured. To temporarily disable, comment out the `schedule:` block in `instagram.yml`.
4. **Stage 4 — daily automation**: Once stable for a few days, leave it alone. The system posts once per day.

---

## License

MIT — see `LICENSE` file if/when added. This project is for legitimate content automation (daily useful facts). Do not use it for spam, engagement manipulation, or copyrighted content.
