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
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
ALERTS_PATH = DATA_DIR / "alerts.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; IOM-RoadClosures-Bot/1.0; "
    "+https://github.com/Lordofmann/iom-road-closures)"
)

MANX_RADIO_TT_URL = "https://www.manxradio.com/news/tt-news/"
IOMTT_SCHEDULE_URL = "https://www.iomttraces.com/racing/page/schedule/"
SOUTHERN_100_RSS_URL = "https://southern100.com/feed/"

REQUEST_TIMEOUT = 30
MAX_HEADLINES = 5
CACHE_SIZE = 20
CACHE_MAX_DAYS = 30

# Trust-tier alerts (powers the <!-- AUTO_ALERTS_START/END --> block on the page)
ALERTS_CACHE_MAX_DAYS = 90       # how long auto items linger in the cache before age-out
ALERTS_MAX_ITEMS = 20            # cap on cached auto items per source bucket
ALERTS_DISPLAY_CAP = 5           # top-N shown on the page (pinned items can exceed this)
ALERTS_EVENT_CLASSIFY_WINDOW = 30  # days of date-proximity used to map an article to an event

# Conservative title-keyword filter for the Southern 100 RSS feed. Case-insensitive
# substring match. Approved set: Tier 1 (definite phrases) + "contingency".
S100_SCHEDULE_KEYWORDS = (
    "revised schedule",
    "revised timetable",
    "revised practice",
    "revised programme",
    "revised race",
    "schedule revision",
    "schedule update",
    "schedule change",
    "new timetable",
    "rescheduled",
    "postponed",
    "cancelled",
    "cancellation",
    "contingency",
)

# The note shown beneath every auto-detected Southern 100 alert. Honest about the
# data source's limitations: revised times are baked into JPG images on their site,
# so we can detect that a revision happened but can't extract the new times.
S100_LIMITATION_NOTE = (
    "Heads-up: this source publishes revised times as images, which we cannot read "
    "automatically. Open the source link to see the new times. Our route checker has "
    "<strong>not</strong> been updated for this revision."
)


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
# Trust-tier alerts (powers <!-- AUTO_ALERTS_START/END --> on the page)
#
# Architecture:
#   - Each cached item is a self-describing dict with a `tier` field (auto /
#     human / verified). The page's CSS picks the visual treatment from `tier`.
#   - Auto items come from scrapers (currently: Southern 100 RSS). Human items
#     would be added by hand to data/alerts.json. Verified items typically
#     reference the canonical `closures` array in index.html.
#   - Pinning rule: auto items with implies_schedule_change=True for an event
#     that's "running today" (today's date appears in any closure entry for
#     that event) are kept visible regardless of how many newer items arrive.
# ----------------------------------------------------------------------------

ALERTS_BLOCK_RE = re.compile(
    r"(<!-- AUTO_ALERTS_START -->)(.*?)(<!-- AUTO_ALERTS_END -->)", re.DOTALL
)


# --- RSS parsing -----------------------------------------------------------

