"""
Daily updater for the Isle of Man road race closures site.

What it does:
  1. Fetches the latest TT news headlines from Manx Radio.
  2. Fetches the official iomttraces.com schedule page.
  3. Hashes the schedule content and compares to a stored baseline.
  4. Patches index.html:
     - Updates the "Last updated" timestamp.
     - Injects the latest news between the AUTO_NEWS markers.
     - Adds a high-visibility banner if the official schedule appears to have changed.
  5. Writes the new baseline hash to data/baseline.json if the change was acknowledged.
  6. Exits cleanly if nothing changed (no commit needed).

Designed to fail gracefully: if any single source can't be fetched, the script
continues with what it has rather than blowing up the whole site.

Run locally: python scripts/update.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"Missing dependency: {e}. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / "index.html"
DATA_DIR = ROOT / "data"
BASELINE_PATH = DATA_DIR / "baseline.json"
NEWS_PATH = DATA_DIR / "news.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; IOM-RoadClosures-Bot/1.0; "
    "+https://github.com/your-username/iom-road-closures)"
)

MANX_RADIO_TT_URL = "https://www.manxradio.com/news/tt-news/"
IOMTT_SCHEDULE_URL = "https://www.iomttraces.com/racing/page/schedule/"

REQUEST_TIMEOUT = 30
MAX_HEADLINES = 5


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    """Print with a timestamp so workflow logs are readable."""
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)


def fetch(url: str) -> str | None:
    """GET a URL with a polite User-Agent. Returns text or None on any failure."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log(f"  FAILED to fetch {url}: {e}")
        return None


def short_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# Manx Radio: extract latest TT news headlines
# ----------------------------------------------------------------------------

def parse_manx_radio_headlines(html: str) -> list[dict]:
    """Extract recent TT news items from Manx Radio.

    Strategy (permissive: scans all links, filters by URL structure):
      - Look at every <a href> on the page.
      - Keep only links to manxradio.com whose path has 3+ segments under /news/
        (e.g., /news/tt-news/article-slug/ — filters out /news/weather/ etc).
      - Use the link text as the title; if too short, look for the title in a
        nearby heading inside the same card.
      - Pull a date from any time/span/div near the link.

    Debug counters are printed so any future shape change is easy to diagnose
    from the workflow log.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []

    total_links = 0
    rejected_offsite = 0
    rejected_path = 0
    rejected_title = 0

    for link in soup.find_all("a", href=True):
        total_links += 1
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.manxradio.com" + href
        if not href.startswith("https://www.manxradio.com/"):
            rejected_offsite += 1
            continue
        # Real article URLs look like /news/<category>/<slug>/ → 3+ path segments.
        # Category pages like /news/weather/ → 2 segments.
        path_parts = [p for p in href.replace("https://www.manxradio.com", "").split("/") if p]
        if len(path_parts) < 3 or path_parts[0] != "news":
            rejected_path += 1
            continue

        # Prefer the link's own text. If it's empty/too short, try sibling/child headings.
        # Use " " as separator and collapse whitespace so concatenated children don't run together.
        title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
        # Strip trailing date fragments that often live inside the link itself.
        title = re.sub(r"\s+\d{1,2}\s+\w+\s+20\d{2}\s*$", "", title)
        title = re.sub(r"\s+\d+\s+(hours?|mins?|minutes?|days?)\s+ago\s*$", "", title, flags=re.I)
        title = re.sub(r"\s+(today|yesterday)\s*$", "", title, flags=re.I)
        title = title.strip()
        if len(title) < 15:
            parent = link.parent
            for tag in (parent.find_all(["h1", "h2", "h3", "h4", "h5"]) if parent else []):
                candidate = tag.get_text(strip=True)
                if len(candidate) >= 15:
                    title = candidate
                    break
        if len(title) < 15:
            rejected_title += 1
            continue

        # Hunt for a date near the link (look up to 4 levels up the DOM)
        date_text = ""
        ctx = link.parent
        for _ in range(4):
            if not ctx:
                break
            for tag in ctx.find_all(["time", "span", "div"], limit=30):
                t = tag.get_text(strip=True)
                if re.search(r"\b(20\d\d|today|yesterday|hours? ago|mins? ago)\b", t, re.I) and len(t) < 60:
                    date_text = t
                    break
            if date_text:
                break
            ctx = ctx.parent

        items.append({"title": title, "url": href, "date": date_text})

    # Deduplicate by URL, preserving order
    seen = set()
    unique: list[dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique.append(it)
        if len(unique) >= MAX_HEADLINES:
            break

    # Visible in the workflow log to help diagnose if zero items came back
    print(
        f"  parser stats: total_links={total_links} "
        f"offsite={rejected_offsite} bad_path={rejected_path} "
        f"short_title={rejected_title} candidates={len(items)} kept={len(unique)}",
        flush=True,
    )
    return unique


# ----------------------------------------------------------------------------
# iomttraces.com: hash the schedule content to detect changes
# ----------------------------------------------------------------------------

def extract_schedule_content(html: str) -> str | None:
    """Return a normalised string representing the schedule content.

    We look for the SCHEDULE heading and grab the surrounding tables.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Look for the heading then collect tables that follow
    target = None
    for h in soup.find_all(["h1", "h2", "h3"]):
        if "schedule" in h.get_text(strip=True).lower():
            target = h
            break
    if not target:
        return None
    pieces: list[str] = []
    for sib in target.find_all_next():
        if sib.name in ("h1", "h2") and sib is not target:
            break
        if sib.name == "table":
            pieces.append(sib.get_text(" ", strip=True))
    if not pieces:
        return None
    # Normalise whitespace
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()


