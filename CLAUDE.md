# IoM Road Closures — project notes

A single-page static site that tells visitors when Isle of Man roads are closed for the TT, Pre-TT Billown, Southern 100 and Manx Grand Prix. It updates itself daily via a GitHub Action and auto-deploys through Netlify.

## What's in the repo

- `index.html` — the entire site. HTML, CSS and JavaScript are all inline. There is no build step.
- `scripts/update.py` — the daily scraper (Python).
- `requirements.txt` — Python dependencies for the scraper (`requests`, `beautifulsoup4`).
- `.github/workflows/daily-update.yml` — the GitHub Action that runs the scraper on a schedule.
- `netlify.toml` — Netlify build/deploy settings.
- `data/news.json` — rolling cache of the last ~20 headlines (managed by the scraper).
- `data/baseline.json` — a hash of the last verified official iomttraces.com schedule (managed by the scraper).

## How the page itself works

Everything visitors see is rendered by JavaScript at the bottom of `index.html`. Four tabs: **Now & today**, **Full schedule**, **Map & sections**, **Route checker**.

The data that drives every tab lives in a handful of JavaScript constants near the bottom of the file (the `<script>` block, around lines 847–1121):

- **`closures`** (≈ line 847) — the master schedule. One entry per closure day. This is the array to edit when official times change.
  - Mountain Course days use `mountainCloses`, `fullCloses`, `reopen` (and optionally `secondFull`, `secondReopen` for days with two sessions).
  - Billown days use a `windows` array of `{ close, reopen }` pairs.
  - Days can be flagged `restDay`, `pending` (times TBC), or `contingency`.
- **`eventMeta`** — labels and colours for each event (`pre-tt`, `tt`, `s100`, `mgp`).
- **`sections`** (≈ line 947) — the glossary of course section names shown on the Map tab.
- **`places`**, **`routeRules`**, **`diversions`** (≈ line 973 onward) — power the Route checker and the diversions list.

The page also has a hard-coded special case for the **Mountain Road one-way operation** (22 May 16:30 → 9 June 09:30) and **A18 cone setup/teardown windows** inside the `statusAt()` function (≈ line 1152). If those dates change year-to-year, edit them there.

## Where auto-updated content sits inside the page

Two HTML comment markers tell the scraper where it's allowed to write. **Don't remove or rename these markers** — the scraper finds them with regex:

- `<!-- AUTO_NEWS_START --> ... <!-- AUTO_NEWS_END -->` (around line 628). The scraper replaces everything between these with the latest Manx Radio headlines and, if a schedule change has been detected, a red warning banner.
- `<!-- LAST_UPDATED -->...<!-- /LAST_UPDATED -->` (in the footer, around line 832). The scraper writes the timestamp here.

## How the auto-update job works

1. **GitHub Action `daily-update.yml`** runs on a cron schedule:
   - May–August (race season): twice daily at 06:00 and 17:00 UTC.
   - Sept–April: once daily at 06:00 UTC.
   - Also runnable manually from the Actions tab (workflow_dispatch).
2. The Action checks out the repo, installs Python deps, and runs `python scripts/update.py`.
3. `update.py`:
   - Fetches Manx Radio TT news headlines and merges them into `data/news.json` (rolling 20-item cache, 30-day max age).
   - Fetches the iomttraces.com schedule page, extracts the schedule tables, hashes them, and compares to `data/baseline.json`. If the hash differs from baseline, it sets a "schedule changed" flag.
   - Decides whether the visible page has actually changed. It only rewrites `index.html` if the top-5 displayed headlines have changed OR a schedule change has been flagged — this avoids burning Netlify build minutes on quiet days.
   - Always writes `news.json` when cache content changes (so the rolling history survives quiet news days, even when `index.html` isn't touched).
4. The Action commits any changed files as `iom-roads-bot` with a message like `Auto-update: news + timestamp YYYY-MM-DD HH:MM UTC` and pushes to `main`.

If a source can't be fetched, the script logs the failure and carries on rather than crashing.

## How a change reaches the live site

1. Edit files locally in this folder.
2. `git add` the files, `git commit`, `git push` to `origin/main`.
3. Netlify watches the `main` branch of this repo and starts a build on every push.
4. Before building, Netlify runs the `ignore` command from `netlify.toml`:
   `git diff --quiet HEAD^ HEAD -- index.html`
   - Exit 0 (no change to `index.html`) → **build skipped**. This is how bot commits that only touch `data/*.json` avoid redeploying.
   - Exit non-zero (`index.html` changed) → **build proceeds**.
5. With `publish = "."`, Netlify deploys the entire repo root as the live site. The site is up within ~1–2 minutes of the push.

So: any change that needs to go live **must touch `index.html`**. Edits to `data/*.json` alone won't trigger a deploy by design.

## After a schedule-change banner appears

When `update.py` detects the official iomttraces.com schedule has changed, it adds a red "Heads-up" banner to the page. To acknowledge it:

1. Open the official schedule and compare against the `closures` array in `index.html`.
2. Edit any affected entries.
3. Delete `data/baseline.json`. The next scraper run will establish a fresh baseline hash, and the banner will stop appearing.
4. Commit and push.

## Running the scraper locally

```bash
pip install -r requirements.txt
python scripts/update.py
```

Logs go to stdout. `index.html`, `data/news.json` and `data/baseline.json` may be modified in place.
