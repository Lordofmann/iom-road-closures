# Isle of Man Road Race Closures (self-updating site)

A single-page static site that explains, in plain English, when roads on the Isle of Man are closed for the TT, Pre-TT Classic, Southern 100 and Manx Grand Prix.

## How it stays current

A GitHub Action runs the `scripts/update.py` Python script on a schedule (twice daily May-August, once daily otherwise; also runnable manually from the Actions tab). The script:

1. Fetches the latest TT news headlines from Manx Radio.
2. Fetches the official iomttraces.com schedule page and hashes the content.
3. Compares to the stored baseline hash and flags any change.
4. Patches `index.html` between the `<!-- AUTO_NEWS_START -->` and `<!-- AUTO_NEWS_END -->` markers with the headlines, and adds a high-visibility banner if the official schedule appears to have changed.
5. Updates the "Last updated" timestamp.
6. Commits the changes back to the repo. Netlify (connected to this repo) auto-deploys within a minute.

If any source can't be fetched, the script continues gracefully without breaking the page.

## Files

- `index.html` — the entire site, self-contained (CSS and JS inline).
- `scripts/update.py` — the daily scraper.
- `requirements.txt` — Python dependencies.
- `.github/workflows/daily-update.yml` — the GitHub Action.
- `data/baseline.json` — schedule hash baseline (auto-managed).
- `data/news.json` — last fetched news cache (auto-managed).

## Running locally

```bash
pip install -r requirements.txt
python scripts/update.py
```

You'll see log output and `index.html` will be patched in place if anything changed.

## Manual update

In the GitHub web UI, go to **Actions → Daily site update → Run workflow**. The site will be regenerated and redeployed within ~2 minutes.

## Acknowledging a confirmed schedule change

When the script detects that the official iomttraces.com schedule has changed, it adds a red banner to the site. After you've checked the change and updated the embedded schedule data in `index.html` accordingly, delete `data/baseline.json` and let the next run establish a fresh baseline.

## Credits

Data sourced from iomttraces.com, southern100.com, manxgrandprix.co.uk, gov.im, iomtoday.co.im and Manx Radio. This is an unofficial guide. Always verify with the Road Information Hotline (01624 685888) before setting off.
