# IDEAS — running backlog

A running list of improvements I want to make to the IoM Road Closures app, collected from my own thinking and from things Facebook users have flagged. Not a roadmap, not a promise — just where to look next when there's time to build.

---

## Auto-updating schedule, not just news

Build a version where the app automatically detects changes to the official race schedule and updates the schedule data itself, so the schedule and the route checker results are always accurate — not just the news headlines.

- **Source:** Me. This is the big one.
- **Status:** Not started.

## More frequent Manx Radio news updates

The news currently refreshes twice a day. Increase that frequency so it feels closer to live, because some race news is time-critical and a twice-daily delay is too slow.

- **Source:** Me.
- **Status:** Not started.

## Add Isle of Man car rallies and other motorsport road-closure events

Beyond the bike races (TT, MGP, Pre-TT Billown, Southern 100), the island runs a string of car-based motorsport events through the summer that also close roads: rallies, hill climbs, sprints. None of these are currently covered. Adding them would complete the picture for residents and visitors who get caught out by closures they don't expect.

**Research task first**, before any build:

- Identify the events that actually close roads (which season weeks, which roads, which organising bodies).
- Find each event's authoritative published schedule (organising body's website, DOI closures notices, Isle of Man Newspapers, etc.).
- Assess whether each source is scrapeable — some will be tidy HTML tables, some will be PDFs only, some may not have any public schedule at all.

Then build, reusing the existing trust-tier alerts and parser/validator architecture so each new source plugs in the same way the Southern 100 RSS watcher does.

**Important caveat:** each new source adds its own format fragility and per-year maintenance cost. Every August iomttraces shifts something subtly; every new source means another upstream supplier whose website you depend on. Budget for that overhead in the decision to add.

- **Source:** Originally me / Facebook users; re-scoped during the TT auto-update work to include hill climbs and sprints rather than rallies alone.
- **Status:** Not started. Research task first; build only after the source inventory is in.

## Pull live status from the DOI matrix boards

Right now the app predicts road state from the published schedule. The matrix boards — the variable message signs DOI operates along the course — show the actual real-time state (open, shut, one-way). If we could pull that feed in, the app would reflect what's really happening on the ground rather than what's planned, which matters when sessions run early or late or an unplanned closure happens. Related to the auto-updating-schedule idea above but more ambitious, because it would need DOI to provide a data feed.

**Cross-cutting note: a DOI feed is the long-term north star.** The recurring obstacle that surfaces across multiple items in this file — MGP scheduling (`manxgrandprix.co.uk` hard-blocks scrapers; HTTP 503 on the root and `/racing/`, 404 on `/racing/page/road-closures-and-contingencies/` — the inconsistent status codes are the tell of a bot-block layer like Cloudflare), car-rally road closures, and live road status itself — is the same: authoritative IoM road-closure data is scattered across organisers and inconsistently accessible. DOI runs the roadside matrix boards *and* the MGP infrastructure, *and* is the road authority that approves rally road closures. A single official DOI data feed could potentially solve MGP scheduling, rally coverage, and matrix-board live status all at once. Long-term aspiration; possibly worth a conversation with DOI if the opportunity arises.

- **Source:** Prompted by a Facebook user's question (they were trying to resolve conflicting matrix-board info on the DOI app and wondered if our app already used that data).
- **Status:** Not started.

## Watch Southern 100 news feed for schedule revisions

Southern 100 Racing (the same organiser for the Pre-TT Billown, the Southern 100 itself, and Post-TT Road Races) publishes schedule changes as **news articles** on southern100.com — not as edits to a fixed schedule page. Examples seen in May 2026: "Revised Schedule for Saturday 23rd May (Afternoon)", "STATEMENT ISSUED FROM THE SOUTHERN 100 ROAD RACES". The current scraper only watches the TT schedule at iomttraces.com, so we are blind to these revisions in real time — which is what prompted this entry.

**Chosen approach (after investigation on 25 May 2026):** Mirror the existing Manx Radio scraping pattern but pointed at Southern 100. Watch the news feed for new articles whose title matches schedule-related keywords ("revised schedule", "schedule revision", "postponed", "contingency", "new timetable", and similar). When one appears, raise a red banner on the page with a direct link to the article so I (or a visitor) can read the prose and act on it.

**Data source confirmed:** The site exposes a clean WordPress RSS feed at `southern100.com/feed/` (HTTP 200, `application/rss+xml`, 10 most recent articles per fetch with proper RFC 822 timestamps). We will parse this XML feed directly rather than scrape the HTML `/news/` page — RSS is more robust to layout changes and gives us structured publication dates for free. Note: `/news/feed/` looks like it should also be an RSS endpoint but is actually a WordPress comment-feed URL that returns HTTP 403; ignore it.