def parse_southern100_rss(xml_text: str) -> list[dict]:
    """Parse the RSS XML into a list of {title, url, pub_iso, guid} dicts.

    Returns [] on parse failure. Skips items missing either title or link.
    Converts pubDate (RFC 822) to ISO UTC for downstream use.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log(f"  RSS parse error: {e}")
        return []

    items: list[dict] = []
    for item_el in root.iter("item"):
        title_el = item_el.find("title")
        link_el = item_el.find("link")
        pub_el = item_el.find("pubDate")
        guid_el = item_el.find("guid")

        title = (title_el.text or "").strip() if title_el is not None else ""
        url = (link_el.text or "").strip() if link_el is not None else ""
        if not title or not url:
            continue

        pub_iso = None
        if pub_el is not None and pub_el.text:
            try:
                dt = parsedate_to_datetime(pub_el.text)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                pub_iso = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (TypeError, ValueError):
                pub_iso = None

        guid = (guid_el.text or "").strip() if guid_el is not None and guid_el.text else url

        items.append({"title": title, "url": url, "pub_iso": pub_iso, "guid": guid})
    return items


def _matches_schedule_keywords(title: str) -> bool:
    """Conservative title filter — Tier 1 phrases + 'contingency'."""
    lower = title.lower()
    return any(kw in lower for kw in S100_SCHEDULE_KEYWORDS)


# --- Event-date awareness (drives the pinning rule) ------------------------

def extract_event_dates_from_index(html: str) -> dict[str, set[str]]:
    """Parse {event_id -> set of YYYY-MM-DD strings} out of the closures array in index.html.

    Walks the array character by character, tracking brace depth, to extract
    each top-level {...} entry as a string. This is necessary because Pre-TT
    and S100 entries contain nested objects (`windows: [{ close, reopen }]`)
    which a simple `\\{[^{}]*\\}` regex would mis-match.

    If the parse fails entirely (e.g. file shape drifts) we return {} and the
    pinning rule silently degrades to "nothing is pinned" — pages still render,
    just without the pinned indicator. Failure-tolerance over precision here.
    """
    m = re.search(r"const closures\s*=\s*\[(.*?)\];", html, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)

    result: dict[str, set[str]] = {}
    depth = 0
    start: int | None = None
    for i, ch in enumerate(block):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                entry = block[start:i + 1]
                e = re.search(r"event:\s*'([^']+)'", entry)
                d = re.search(r"date:\s*'(\d{4}-\d{2}-\d{2})'", entry)
                if e and d:
                    result.setdefault(e.group(1), set()).add(d.group(1))
                start = None
    return result


def running_events_today(event_dates: dict[str, set[str]], today_iso: str) -> set[str]:
    """Which events have any closure entry on today's date."""
    return {ev for ev, dates in event_dates.items() if today_iso in dates}


