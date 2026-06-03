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
SCHEDULE_TT_PARSED_PATH = DATA_DIR / "schedule-tt-parsed.json"
SCHEDULE_TT_OVERRIDE_PATH = DATA_DIR / "schedule-tt-manual-override.json"
TT_PARSER_VERSION = "1.0"

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
# iomttraces.com: parse the schedule INTO our closures-array format (TT only)
#
# Phases 2-3 of the TT auto-update work. Phase 2 (this section) extracts a
# candidate list of TT entries from the page. Phase 3 validates the result and
# decides whether the parse is trustworthy enough to apply. Phase 4 (not yet
# shipped) will actually overwrite the closures array between markers in
# index.html — until then, the parser's output only goes to a sidecar JSON
# file (data/schedule-tt-parsed.json) for observation.
#
# The page shape we're parsing (verified against the live site on 2026-05-26):
#   - heading "2026 SCHEDULE"
#   - 3 tables: Qualifying Week, Race Week, Contingency Periods
#   - each row is a day, cells = [date label] | [schedule text]
#   - times are HH:MM 24-hour, followed by " – <phrase>"
#   - 4 key phrases: "Mountain Section only begins to close",
#     "Full TT Mountain Course closed",
#     "All roads will re-open no later than",
#     "All roads except for mountain section will re-open"
#   - past-day rows lose their times once a session has run
# ----------------------------------------------------------------------------