**Important limitation to be honest about:** The actual revised *times* inside Southern 100 articles are embedded as JPG images (e.g. `Saturday-23rd-May-2026-Afternoon.jpg`), not as HTML tables. We can detect that a revision happened and link the user to the article, but we cannot auto-extract the new times into the app's schedule data without OCR. So this feature is a smart "human, please look at this" alert — not an auto-sync of schedule data. Adding OCR would be a much bigger jump and is not in scope here.

- **Source:** Me, after the Pre-TT Billown organisers posted a same-day schedule revision in May 2026 that we missed entirely.
- **Status:** Investigated and pre-checked. RSS source confirmed working. Approach decided (RSS-based news watcher with keyword filter). Not yet built.

## Geographic nuance: Onchan → Douglas during Mountain Course closures

When the Mountain Course is closed, Castletown → Douglas now correctly returns a "small detour" verdict pointing at the NSC junction bypass (this was sorted in June 2026). The mirror-image case for Onchan → Douglas is still treated as plain "open" — but in reality the natural approach from Onchan into Douglas centre is via the A2 / Glencrutchery Road, and Glencrutchery is on the closed course. The route checker is currently optimistic for this case.

To encode it properly we'd need solid local or official guidance on the bypass: what's the equivalent of the NSC turn-off for traffic coming from Onchan? Likely something via Bedstead / Cronk-ny-Mona / a back route into central Douglas, but I want to verify with someone who lives or marshalls there before adding a rule that misleads visitors. The Castletown fix had verified ground-truth before encoding; this one should too.

Possible model change when we do build it: Onchan likely needs its own treatment (either as a small zone of its own or as an edgePlace), since it's geographically distinct from both Douglas-centre (which is on the course) and the south-coast cluster.

- **Source:** Surfaced while building the Castletown / Quarterbridge / NSC junction nuance in June 2026.
- **Status:** Deferred pending verified local information on the correct bypass route. Not started.

## Fill in Manx GP / Classic TT schedule entries (currently empty)

The Manx Grand Prix and Classic TT run mid-to-late August on the same Snaefell Mountain Course as the TT. Our `closures` array currently has placeholder entries for the MGP date range (16–28 August 2026) tagged `pending: true` with no closure times — visitors querying any of those dates get "POSSIBLY — exact times not yet published" rather than a real answer. The official daily schedule needs to be entered manually.

**Auto-update is blocked.** `manxgrandprix.co.uk` hard-blocks scrapers (the 503-on-root, 404-on-specific-page pattern surfaced during the southern100.com investigation). The TT auto-update approach (`curl` + BeautifulSoup against a stable HTML schedule table) does not work against MGP. Three resolution paths in increasing ambition:

1. **Manual encoding** from the official PDF / press release when the schedule is published. Reliable, but a yearly chore.
2. **Headless-browser scraping** (Playwright / Puppeteer) — emulates a real Chrome session, bypasses the bot-block. Heavier dependency stack, bigger CI footprint, more failure modes, but plug-compatible with the existing parser/validator/applier architecture once the fetch is solved.
3. **Cowork** (see next item) — same as headless-browser but human-like and able to read images too. Open question on unattended scheduling.
4. **DOI data feed** — see the cross-cutting note in the matrix-boards entry above. Long-term north star.

- **Source:** Surfaced repeatedly during the TT auto-update work — MGP is the most conspicuous remaining gap once TT is automated.
- **Status:** Not started. Becomes top priority as August 2026 approaches; before then, the placeholder `pending: true` entries are doing the right thing (warning users that times aren't published yet).

## Investigate Cowork for the hard-to-scrape schedules

Two stubbornly inaccessible sources for the app both require something that behaves like a human browser to extract:

- **Southern 100** — revised times are embedded in JPG images (e.g. `Saturday-23rd-May-2026-Afternoon.jpg`) that we can't cheaply OCR.
- **Manx Grand Prix** — `manxgrandprix.co.uk` hard-blocks any automated request (503 / 404 pattern).

Cowork can read both when run manually — it browses like a person, reads images, and bypasses bot-blocks. The reading is the easy part for Cowork. **The open question is whether Cowork can run unattended on a schedule** (cron-like, no human kickoff). That's the feasibility unknown that determines whether this is worth investing in.

If unattended scheduled runs are possible, this single approach unlocks both S100 image-time extraction AND MGP scheduling at once — far higher leverage than building one-off solutions for each source. If not, it stays a manual-kickoff tool and the per-source builds remain on the table.

- **Source:** Synthesised from the S100 OCR gap and the MGP bot-block, both surfaced during the auto-update work in May–June 2026.
- **Status:** Not started. Feasibility check is the first deliverable — verify whether scheduled / unattended Cowork runs are possible at all, then decide whether to build on top.
