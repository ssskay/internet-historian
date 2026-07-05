---
name: internet-historian
description: Quietly preserve the web things you love, forever — via the Internet Archive. Use whenever the user wants to archive / save / back up / "keep a copy of" web content: specific URLs (a single link, a pasted list, a text file), everything linked from a page, OR everything about a subject or fandom they name ("archive Chiikawa stuff", "preserve the real pages about X", "be a Chiikawa internet historian") — in that last case the skill finds the real/official pages itself. Also use to check how the archive queue is doing, or find out why a page hasn't been preserved yet. Motivating case: saving Chiikawa (ちいかわ) pages before they disappear, but it works for any subject or URL.
---

# Internet Historian

Internet Historian quietly preserves the web things you love, forever. You hand it URLs;
it patiently makes sure each one is saved into the Internet Archive's Wayback Machine and
never gives up on a live page. It runs on its own in the background (a launchd job every 10
minutes) and optimizes for **never losing a URL**, not for speed. Throttling from the
Archive is normal weather, not failure — the tool simply waits and tries again.

All logic lives in the `internet-historian` command (installed on the user's PATH via
`pipx install internet-historian`; a shorter `historian` alias is installed too). This skill
is a **thin** control surface: it knows which command to run and how to read the output. Do
not reimplement queue, backoff, or SPN2 logic here — call the CLI.

## The four verbs

