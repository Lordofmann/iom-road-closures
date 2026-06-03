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

## Include Isle of Man car rallies

Add the schedules and road closure information for the car rallies that happen on the island over the summer. These are a separate type of event that also closes roads, and they aren't covered at all today.

- **Source:** Me / Facebook users.
- **Status:** Not started.

## Pull live status from the DOI matrix boards

Right now the app predicts road state from the published schedule. The matrix boards — the variable message signs DOI operates along the course — show the actual real-time state (open, shut, one-way). If we could pull that feed in, the app would reflect what's really happening on the ground rather than what's planned, which matters when sessions run early or late or an unplanned closure happens. Related to the auto-updating-schedule idea above but more ambitious, because it would need DOI to provide a data feed.

**Possible bundled win — Manx Grand Prix coverage.** When we investigated southern100.com (see next item), we also tried to fetch manxgrandprix.co.uk and found it's hard-blocked to automated requests (HTTP 503 on the root and `/racing/`, 404 on `/racing/page/road-closures-and-contingencies/` — the inconsistent status codes are the tell of a bot-block layer like Cloudflare). DOI runs both the matrix boards *and* the MGP infrastructure, so if we ever pursue an official DOI data feed, it would likely solve MGP coverage and matrix-board live status at the same time. Worth raising both together if there's ever a conversation with DOI.

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
