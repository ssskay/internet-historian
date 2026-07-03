# Internet Historian 🏛️

**Quietly preserve the web things you love, forever.**

Internet Historian is a tiny, patient tool that saves the web pages you care about into the
[Internet Archive's Wayback Machine](https://web.archive.org/) — and keeps trying until each
one is safely preserved. You hand it URLs (or just name a thing you love); it runs quietly in
the background on your Mac and makes sure they don't disappear.

It was built for one purpose: **saving [ちいかわ (Chiikawa)](https://en.wikipedia.org/wiki/Chiikawa)
pages before they vanish** — official sites, the anime, the shops, the wikis. But it works for
anything: a band, a webcomic, a favorite blog, a fandom, a single irreplaceable page.

> It optimizes for **never losing a URL**, not for speed. The Wayback Machine throttles when
> it's busy — Internet Historian treats that as normal weather, waits, and tries again. And
> again. For as long as it takes.

---

## Why this exists

Web pages die. Fan sites go offline, shops close, links rot. The Internet Archive can preserve
almost any public page — but only if someone asks it to, at the right time, and keeps asking
when the Archive is too busy to answer. That "keep patiently asking" part is tedious to do by
hand. Internet Historian does it for you, forever, in the background.

- 🗃️ **A patient queue, not a scraper.** Add URLs once; it preserves them and remembers what's done.
- 🔁 **Throttle-proof.** Built around the Internet Archive's real rate limits. Being throttled is expected, not an error.
- 💀 **Knows dead from busy.** A 404 or a vanished domain gets flagged as dead — but only after real confirmation. A busy Archive never counts against a page.
- 🧠 **Talks to you (optionally).** Ships with a [Claude Code](https://claude.ai/code) skill so you can just say *"archive Chiikawa stuff"* and it finds the real pages itself.
- 🔒 **Your keys stay yours.** API keys live in your macOS Keychain, never in this repo.

## Requirements

- **macOS** (uses the Keychain and `launchd`) — see [Windows / Linux](#windows--linux) below.
- **Python 3.11+** (for `tomllib`). `python3 --version` to check.
- A **free [archive.org](https://archive.org) account** and its S3-style API keys (setup walks you through this).
- One dependency: [`requests`](https://pypi.org/project/requests/).

## Quickstart

```bash
# 1. Get the code
git clone https://github.com/ssskay/internet-historian.git ~/internet-historian
cd ~/internet-historian

# 2. Install the one dependency
pip3 install --break-system-packages requests   # or: pip3 install requests

# 3. Set it up — this connects your Internet Archive account and starts the background job
python3 historian.py setup
```

`setup` will walk you through getting your free API keys (it opens the right page), store them
in your Keychain, verify them, and install the background job that does the archiving. That's it.

Now preserve something:

```bash
python3 historian.py add https://www.anime-chiikawa.jp/ --collection chiikawa
python3 historian.py status
```

You never have to run anything on a schedule — a `launchd` job wakes up every 10 minutes,
preserves whatever's queued, and goes back to sleep.

## The commands

| Command | What it does |
|---------|--------------|
| `setup` | Connect your Archive account + install the background job (run once) |
| `add URL [URL ...] [--collection NAME]` | Queue URLs for preservation |
| `add --file urls.txt [--collection NAME]` | Queue a whole text file of URLs |
| `status` | See what's preserved, queued, or dead |
| `diagnose` | Plain-English "why isn't this archived yet?" — throttled vs. genuinely dead |
| `pause` / `resume` | Stop/restart a single URL or a whole `--collection` |
| `check` | Raw Internet Archive capacity right now |

Collections are just tags — group your Chiikawa pages, your webcomics, your blogs. No setup needed.

## The Claude Code skill (optional, but lovely)

If you use [Claude Code](https://claude.ai/code), install the bundled skill and you can drive
the whole thing in plain language — no commands to remember:

```bash
ln -sfn ~/internet-historian ~/.claude/skills/internet-historian
```

Then, in any Claude Code session:

> **You:** archive Chiikawa stuff
>
> **Claude:** *searches the web for the real official ちいかわ pages, checks they're live,
> shows you the list, and queues them to a `chiikawa` collection — then the background job
> preserves them.*

> **You:** how's my archive doing?  ·  why hasn't that shop page saved yet?

It knows to lead with **real, primary pages** (official sites, Wikipedia, press) over fan
wikis unless you ask otherwise — you become a proper internet historian without lifting a finger.

## How it works (the 60-second version)

```
  you ──add──▶ queue.db (SQLite) ◀──drain── launchd (every 10 min)
                                              │
                                              ▼
                                   Internet Archive "Save Page Now"
```

- **`historian.py`** is the whole engine: a queue + an Internet Archive client, in one file.
- **`queue.db`** remembers every URL and its state (queued → submitted → archived / dead).
- **`launchd`** is the heartbeat: it runs `historian.py drain` on a timer so you don't have to.
- Captures use server-side dedup (`if_not_archived_within=30d`), so already-saved pages aren't
  needlessly recaptured — they're just recorded as preserved.

Failures are classified carefully: rate-limits and timeouts retry forever with backoff; only a
page whose own server keeps answering badly (404, dead DNS, blocked) is marked dead, and only
after 3 confirmations spaced a day apart. Being throttled by a busy Archive **never** kills a URL.

## What it does NOT do (by design, for now)

- **One backend: the Internet Archive.** No archive.today, no local copies yet. (Local
  snapshots via [`monolith`](https://github.com/Y2Z/monolith) are a planned future backend.)
- **No auto-discovery.** You add pages (or ask the skill to find them); it doesn't crawl the
  web hunting for new ones on its own.
- **Social media is best-effort.** X/Twitter and Instagram hide content behind login walls, so
  the Archive often captures a login page instead. Internet Historian will still try, but flags
  these — it's expected, not a bug.

## Windows / Linux

Not yet — but it's close. The **engine** (`historian.py`: the queue, the SQLite store, the
Archive client) is pure Python and already cross-platform. Only **two** pieces are macOS-specific:

1. **Key storage** uses the macOS Keychain (via the `security` command). *There's already an
   env-var fallback* — set `IA_ACCESS_KEY` / `IA_SECRET_KEY` and the engine works anywhere.
2. **The background heartbeat** uses `launchd`. On Linux you'd use a systemd timer or cron; on
   Windows, Task Scheduler — each just needs to run `python3 historian.py drain` on a timer.

Porting means swapping those two. **Contributions very welcome** — that's the whole to-do list.

## Configuration

Knobs live in [`config.toml`](config.toml) (no secrets there). Sensible defaults ship in the
box: 10-minute drain interval, 2 capture slots left free for your own browsing, 30-day dedup
window, and a conservative per-URL daily attempt cap. Tweak if you like; the defaults are fine.

## License

[MIT](LICENSE) — do what you like. Preserve the web things *you* love.