def classify_event_by_date(
    pub_date_str: str | None,
    event_dates: dict[str, set[str]],
    window_days: int = ALERTS_EVENT_CLASSIFY_WINDOW,
) -> str | None:
    """Map an article publication date to the nearest event whose closures
    fall within `window_days` of it. Returns None if no event is close enough."""
    if not pub_date_str:
        return None
    try:
        pub_date = datetime.strptime(pub_date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    best_event, best_distance = None, None
    for event_id, date_strs in event_dates.items():
        for ds in date_strs:
            try:
                d = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            distance = abs((pub_date - d).days)
            if distance <= window_days and (best_distance is None or distance < best_distance):
                best_event = event_id
                best_distance = distance
    return best_event


# --- Watcher: Southern 100 RSS --------------------------------------------

def fetch_southern100_alerts(
    event_dates: dict[str, set[str]],
    now: datetime,
) -> list[dict]:
    """Fetch the S100 feed, keep only items whose title matches schedule
    keywords, and shape them into the canonical alert dict for the cache.

    Failure-tolerant: any error returns []. The rest of the run continues.
    """
    log("Fetching Southern 100 RSS feed...")
    xml = fetch(SOUTHERN_100_RSS_URL)
    if not xml:
        return []
    raw = parse_southern100_rss(xml)
    log(f"  parsed {len(raw)} RSS items from feed")
    matched = [it for it in raw if _matches_schedule_keywords(it["title"])]
    log(f"  {len(matched)} matched schedule-revision keywords")

    nowstamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[dict] = []
    for it in matched:
        event = classify_event_by_date(it.get("pub_iso"), event_dates)
        out.append({
            # id is stable and used for dedup display-signature; URL is the cache key.
            "id": "s100-" + (it.get("guid") or it["url"]),
            "tier": "auto",
            "text": it["title"],
            "source_name": "Southern 100",
            "source_url": it["url"],
            "timestamp": it.get("pub_iso") or nowstamp,
            "first_seen": nowstamp,
            "implies_schedule_change": True,
            "event": event,
            "note": S100_LIMITATION_NOTE,
        })
    return out


# --- Cache I/O --------------------------------------------------------------

def load_alerts_cache() -> tuple[list[dict], str | None]:
    """Returns (items, last_fetch_iso). Both default if the file is missing/corrupt."""
    if not ALERTS_PATH.exists():
        return [], None
    try:
        data = json.loads(ALERTS_PATH.read_text())
        items = data.get("items", [])
        # defensive filter — only dicts with at least source_url survive
        items = [it for it in items if isinstance(it, dict) and it.get("source_url")]
        return items, data.get("last_fetch")
    except Exception:
        return [], None


def write_alerts_cache(items: list[dict], now: datetime) -> None:
    """Always write — last_fetch must refresh every run so the freshness label is honest.

    Strips any transient flags (e.g. _pinned) so the cache file stays canonical.
    """
    DATA_DIR.mkdir(exist_ok=True)
    clean = [{k: v for k, v in it.items() if not k.startswith("_")} for it in items]
    payload = {
        "last_fetch": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": clean,
    }
    ALERTS_PATH.write_text(json.dumps(payload, indent=2))


def merge_alerts_into_cache(
    existing: list[dict], fresh: list[dict], now: datetime
) -> list[dict]:
    """Add unseen `fresh` auto items to `existing`. Preserve human/verified items
    untouched. Drop auto items older than ALERTS_CACHE_MAX_DAYS by first_seen.
    Cap auto items at ALERTS_MAX_ITEMS. Returns a new list (no mutation)."""
    existing_auto_urls = {it.get("source_url") for it in existing if it.get("tier") == "auto"}
    result = list(existing)
    for f in fresh:
        if f.get("source_url") in existing_auto_urls:
            continue
        result.append(f)
        existing_auto_urls.add(f.get("source_url"))

    cutoff = now - timedelta(days=ALERTS_CACHE_MAX_DAYS)

    def keep(it: dict) -> bool:
        if it.get("tier") != "auto":
            return True
        return _parse_iso(it.get("first_seen", "")) >= cutoff

    result = [it for it in result if keep(it)]

    # Cap only the auto bucket; human/verified items are unbounded.
    auto = [it for it in result if it.get("tier") == "auto"]
    non_auto = [it for it in result if it.get("tier") != "auto"]
    auto.sort(key=lambda x: x.get("first_seen", ""), reverse=True)
    auto = auto[:ALERTS_MAX_ITEMS]
    return non_auto + auto


# --- Display selection (pinning rule lives here) ---------------------------

def _recency_key(it: dict) -> str:
    return it.get("timestamp") or it.get("first_seen", "")


def select_displayed_alerts(
    cache: list[dict],
    running_events: set[str],
    max_items: int,
) -> list[dict]:
    """Decide which items get rendered on the page, applying the pinning rule:

      - Auto items with implies_schedule_change=True AND event currently running
        are PINNED — they always render, even if more recent items would push
        them out of the top-N display cap.
      - Remaining slots (max_items - pinned) are filled with the most recent
        unpinned auto items.
      - Human and verified items are ALWAYS displayed (no cap, no aging).
      - Pinned items are marked with _pinned=True (a transient flag the
        renderer reads to draw the pinned badge; never persisted to cache).
    """
    autos = [dict(it) for it in cache if it.get("tier") == "auto"]
    autos.sort(key=_recency_key, reverse=True)

    pinned: list[dict] = []
    unpinned: list[dict] = []
    for it in autos:
        ev = it.get("event")
        if it.get("implies_schedule_change") and ev and ev in running_events:
            it["_pinned"] = True
            pinned.append(it)
        else:
            unpinned.append(it)

    slots = max(0, max_items - len(pinned))
    # Pinned items render at the TOP regardless of date (they're already sorted
    # by recency among themselves), followed by the most recent unpinned items.
    # We deliberately do NOT re-sort the combined list, so the badge's
    # importance signal lines up with visual position.
    auto_displayed = pinned + unpinned[:slots]

    others = [dict(it) for it in cache if it.get("tier") in ("human", "verified")]
    others.sort(key=_recency_key, reverse=True)

    # Stack human/verified at the top of the section, then auto items below.
    return others + auto_displayed


def displayed_signature(items: list[dict]) -> list[tuple]:
    """A stable, comparable signature used to gate index.html rewrites.

    Includes id, tier and pinned state so the page is rewritten when any of
    those change for the displayed set (e.g. a pin transition on race day).
    """
    return [
        (it.get("id") or it.get("source_url"), it.get("tier"), bool(it.get("_pinned")))
        for it in items
    ]


# --- HTML rendering --------------------------------------------------------

def _esc(s: str) -> str:
    """Minimal HTML escaping consistent with render_news_block."""
    return (s or "").replace("<", "&lt;").replace(">", "&gt;")


def _format_pub_timestamp(ts: str) -> str:
    """Convert an ISO timestamp into 'DD Mon YYYY, HH:MM UTC' for display.
    Falls back to the raw string if parsing fails."""
    if not ts:
        return ""
    try:
        d = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
        return d.strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return ts


def _render_alert_item(it: dict, now: datetime) -> str:
    tier = it.get("tier", "auto")
    text = _esc(it.get("text", ""))
    source_name = _esc(it.get("source_name", ""))
    source_url = it.get("source_url", "#")
    timestamp = it.get("timestamp", "")
    first_seen = it.get("first_seen", "")
    event = it.get("event")
    note = it.get("note", "")  # rendered with HTML allowed (we author it server-side)

    tier_label_map = {
        "verified": "&#10003; VERIFIED SCHEDULE",
        "human":    "&#9679; CONFIRMED BY " + source_name.upper(),
        "auto":     "&#9888; AUTOMATICALLY DETECTED &mdash; NOT YET VERIFIED",
    }
    tier_label = tier_label_map.get(tier, tier.upper())

    pin_html = ' <span class="pin">&#128204; TODAY\'S RACING</span>' if it.get("_pinned") else ""

    meta_parts: list[str] = []
    if source_name and source_url:
        meta_parts.append(
            f'Source: <a href="{source_url}" target="_blank" rel="noopener">{source_name}</a>'
        )
    pub_display = _format_pub_timestamp(timestamp)
    if pub_display:
        meta_parts.append(f"published {pub_display}")
    if first_seen and first_seen != timestamp:
        seen_label = format_relative_age(first_seen, now)
        meta_parts.append(f"seen {seen_label}")
    meta_html = " &middot; ".join(meta_parts)

    note_html = f'<div class="alert-note">{note}</div>' if note else ""
    event_attr = f' data-event="{_esc(event)}"' if event else ""

    return (
        f'<div class="alert tier-{tier}"{event_attr}>'
        f'<div class="alert-tier-label">{tier_label}{pin_html}</div>'
        f'<div class="alert-text"><strong>{text}</strong></div>'
        f'<div class="alert-meta">{meta_html}</div>'
        f'{note_html}'
        f'</div>'
    )


def render_alerts_block(
    displayed: list[dict],
    last_fetch_iso: str | None,
    now: datetime,
) -> str:
    """Return the full replacement for the <!-- AUTO_ALERTS_START/END --> block.

    If there are no items to display, returns a minimal markers-only block
    (effectively hiding the section, per the "hide entirely when empty" rule).
    """
    if not displayed:
        return "<!-- AUTO_ALERTS_START -->\n  <!-- no alerts to display -->\n  <!-- AUTO_ALERTS_END -->"

    parts: list[str] = ["<!-- AUTO_ALERTS_START -->", '<div class="alerts-block">']

    if last_fetch_iso:
        initial_label = format_relative_age(last_fetch_iso, now)
        parts.append(
            '<div class="alerts-meta">'
            f'Last checked <span class="freshness" data-freshness-from="{last_fetch_iso}">{initial_label}</span>'
            ' &middot; auto-detected from external sources, not yet verified.'
            '</div>'
        )

    for it in displayed:
        parts.append(_render_alert_item(it, now))

    parts.append("</div>")
    parts.append("<!-- AUTO_ALERTS_END -->")
    return "\n".join(parts)


def patch_html_alerts(html: str, alerts_html: str) -> str:
    return ALERTS_BLOCK_RE.sub(alerts_html, html)


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

    # --- 3. Southern 100 alerts (first user of the trust-tiered alerts system) ---
    today_iso = now.strftime("%Y-%m-%d")
    event_dates = extract_event_dates_from_index(original_html)
    running_today = running_events_today(event_dates, today_iso)
    log(f"  events running today ({today_iso}): {sorted(running_today) or 'none'}")

    fresh_alerts = fetch_southern100_alerts(event_dates, now)
    old_alerts, _old_last_fetch = load_alerts_cache()
    log(f"  alerts cache had {len(old_alerts)} items before merge")
    merged_alerts = merge_alerts_into_cache(old_alerts, fresh_alerts, now)
    log(f"  alerts cache has {len(merged_alerts)} items after merge")

    displayed_alerts = select_displayed_alerts(merged_alerts, running_today, ALERTS_DISPLAY_CAP)
    pinned_count = sum(1 for x in displayed_alerts if x.get("_pinned"))
    log(f"  displaying {len(displayed_alerts)} alerts ({pinned_count} pinned)")

    # --- 4. Decide what's actually worth writing ---
    old_displayed_news = [it.get("url") for it in old_cache[:MAX_HEADLINES]]
    new_displayed_news = [it.get("url") for it in new_cache[:MAX_HEADLINES]]
    display_changed = old_displayed_news != new_displayed_news
    cache_changed = old_cache != new_cache

    # Alerts display gate. Includes pin state and tier in the signature so any
    # of the following triggers an index.html rewrite (and therefore a deploy):
    #   - a new auto item enters/leaves the displayed set
    #   - an item changes tier (e.g. a human upgrade)
    #   - pinning flips because the calendar moved into/out of a race day
    old_displayed_alerts = select_displayed_alerts(old_alerts, running_today, ALERTS_DISPLAY_CAP)
    alerts_display_changed = (
        displayed_signature(old_displayed_alerts) != displayed_signature(displayed_alerts)
    )

    log(
        f"  display_changed={display_changed} cache_changed={cache_changed} "
        f"schedule_changed={schedule_changed} alerts_display_changed={alerts_display_changed}"
    )

    # Persist news cache when its content has changed (so rolling history survives)
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

    # Always persist alerts.json — last_fetch must refresh every run so the
    # JS-rendered "Last checked X ago" label on the page stays honest, even
    # on quiet runs that don't touch index.html.
    write_alerts_cache(merged_alerts, now)
    log("  alerts.json updated (last_fetch refreshed)")

    # Only rewrite index.html when the page visitors see would actually change.
    # Three independent signals can trigger a rewrite; any one is enough.
    if display_changed or schedule_changed or alerts_display_changed:
        news_html = render_news_block(headlines, schedule_changed, now)
        alerts_html = render_alerts_block(
            displayed_alerts, now.strftime("%Y-%m-%dT%H:%M:%SZ"), now
        )
        timestamp = now.strftime("%d %B %Y, %H:%M UTC")
        new_html = patch_html(original_html, news_html, timestamp)
        new_html = patch_html_alerts(new_html, alerts_html)
        HTML_PATH.write_text(new_html, encoding="utf-8")
        log(f"  index.html updated at {timestamp} (a deploy will follow)")
    else:
        log("  display unchanged, skipping index.html update (no deploy needed)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