# Known closure-phrase markers, lower-cased for matching.
_TT_MARK_MOUNTAIN_CLOSES = "mountain section only begins to close"
_TT_MARK_FULL_CLOSES = "full tt mountain course closed"
_TT_MARK_FULL_REOPEN = "all roads will re-open"
_TT_MARK_PARTIAL_REOPEN = "all roads except for mountain section will re-open"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_tt_schedule_year(html: str) -> int | None:
    """Extract the schedule year from a heading like '2026 SCHEDULE' or
    '2026 Schedule'. Returns None if no plausible year heading is found."""
    m = re.search(r"\b(20\d{2})\s+SCHEDULE\b", html, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _parse_tt_day_label(text: str, year: int) -> dict | None:
    """Parse a date-label cell like 'RACE DAY 3 Tuesday 2 June' or
    'Thursday 28 May' or 'Monday 25 May (Spring Bank Holiday)' into:
        { 'date': 'YYYY-MM-DD', 'day_label': 'Tue — Race Day 3', ... }
    Returns None if no day/month token was found.
    """
    m = re.search(
        r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
        r"(\d{1,2})\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\b",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    weekday = m.group(1).title()
    day = int(m.group(2))
    month = _MONTHS[m.group(3).lower()]
    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None

    short_day = weekday[:3]
    race_day_m = re.search(r"\bRACE\s+DAY\s+(\d+)\b", text, re.IGNORECASE)
    paren_m = re.search(r"\(([^)]+)\)", text)
    label = short_day
    if race_day_m:
        label = f"{short_day} — Race Day {race_day_m.group(1)}"
    if paren_m:
        label = f"{label} ({paren_m.group(1)})"
    return {"date": dt.strftime("%Y-%m-%d"), "day_label": label}


def _clean_tt_activity(chunk: str) -> str:
    """Reduce a race-name chunk like 'Milwaukee Senior TT – [6 laps] Start List
    - Results - Lap by Lap - Fast Laps' to just 'Milwaukee Senior TT'."""
    s = chunk
    # Drop "[N lap(s)]" annotations and everything after
    s = re.sub(r"\s*[–—-]\s*\[\d+\s*laps?\].*$", "", s, flags=re.IGNORECASE)
    # Drop "Start List ..." trailing decoration
    s = re.sub(r"\s*Start\s+List.*$", "", s, flags=re.IGNORECASE)
    # Drop results / lap-by-lap / fast-laps trailing links
    s = re.sub(r"\s+-\s+(Results|Lap by Lap|Fast Laps).*$", "", s, flags=re.IGNORECASE)
    return s.strip(" -–— ").strip()


def _parse_tt_schedule_cell(text: str) -> dict:
    """Parse the schedule-detail cell into a partial closures-array entry.

    Returns a dict that may contain any of: is_rest (bool), mountainCloses,
    fullCloses, reopen, secondMountain, secondFull, secondReopen, activity.

    The walker is tolerant: unrecognised chunks are ignored, RACE POSTPONED
    markers are dropped, and a cell with no time tokens at all (typical for a
    past day whose results have replaced the schedule) returns {}.
    """
    text = (text or "").strip()
    info: dict = {}

    # Quick rest-day detection (case-insensitive exact-ish match)
    if re.fullmatch(r"\s*Rest\s+Day\s*", text, re.IGNORECASE):
        info["is_rest"] = True
        info["activity"] = "Rest day"
        return info

    # Find every "HH:MM – " marker; chunks run from one marker to the next
    markers = list(re.finditer(r"(\d{1,2}):(\d{2})\s*[–—-]\s*", text))
    if not markers:
        return info  # no times — likely a past day or atypical row

    chunks: list[tuple[str, str]] = []
    for i, mt in enumerate(markers):
        hh, mm = int(mt.group(1)), int(mt.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        time_str = f"{hh:02d}:{mm:02d}"
        chunk_start = mt.end()
        chunk_end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        chunks.append((time_str, text[chunk_start:chunk_end].strip()))

    first_reopen_seen = False
    second_full_close_seen = False
    for time_str, chunk in chunks:
        lower = chunk.lower()
        # Phrase markers are matched BEFORE the race-postponed filter. The page
        # often appends "RACE POSTPONED ..." to the end of an earlier chunk
        # (e.g. the 17:00 partial-reopen chunk on Tue 2 June 2026), so we must
        # detect the phrase first or we'd lose the time entirely.
        if _TT_MARK_PARTIAL_REOPEN in lower:
            if not first_reopen_seen:
                info["reopen"] = time_str
                info["secondMountain"] = "stays closed"
                first_reopen_seen = True
            continue
        if _TT_MARK_FULL_REOPEN in lower:
            if not first_reopen_seen:
                info["reopen"] = time_str
                first_reopen_seen = True
            elif "secondReopen" not in info:
                info["secondReopen"] = time_str
            continue
        if _TT_MARK_MOUNTAIN_CLOSES in lower:
            if "mountainCloses" not in info:
                info["mountainCloses"] = time_str
            elif first_reopen_seen and "secondMountain" not in info:
                info["secondMountain"] = time_str
            continue
        if _TT_MARK_FULL_CLOSES in lower:
            if "fullCloses" not in info:
                info["fullCloses"] = time_str
            elif not second_full_close_seen:
                info["secondFull"] = time_str
                second_full_close_seen = True
            continue
        # Race-postponed entries appear as time-less chunks tagged "RACE POSTPONED"
        # at the start. Only skip those (don't false-trigger on chunks that
        # mention "race postponed" elsewhere in trailing text).
        if lower.startswith("race postponed"):
            continue
        # Anything else with content is a race-name chunk; the first one wins
        # (decision: activity label is the headline race only).
        if chunk and "activity" not in info:
            cleaned = _clean_tt_activity(chunk)
            if cleaned and not cleaned.lower().startswith(("daytime racing", "evening racing")):
                info["activity"] = cleaned

    return info


def parse_tt_schedule(html: str) -> dict:
    """Parse the iomttraces schedule page into a list of TT closures-array entries.

    Returns:
        {
          "entries":  list[dict] - candidate entries in our schema,
          "year":     int        - schedule year detected from page heading,
          "warnings": list[str]  - non-fatal issues (missing year, missing tables,
                                   past-day rows with no times, etc.),
        }

    Failure-tolerant: any unexpected error returns {entries: [], warnings: [...]}.
    Tables 0 and 1 (Qualifying Week + Race Week) provide normal-schedule entries.
    Table 2 (Contingency Periods) contributes ONLY for dates not seen in 0/1 -
    those become minimal `contingency: true` entries with no times, matching how
    Sunday 7 June is hand-encoded today.
    """
    out: dict = {"entries": [], "year": None, "warnings": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        out["warnings"].append(f"BeautifulSoup parse error: {e}")
        return out

    year = parse_tt_schedule_year(html)
    if year is None:
        out["warnings"].append("could not detect schedule year from page heading")
        year = datetime.utcnow().year  # last-ditch fallback (validation will catch)
    out["year"] = year

    tables = soup.find_all("table")
    if not tables:
        out["warnings"].append("no <table> elements found on schedule page")
        return out

    regular_dates: set[str] = set()
    contingency_candidates: list[dict] = []

    for ti, table in enumerate(tables):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Identify which "section" this table represents from its first row.
        # The header row is a single cell whose text matches QUALIFYING WEEK / RACE WEEK / CONTINGENCY PERIODS.
        header_text = rows[0].get_text(" ", strip=True).lower()
        is_contingency_table = "contingency" in header_text

        for ri, row in enumerate(rows):
            if ri == 0:
                continue
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label_text = cells[0].get_text(" ", strip=True)
            cell_text = cells[1].get_text(" ", strip=True)

            day_info = _parse_tt_day_label(label_text, year)
            if not day_info:
                out["warnings"].append(f"could not parse day label: {label_text!r}")
                continue
            iso_date = day_info["date"]

            if is_contingency_table:
                # Capture for second-pass dedup against regular dates
                contingency_candidates.append({
                    "iso_date": iso_date,
                    "day_label": day_info["day_label"],
                    "label_text": label_text,
                })
                continue

            regular_dates.add(iso_date)
            cell_info = _parse_tt_schedule_cell(cell_text)

            entry: dict = {
                "event": "tt",
                "course": "mountain",
                "date": iso_date,
                "day": day_info["day_label"],
            }

            if cell_info.get("is_rest"):
                entry["restDay"] = True
                entry["activity"] = cell_info.get("activity") or "Rest day"
                out["entries"].append(entry)
                continue

            for k in ("mountainCloses", "fullCloses", "reopen",
                      "secondMountain", "secondFull", "secondReopen", "activity"):
                if cell_info.get(k) is not None:
                    entry[k] = cell_info[k]

            # If no times and no rest flag, this is a mutated past-day cell
            # (results/links replacing schedule). Record a warning and skip.
            if not any(entry.get(k) for k in ("mountainCloses", "fullCloses", "reopen", "restDay")):
                out["warnings"].append(
                    f"no closure times or rest marker for {iso_date} "
                    f"(likely a past day with results displayed)"
                )
                continue

            out["entries"].append(entry)

    # Second pass: any contingency-table date that didn't appear in regular
    # tables gets a minimal contingency-only entry, mirroring how Sun 7 June
    # is currently hand-encoded. We don't attempt to parse contingency times -
    # those are alternative-scenario times and don't fit our single-session schema.
    for c in contingency_candidates:
        if c["iso_date"] in regular_dates:
            continue
        out["entries"].append({
            "event": "tt",
            "course": "mountain",
            "date": c["iso_date"],
            "day": c["day_label"],
            "activity": "Contingency day — only used if rescheduling needed",
            "contingency": True,
        })

    return out


def extract_tt_entries_from_index(html: str) -> list[dict]:
    """Read the current TT entries from index.html between the
    `// AUTO_TT_SCHEDULE_START` and `// AUTO_TT_SCHEDULE_END` comment markers.

    Used by the validator for diff comparison. Returns [] if markers are
    missing (e.g. on a fresh clone before Phase 1 lands) - validation will
    then treat every parsed entry as "new".
    """
    m = re.search(
        r"//\s*AUTO_TT_SCHEDULE_START\b.*?(?P<body>.*?)//\s*AUTO_TT_SCHEDULE_END\b",
        html, re.DOTALL,
    )
    if not m:
        return []
    body = m.group("body")
    entries: list[dict] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                entry_text = body[start:i + 1]
                parsed = _parse_js_tt_entry(entry_text)
                if parsed:
                    entries.append(parsed)
                start = None
    return entries


def _parse_js_tt_entry(text: str) -> dict | None:
    """Extract fields from a TT closures entry's JS object literal."""
    fields: dict = {}
    for key in (
        "event", "course", "date",
        "mountainCloses", "fullCloses", "reopen",
        "secondMountain", "secondFull", "secondReopen",
        "activity", "day",
    ):
        m = re.search(rf"\b{key}:\s*'([^']*)'", text)
        if m:
            fields[key] = m.group(1)
    for key in ("restDay", "contingency", "pending"):
        if re.search(rf"\b{key}:\s*true\b", text):
            fields[key] = True
    return fields if fields.get("date") else None


def _hhmm_to_minutes(t: str | None) -> int | None:
    """Convert 'HH:MM' to minutes-since-midnight, or None if unparseable."""
    if not t:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h * 60 + mn


# Time fields compared when diffing a parsed entry against a current entry.
_TT_TIME_FIELDS = ("mountainCloses", "fullCloses", "reopen", "secondFull", "secondReopen")


def validate_tt_parse(
    parsed: list[dict],
    current: list[dict],
    today_iso: str,
) -> dict:
    """Run every validation gate on a parsed TT schedule.

    Returns:
        {
          'status': 'ok' | 'rejected',
          'reasons': [str, ...],          # human-readable rejection reasons, empty if ok
          'diff_summary': {
              'entries_parsed', 'entries_in_current_array',
              'past_days_skipped', 'differences',
              'missing_future_days', 'new_future_days',
              'max_shift_per_day_minutes',
              'pct_fields_shifted_over_2h',
          }
        }

    Rules applied (any failure rejects the whole parse - no partial applies):
        1. parser returned at least 1 entry
        2. parsed dates fall within the May-June window of the current year
        3. every per-entry time matches HH:MM and falls within 00:00-23:59
        4. per-entry ordering: mountainCloses ≤ fullCloses ≤ reopen, and the
           second-session times are strictly ordered after reopen
        5. every FUTURE day present in the current array is also in the parse
           (parser must not silently drop future entries)
        6. diff sanity: not more than 50% of compared times shift by >2 hours
        7. per-day diff sanity: no single day shifts by >3 hours

    Past-day immutability (decision 1) is enforced by silently skipping any
    parsed entry whose date is ≤ today. They appear in past_days_skipped for
    visibility but never trigger rejection.
    """
    diff_summary: dict = {
        "entries_parsed": len(parsed),
        "entries_in_current_array": len(current),
        "past_days_skipped": [],
        "differences": [],
        "missing_future_days": [],
        "new_future_days": [],
        "max_shift_per_day_minutes": {},
        "pct_fields_shifted_over_2h": 0.0,
    }
    reasons: list[str] = []

    # Rule 1: non-empty
    if not parsed:
        return {
            "status": "rejected",
            "reasons": ["parser returned 0 entries"],
            "diff_summary": diff_summary,
        }

    # Parse today
    try:
        today_dt = datetime.strptime(today_iso, "%Y-%m-%d").date()
    except ValueError:
        return {
            "status": "rejected",
            "reasons": [f"invalid today_iso passed to validator: {today_iso!r}"],
            "diff_summary": diff_summary,
        }

    # Rule 2: date window sanity
    try:
        parsed_dates = [datetime.strptime(e["date"], "%Y-%m-%d").date() for e in parsed]
    except (KeyError, ValueError) as e:
        reasons.append(f"unparseable date in parsed entries: {e}")
        parsed_dates = []
    if parsed_dates:
        d_min, d_max = min(parsed_dates), max(parsed_dates)
        if d_min.year != today_dt.year:
            reasons.append(
                f"parsed dates ({d_min} - {d_max}) span the wrong year for today ({today_dt})"
            )
        # TT 2026 runs late May to early June; reject anything outside May-June.
        for d in (d_min, d_max):
            if d.month not in (5, 6):
                reasons.append(f"parsed date {d} falls outside the TT May-June window")
                break

    # Rule 3+4: time format and per-entry ordering
    for e in parsed:
        for k in _TT_TIME_FIELDS:
            v = e.get(k)
            if v is None:
                continue
            if not re.fullmatch(r"\d{2}:\d{2}", v):
                reasons.append(f"{e.get('date','?')} {k}={v!r} is not HH:MM")
        mc = _hhmm_to_minutes(e.get("mountainCloses"))
        fc = _hhmm_to_minutes(e.get("fullCloses"))
        ro = _hhmm_to_minutes(e.get("reopen"))
        sf = _hhmm_to_minutes(e.get("secondFull"))
        sr = _hhmm_to_minutes(e.get("secondReopen"))
        date = e.get("date", "?")
        if mc is not None and fc is not None and mc > fc:
            reasons.append(f"{date} mountainCloses {e['mountainCloses']} > fullCloses {e['fullCloses']}")
        if fc is not None and ro is not None and fc > ro:
            reasons.append(f"{date} fullCloses {e['fullCloses']} > reopen {e['reopen']}")
        if sf is not None and ro is not None and sf <= ro:
            reasons.append(f"{date} secondFull {e['secondFull']} <= reopen {e['reopen']}")
        if sr is not None and sf is not None and sr <= sf:
            reasons.append(f"{date} secondReopen {e['secondReopen']} <= secondFull {e['secondFull']}")

    # Index by date and partition past vs future
    current_by_date = {e["date"]: e for e in current}
    parsed_by_date = {e["date"]: e for e in parsed}
    parsed_future = []
    for e in parsed:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if d <= today_dt:
            diff_summary["past_days_skipped"].append(e["date"])
        else:
            parsed_future.append(e)

    # Rule 5: future-day coverage. Every future entry currently in the
    # closures array must be in the parse, otherwise the parser is silently
    # dropping schedule we already know exists.
    for c_date in current_by_date:
        try:
            d = datetime.strptime(c_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d <= today_dt:
            continue
        if c_date not in parsed_by_date:
            diff_summary["missing_future_days"].append(c_date)
    if diff_summary["missing_future_days"]:
        reasons.append(
            f"parse is missing {len(diff_summary['missing_future_days'])} future "
            f"day(s) we already have in the array: {diff_summary['missing_future_days']}"
        )

    # Track NEW future days (parser found them, we didn't have them). Not a
    # rejection on its own - could be a legitimate addition by the organisers.
    for p_date in parsed_by_date:
        try:
            d = datetime.strptime(p_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d <= today_dt:
            continue
        if p_date not in current_by_date:
            diff_summary["new_future_days"].append(p_date)

    # Rules 6 + 7: diff sanity
    n_fields_compared = 0
    n_fields_shifted_over_2h = 0
    for e in parsed_future:
        c = current_by_date.get(e["date"])
        if not c:
            continue
        day_max_shift_min = 0
        for f in _TT_TIME_FIELDS:
            cv = c.get(f)
            pv = e.get(f)
            if cv and pv:
                cm = _hhmm_to_minutes(cv)
                pm = _hhmm_to_minutes(pv)
                if cm is None or pm is None:
                    continue
                n_fields_compared += 1
                delta = pm - cm
                abs_delta = abs(delta)
                if abs_delta > 120:
                    n_fields_shifted_over_2h += 1
                if abs_delta > day_max_shift_min:
                    day_max_shift_min = abs_delta
                if delta != 0:
                    diff_summary["differences"].append({
                        "date": e["date"],
                        "field": f,
                        "current": cv,
                        "parsed": pv,
                        "delta_minutes": delta,
                    })
        if day_max_shift_min > 0:
            diff_summary["max_shift_per_day_minutes"][e["date"]] = day_max_shift_min

    if n_fields_compared > 0:
        pct = n_fields_shifted_over_2h / n_fields_compared
        diff_summary["pct_fields_shifted_over_2h"] = round(pct, 4)
        if pct > 0.5:
            reasons.append(
                f"{n_fields_shifted_over_2h}/{n_fields_compared} ({pct:.0%}) "
                f"of compared times shift >2h vs current array — too large to auto-apply"
            )

    for date, sm in diff_summary["max_shift_per_day_minutes"].items():
        if sm > 180:
            reasons.append(
                f"{date} shifts {sm} minutes (>3h) vs current array — too large to auto-apply, flag for review"
            )

    return {
        "status": "rejected" if reasons else "ok",
        "reasons": reasons,
        "diff_summary": diff_summary,
    }


def write_tt_parsed_sidecar(
    parsed: dict, validation: dict | None, now: datetime
) -> None:
    """Write data/schedule-tt-parsed.json with the parser output + (optionally)
    the validation verdict. Always called, even on parse/validation failure -
    the sidecar IS the observation surface during the Phase-3 watch period."""
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_url": IOMTT_SCHEDULE_URL,
        "parser_version": TT_PARSER_VERSION,
        "year_detected": parsed.get("year"),
        "warnings": parsed.get("warnings", []),
        "validation": validation or {"status": "not_run", "reasons": []},
        "entries": parsed.get("entries", []),
    }
    SCHEDULE_TT_PARSED_PATH.write_text(json.dumps(payload, indent=2))


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

    # --- 2b. TT schedule parse + validate (observation only, Phases 2-3) ---
    # Run the candidate parser against the same iomttraces page already fetched
    # above, validate against the current closures array in index.html, and write
    # everything to data/schedule-tt-parsed.json for observation. Phase 4 will
    # later read this sidecar and actually overwrite the closures block - until
    # then this code never touches the route checker's data.
    log("Parsing TT schedule (observation-only, Phases 2-3)...")
    tt_parsed: dict = {"entries": [], "warnings": ["iomttraces page not fetched"], "year": None}
    tt_validation: dict | None = None
    if page:
        try:
            tt_parsed = parse_tt_schedule(page)
            log(
                f"  parser: year={tt_parsed.get('year')} "
                f"entries={len(tt_parsed.get('entries', []))} "
                f"warnings={len(tt_parsed.get('warnings', []))}"
            )
        except Exception as e:
            log(f"  TT parser error: {e}")
            tt_parsed = {"entries": [], "warnings": [f"parser exception: {e}"], "year": None}
        try:
            current_tt = extract_tt_entries_from_index(original_html)
            log(f"  current closures array contains {len(current_tt)} TT entries")
            tt_validation = validate_tt_parse(
                tt_parsed.get("entries", []),
                current_tt,
                now.strftime("%Y-%m-%d"),
            )
            log(f"  validation: {tt_validation['status']}")
            for r in tt_validation.get("reasons", []):
                log(f"    - {r}")
            ds = tt_validation.get("diff_summary", {})
            log(
                f"    diff: {len(ds.get('differences', []))} field changes, "
                f"max-shift days: {len(ds.get('max_shift_per_day_minutes', {}))}, "
                f"missing-future: {len(ds.get('missing_future_days', []))}, "
                f"past-skipped: {len(ds.get('past_days_skipped', []))}"
            )
        except Exception as e:
            log(f"  TT validation error: {e}")
            tt_validation = {"status": "rejected", "reasons": [f"validator exception: {e}"], "diff_summary": {}}

    # Always write the sidecar - this IS the observation surface during Phases 2-3.
    try:
        write_tt_parsed_sidecar(tt_parsed, tt_validation, now)
        log("  schedule-tt-parsed.json updated")
    except Exception as e:
        log(f"  schedule-tt-parsed.json write error: {e}")

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