Run the `internet-historian` command from anywhere (it's on the PATH). If it isn't found, the
tool isn't installed — tell the user to run `pipx install internet-historian`.

| Intent | Command |
|--------|---------|
| Preserve one or more URLs | `internet-historian add URL [URL ...] [--collection NAME]` |
| Preserve a whole list from a file | `internet-historian add --file /path/to/urls.txt [--collection NAME]` |
| "How's the archive doing?" | `internet-historian status` |
| "Why hasn't X been saved?" | `internet-historian diagnose` |
| Install / repair the background job | `internet-historian setup` |

Also available: `pause` / `resume` (a URL or a whole `--collection`), `check` (raw IA slot
availability), and `install-skill` (re-install this skill). `drain` exists but you never call
it by hand — launchd runs it.

### add

- Accept URLs straight from the conversation: a single link, a pasted list, or a file.
- Group related URLs with `--collection` (e.g. `--collection chiikawa`). A collection is
  just a tag — no setup needed; naming one that doesn't exist yet creates it.
- **Periodic recapture is opt-in per collection.** By default a page is archived once and left
  alone. If the user wants a collection kept fresh (re-snapshotting pages that change over
  time), add it to the `[collections]` table in `config.toml` with `refresh_days`, e.g.
  `chiikawa = { refresh_days = 30 }`. Archived pages older than that are re-queued automatically
  on the next drain; collections not listed there never recapture.
- `add` normalizes URLs (drops `#fragments`, `utm_*`/`fbclid`/`gclid` junk, lowercases the
  host) and silently skips anything already tracked. It prints `added N, skipped M, ...`.
- **"Archive everything linked from this page"**: fetch the page, extract the outbound
  links, **show the user the list and confirm** before enqueuing, then write them to a temp
  file and run `add --file`. Don't enqueue dozens of URLs without a look.

### Discovery — "archive everything about <subject>"

This is the internet-historian flow. When the user names a **subject** instead of URLs
("archive Chiikawa stuff", "preserve the real pages about <band / show / artist>"), YOU do
the finding. Don't ask for URLs — go get them:

1. **Search** the web for the subject's real, primary sources. Prioritize, in order:
   official sites (the thing's own homepage, official shop, official anime/movie/publisher
   pages), then encyclopedic/press references (Wikipedia in relevant languages, Wikidata,
   reputable articles). Search in the subject's native language too — for ちいかわ that
   means Japanese queries, which surface the official `.jp` sites.
2. **Default to real/primary pages, not fan pages.** The user's phrasing "real pages, not fan
   pages" is the default intent: skip fan wikis, 考察/analysis blogs, and unofficial
   databases unless the user explicitly asks for them. (If a fandom's *primary* medium is a
   social account — e.g. Chiikawa's manga is serialized on X — include it, flagged; see
   routing policy.)
3. **Verify each candidate is live** before queuing (a quick HTTP check). A `403` to your
   own checker is fine — that's often just bot-blocking; the Archive's crawler usually gets
   through, and the queue flags `403` rather than killing it. Drop only genuine 404s/dead
   hosts.
4. **Show the user the curated list, grouped** (official / encyclopedic / social-best-effort),
   and confirm before queuing. They may add or cut a few.
5. **Queue** the confirmed set to a subject-named collection: `add <urls...> --collection
   <subject>`. Then tell them it's preserving in the background and they can check `status` anytime.
   (In shell terms: `internet-historian add <urls...> --collection <subject>`.)

You do NOT need to run `drain` or babysit captures — the background launchd job archives
everything on its own, patiently, over the following minutes/hours.

### status

Prints a human summary: counts by status and collection, the oldest waiting URL, any dead
links with reasons, recent errors, and live IA slot availability. Relay it plainly. A URL
sitting in `queued` with a throttle error is **working as intended**, not stuck.

### diagnose

Use when the user asks why something isn't archived. It classifies each problem URL and states,
in plain language, the distinction that matters:

- **throttled (IA-side)** — the Archive is busy; the queue retries automatically. Nothing
  to do. *Never* describe this as a failure or suggest intervention.
- **likely dead link (strikes: N/3)** — the target's own server keeps answering badly
  (404 / DNS failure / blocked). Being confirmed before giving up.
- **dead** — confirmed gone: marked dead after 3 candidate-dead results spaced a day apart.
- **stuck — investigate** — a genuinely unclassified error; worth a look.

When you relay `diagnose`, preserve that throttle-vs-dead distinction. It is the whole point.

### setup

Installs (or cleanly reinstalls) the background launchd job that does the actual archiving.
Idempotent. Run it once at install time, or again if the job ever stops showing up in
`launchctl list | grep com.internet-historian.drain`.

## Routing policy — Internet Archive only (v1)

Every URL goes to the Internet Archive's Save Page Now (SPN2). There is intentionally no
archive.today and no local-copy backend yet.

- **Social-media URLs (x.com, twitter.com, instagram.com, facebook.com, tiktok.com):**
  enqueue them best-effort, but **flag to the user** that captures behind login walls often
  fail — the Archive gets a login page, not the content. That is expected, not a bug. Don't
  promise these will succeed.
- Everything else (fan sites, blogs, wikis, individual pages) is the sweet spot.

## SPN2 facts (so you never re-derive these)

Auth, limits, and error handling are already implemented in `historian.py`. This block is
here so you can answer questions without guessing.

- **Auth:** header `Authorization: LOW <accesskey>:<secret>`. Keys live in the macOS
  Keychain (`ia-s3-access`, `ia-s3-secret`); the code reads them. Never print or ask for them.
- **Submit:** `POST https://web.archive.org/save/` with `url=...` → `{"job_id": ...}`.
- **Poll:** `GET /save/status/<job_id>` → `pending` / `success` / `error`. Batch via
  `POST /save/status` with `job_ids=`.
- **Slots:** `GET /save/status/user?_t=<rand>` → `{"available": N, "processing": M}`.
  Concurrency cap is 12 for authenticated users; the code always reads `available` and
  leaves a headroom of 2 free for the user's own manual browser saves.
- **System health:** `GET /save/status/system`; if not `ok`, the drain skips that cycle.
- **Limits that matter:** 10 captures/day **per URL** (the code caps attempts at 5/day/URL
  to stay well clear). 100k/day account-wide is irrelevant at this scale.
- **Server-side dedup:** submits carry `if_not_archived_within=30d`. If a recent snapshot
  already exists, the Archive returns it instead of recapturing — the tool records that as
  **archived** (a preservation success), not a failure.
- **Throttle vs. death:** transient errors (`user-session-limit`, `proxy-error`,
  `too-many-daily-captures`, 429/5xx/timeouts, …) retry forever with backoff and never
  count toward death. Only candidate-dead errors (`not-found`, `invalid-host-resolution`,
  `blocked-url`, `forbidden`, `invalid-url`), confirmed 3× a day apart, mark a URL `dead`.

## Worked example

> "Archive these three to the chiikawa collection: <url1> <url2> <url3>"

```
internet-historian add <url1> <url2> <url3> --collection chiikawa
```

Then reassure the user they're queued and the background job will preserve them patiently;
they can check anytime with `status`. Nothing else is required of them.