# ----------------------------------------------------------------------------
# HTML patching
# ----------------------------------------------------------------------------

NEWS_BLOCK_RE = re.compile(
    r"(<!-- AUTO_NEWS_START -->)(.*?)(<!-- AUTO_NEWS_END -->)",
    re.DOTALL,
)
LAST_UPDATED_RE = re.compile(
    r"(<!-- LAST_UPDATED -->)(.*?)(<!-- /LAST_UPDATED -->)",
    re.DOTALL,
)


def render_news_block(items: list[dict], schedule_changed: bool) -> str:
    """Build the HTML to insert between the AUTO_NEWS markers."""
    parts: list[str] = ["<!-- AUTO_NEWS_START -->"]

    if schedule_changed:
        parts.append(
            '<div class="schedule-changed">'
            "<strong>Heads-up: the official schedule on iomttraces.com may have changed since this page was last verified.</strong> "
            'Cross-check the dates and times below against '
            '<a href="https://www.iomttraces.com/racing/page/schedule/" target="_blank" rel="noopener">the official source</a> '
            'or call the Road Information Hotline on <strong>01624&nbsp;685888</strong>.'
            "</div>"
        )

    if items:
        parts.append('<div class="auto-news">')
        parts.append("<h3>Latest TT news from Manx Radio</h3>")
        parts.append("<ul>")
        for it in items:
            title = (it.get("title") or "").replace("<", "&lt;").replace(">", "&gt;")
            url = it.get("url") or "#"
            date = (it.get("date") or "").replace("<", "&lt;").replace(">", "&gt;")
            date_html = f' <span class="meta">&middot; {date}</span>' if date else ""
            parts.append(
                f'<li><a href="{url}" target="_blank" rel="noopener">{title}</a>{date_html}</li>'
            )
        parts.append("</ul>")
        parts.append(
            '<p class="meta" style="margin-top:8px;">'
            "Headlines fetched automatically. For live, definitive road status call "
            "<strong>01624&nbsp;685888</strong>."
            "</p>"
        )
        parts.append("</div>")

    parts.append("<!-- AUTO_NEWS_END -->")
    return "\n".join(parts)


def patch_html(html: str, news_html: str, timestamp: str) -> str:
    """Replace the AUTO_NEWS block and the LAST_UPDATED stamp."""
    new_html = NEWS_BLOCK_RE.sub(news_html, html)
    new_html = LAST_UPDATED_RE.sub(
        f"<!-- LAST_UPDATED -->{timestamp}<!-- /LAST_UPDATED -->",
        new_html,
    )
    return new_html


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    if not HTML_PATH.exists():
        log(f"index.html not found at {HTML_PATH}")
        return 1

    DATA_DIR.mkdir(exist_ok=True)
    original_html = HTML_PATH.read_text(encoding="utf-8")

    # --- 1. News headlines ---
    log("Fetching Manx Radio TT news...")
    headlines: list[dict] = []
    page = fetch(MANX_RADIO_TT_URL)
    if page:
        try:
            headlines = parse_manx_radio_headlines(page)
            log(f"  found {len(headlines)} headlines")
        except Exception as e:
            log(f"  parse error: {e}")

    # --- 2. Schedule change detection ---
    log("Fetching official schedule for change detection...")
    schedule_changed = False
    schedule_text = None
    page = fetch(IOMTT_SCHEDULE_URL)
    if page:
        try:
            schedule_text = extract_schedule_content(page)
            if schedule_text:
                current_hash = short_hash(schedule_text)
                log(f"  schedule hash: {current_hash}")
                baseline = {}
                if BASELINE_PATH.exists():
                    try:
                        baseline = json.loads(BASELINE_PATH.read_text())
                    except Exception:
                        baseline = {}
                if baseline.get("schedule_hash") and baseline["schedule_hash"] != current_hash:
                    schedule_changed = True
                    log(f"  CHANGE DETECTED (baseline was {baseline['schedule_hash']})")
                elif not baseline:
                    # First run: establish baseline silently
                    log("  no baseline yet — establishing one")
                    BASELINE_PATH.write_text(
                        json.dumps({"schedule_hash": current_hash, "first_seen": datetime.utcnow().isoformat()}, indent=2)
                    )
        except Exception as e:
            log(f"  schedule parse error: {e}")

    # --- 3. Build the news block ---
    news_html = render_news_block(headlines, schedule_changed)

    # --- 4. Patch HTML ---
    timestamp = datetime.utcnow().strftime("%d %B %Y, %H:%M UTC")
    new_html = patch_html(original_html, news_html, timestamp)

    # Always write the news cache so we know what was injected
    NEWS_PATH.write_text(
        json.dumps(
            {"timestamp": timestamp, "headlines": headlines, "schedule_changed": schedule_changed},
            indent=2,
        )
    )

    if new_html == original_html:
        log("No changes to write. Done.")
        return 0

    HTML_PATH.write_text(new_html, encoding="utf-8")
    log(f"index.html updated at {timestamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
