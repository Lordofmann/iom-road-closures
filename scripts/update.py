"""
Daily updater for the Isle of Man road race closures site.

What it does:
  1. Fetches the latest TT news headlines from Manx Radio.
  2. Fetches the official iomttraces.com schedule page.
  3. Hashes the schedule content and compares to a stored baseline.
  4. ONLY rewrites index.html (and thus triggers a deploy) when the page
     visitors see would actually look different. This prevents the bot from
     burning Netlify credits on uneventful runs.
  5. Always updates the rolling news cache (data/news.json) when it changes,
     so the cache survives quiet-news days.

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
from datetime import datetime, timedelta
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
CACHE_SIZE = 20
CACHE_MAX_DAYS = 30


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.utcnow().isoformat(timespec='seconds')}Z] {msg}", flush=True)


def fetch(url: str) -> str | None:
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
# News cache (rolling)
# ----------------------------------------------------------------------------

def load_news_cache() -> list[dict]:
    if not NEWS_PATH.exists():
        return []
    try:
        data = json.loads(NEWS_PATH.read_text())
        items = data.get("headlines", [])
        return [it for it in items if isinstance(it, dict) and "first_seen" in it and "url" in it]
    except Exception:
        return []


def merge_into_cache(cache: list[dict], fresh: list[dict], now: datetime) -> list[dict]:
    """Add unseen fresh items to a copy of the cache, dedupe, trim, sort. Does not mutate input."""
    result = list(cache)
    existing_urls = {it["url"] for it in result}
    for item in fresh:
        if item.get("url") in existing_urls:
            continue
        result.append({
            "title": item.get("title", ""),
            "url": item["url"],
            "first_seen": now.isoformat(timespec="seconds") + "Z",
            "source_date": item.get("date", ""),
        })
        existing_urls.add(item["url"])

    cutoff = now - timedelta(days=CACHE_MAX_DAYS)
    result = [it for it in result if _parse_iso(it.get("first_seen", "")) >= cutoff]
    result.sort(key=lambda x: x.get("first_seen", ""), reverse=True)
    return result[:CACHE_SIZE]


def _parse_iso(s: str) -> datetime:
    if not s:
        return datetime(1970, 1, 1)
    try:
        if s.endswith("Z"):
            s = s[:-1]
        return datetime.fromisoformat(s)
    except Exception:
        return datetime(1970, 1, 1)


def format_relative_age(first_seen_iso: str, now: datetime) -> str:
    seen = _parse_iso(first_seen_iso)
    delta = now - seen
    seconds = int(delta.total_seconds())
    if seconds < 3600:
        return "just now"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h}h ago"
    days = delta.days
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''} ago"
    return seen.strftime("%d %b")


# ----------------------------------------------------------------------------
# Manx Radio: extract latest TT news headlines
# ----------------------------------------------------------------------------

def parse_manx_radio_headlines(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    total_links = rejected_offsite = rejected_path = rejected_title = 0

    for link in soup.find_all("a", href=True):
        total_links += 1
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.manxradio.com" + href
        if not href.startswith("https://www.manxradio.com/"):
            rejected_offsite += 1
            continue
        path_parts = [p for p in href.replace("https://www.manxradio.com", "").split("/") if p]
        if len(path_parts) < 3 or path_parts[0] != "news":
            rejected_path += 1
            continue

        title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
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

    seen = set()
    unique: list[dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        unique.append(it)
        if len(unique) >= MAX_HEADLINES:
            break

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
    soup = BeautifulSoup(html, "html.parser")
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
    return re.sub(r"\s+", " ", " ".join(pieces)).strip()


# ----------------------------------------------------------------------------
# HTML patching
# ----------------------------------------------------------------------------

NEWS_BLOCK_RE = re.compile(r"(<!-- AUTO_NEWS_START -->)(.*?)(<!-- AUTO_NEWS_END -->)", re.DOTALL)
LAST_UPDATED_RE = re.compile(r"(<!-- LAST_UPDATED -->)(.*?)(<!-- /LAST_UPDATED -->)", re.DOTALL)


def render_news_block(items: list[dict], schedule_changed: bool, now: datetime) -> str:
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
            first_seen = it.get("first_seen")
            if first_seen:
                date = format_relative_age(first_seen, now)
            else:
                date = (it.get("source_date") or it.get("date") or "")
            date = date.replace("<", "&lt;").replace(">", "&gt;")
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
    new_html = NEWS_BLOCK_RE.sub(news_html, html)
    new_html = LAST_UPDATED_RE.sub(f"<!-- LAST_UPDATED -->{timestamp}<!-- /LAST_UPDATED -->", new_html)
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
    now = datetime.utcnow()

    # --- 1. News headlines ---
    log("Fetching Manx Radio TT news...")
    fresh: list[dict] = []
    page = fetch(MANX_RADIO_TT_URL)
    if page:
        try:
            fresh = parse_manx_radio_headlines(page)
            log(f"  found {len(fresh)} fresh headlines")
        except Exception as e:
            log(f"  parse error: {e}")

    old_cache = load_news_cache()
    log(f"  cache had {len(old_cache)} headlines before merge")
    new_cache = merge_into_cache(old_cache, fresh, now)
    log(f"  cache has {len(new_cache)} headlines after merge")
    headlines = new_cache[:MAX_HEADLINES]

    # --- 2. Schedule change detection ---
    log("Fetching official schedule for change detection...")
    schedule_changed = False
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
                    log("  no baseline yet — establishing one")
                    BASELINE_PATH.write_text(
                        json.dumps({"schedule_hash": current_hash, "first_seen": datetime.utcnow().isoformat()}, indent=2)
                    )
        except Exception as e:
            log(f"  schedule parse error: {e}")

    # --- 3. Decide what's actually worth writing ---
    old_displayed = [it.get("url") for it in old_cache[:MAX_HEADLINES]]
    new_displayed = [it.get("url") for it in new_cache[:MAX_HEADLINES]]
    display_changed = old_displayed != new_displayed
    cache_changed = old_cache != new_cache
    log(f"  display_changed={display_changed} cache_changed={cache_changed} schedule_changed={schedule_changed}")

    # Persist cache when its content has changed (so rolling history survives)
    if cache_changed:
        NEWS_PATH.write_text(
            json.dumps(
                {
                    "last_fetch": now.isoformat(timespec="seconds") + "Z",
                    "schedule_changed": schedule_changed,
                    "headlines": new_cache,
                },
                indent=2,
            )
        )
        log("  news.json updated (cache content changed)")

    # Only rewrite index.html when the page visitors see would actually change
    if display_changed or schedule_changed:
        news_html = render_news_block(headlines, schedule_changed, now)
        timestamp = now.strftime("%d %B %Y, %H:%M UTC")
        new_html = patch_html(original_html, news_html, timestamp)
        HTML_PATH.write_text(new_html, encoding="utf-8")
        log(f"  index.html updated at {timestamp} (a deploy will follow)")
    else:
        log("  display unchanged, skipping index.html update (no deploy needed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
