# Contributing

Thanks for wanting to help preserve the web. Internet Historian is deliberately tiny — the
whole engine is one file, [`historian.py`](historian.py) — so it's an easy codebase to jump
into.

## Dev setup

```bash
git clone https://github.com/ssskay/internet-historian.git
cd internet-historian

# Editable install puts the `internet-historian` / `historian` commands on your PATH
# and pulls in the one dependency (requests).
python3 -m pip install -e .
```

You don't need API keys or a network connection to work on the code — the tests mock the HTTP
layer entirely. Keys are only needed to actually archive things.

State (the `queue.db`, logs, and `config.toml`) lives in your repo checkout when those files
exist there, otherwise in a per-user platform directory (e.g.
`~/Library/Application Support/internet-historian/` on macOS). `internet-historian status`
always prints exactly where it's reading from.

## Run the tests

```bash
python3 -m pytest        # or: python3 -m unittest test_historian test_discover test_bookmarks -v
```

CI runs the same suite on macOS and Linux across Python 3.10–3.12. Please keep it green, and
add a test for any behavior change — the failure-classification logic (throttle vs. dead) is
the heart of the tool and every edge case there deserves coverage.

## The to-do list is the cross-platform port

The engine is pure Python and already cross-platform. Only **two** pieces are macOS-specific,
and porting them is essentially the entire open roadmap:

1. **The background heartbeat** uses `launchd`. Linux wants a **systemd timer** (or cron);
   Windows wants **Task Scheduler**. Each just needs to run `internet-historian drain` on an
   interval.
2. **Key storage** uses the macOS Keychain via the `security` command (there's already an
   `IA_ACCESS_KEY` / `IA_SECRET_KEY` env-var fallback). A cross-platform
   [`keyring`](https://pypi.org/project/keyring/) backend would make it native everywhere.

See the [open issues](https://github.com/ssskay/internet-historian/issues) — the ones tagged
**good first issue** and **help wanted** are exactly these. Pick one, open a PR, keep the
existing logging style (new code paths get log lines too), and you're a contributor.

## Style

- One file, standard library first, `requests` the only runtime dependency. Keep it that way
  unless there's a strong reason.
- Match the surrounding code: the same logging style, the same plain-language log messages.
- Failures are classified carefully on purpose. If you touch that logic, read the SPN2 notes
  in [`SKILL.md`](SKILL.md) first so the throttle-vs-dead distinction stays intact.
