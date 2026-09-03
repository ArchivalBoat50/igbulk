# igbulk — Instagram bulk downloader

Saving a batch of Instagram Reels or posts means, in practice, pasting links
one at a time into an ad-laden web tool that you cannot script, cannot point at
a folder, and cannot call from anything else. igbulk does the same job locally
and in bulk: paste a hundred links, or a chat log with a hundred links buried in
it, and get the files. It has three front ends over one engine — a local web UI,
a CLI, and an MCP stdio server — so the same code path serves a human pasting
into a box, a cron job, and an agent calling it as a tool. Everything is Python
standard library except `yt-dlp`, which runs as a subprocess rather than an
import.

---

## Three front ends, one engine

```
   paste box                  terminal                 agent / LLM
 (browser :8722)              (igdl …)             (MCP over stdio)
       │                          │                        │
┌──────▼───────┐          ┌───────▼──────┐          ┌──────▼───────┐
│  server.py   │          │    cli.py    │          │    mcp.py    │
│ HTTP + JSON  │          │   argparse   │          │  JSON-RPC 2  │
│     API      │          │              │          │              │
└──────┬───────┘          └───────┬──────┘          └──────┬───────┘
       └──────────────────────────┼────────────────────────┘
                                  │
                        ┌─────────▼─────────┐
                        │      jobs.py      │  queue, worker pool,
                        │   JobManager/Job  │  versioned live state,
                        └─────────┬─────────┘  cancel, retry
                                  │
                ┌─────────────────┴─────────────────┐
       ┌────────▼────────┐                 ┌────────▼────────┐
       │    links.py     │                 │    engine.py    │
       │ extract, canon- │                 │  yt-dlp driver, │
       │ icalise, dedupe │                 │  progress parse,│
       │  (pure, no I/O) │                 │  error classify │
       └─────────────────┘                 └────────┬────────┘
                                                    │ subprocess
                                            ┌───────▼────────┐
                                            │     yt-dlp     │
                                            └────────────────┘
```

The engine has no idea how it was invoked. `Engine.download()` takes a URL, an
`Options` dataclass, an optional progress callback and an optional
`threading.Event` for cancellation, and returns a dict. It never touches
`sys.argv`, never writes to a terminal, never knows about HTTP. `jobs.py`
likewise only deals in `Item` and `Job` objects with a `status` string.

The payoff is that the three front ends are thin and none of them is privileged.
`cli.py` builds `Options` from argparse; `server.py` builds the same `Options`
from a JSON body; `mcp.py` builds it from MCP tool arguments — and all three
then call the same `JobManager.create()`. Adding the MCP server did not require
touching the engine at all. It also means the interesting logic is testable
without a network, a terminal, or a browser: the entire test suite constructs
jobs with `start=False` and calls pure functions.

---

## Read this first: Instagram requires a login

As of July 2026 Instagram serves **nothing** to anonymous requests. All five
routes were tested from this machine and every one is closed: [CONFIRM: dates,
machine, and exact responses are from the author's own testing; there is no
saved capture in the repo to re-verify against]

| Route | Result |
| --- | --- |
| `yt-dlp`, no cookies | `Instagram sent an empty media response` |
| `graphql/query` with public `doc_id` | `403` |
| `/embed/captioned/` page | `200`, but a JS shell with no media URLs in it |
| `i.instagram.com/api/v1/media/{id}/info/` | `403 login_required` |
| `www.instagram.com/api/v1/media/{id}/info/` | `302` → login |

The fourth is the endpoint the public downloader sites are built on. Those sites
still work because *their server* is logged in — service accounts, cookie pools,
or a paid scraping API. [CONFIRM: this is an inference from the observed
behaviour, not something confirmed from the inside of any of those services.]
You are anonymous to them; they are not anonymous to Instagram.

The conclusion is the useful part: public-only downloading does not work, no
matter which tool you use. Any project in this space either authenticates or
lies about what it does. igbulk authenticates with *your* session, and says so.
The negative result also shaped the design — see "Block before you spend a
request" below.

Two ways to authenticate, and the difference matters if you want to automate:

| Approach | Unattended? |
| --- | --- |
| **`cookies.txt` file** — `--cookie-file FILE` | Yes, fully. No prompts; works in cron, launchd, agents |
| **Firefox** — `--cookies firefox` | Yes. `cookies.sqlite` is unencrypted: no Keychain, no Full Disk Access [CONFIRM] |
| **Chrome/Brave/Edge** — `--cookies chrome` | One-time macOS Keychain prompt; *Always Allow* makes it silent after [CONFIRM] |
| **Safari** — `--cookies safari` | No. Needs Full Disk Access granted to the exact calling binary — brittle [CONFIRM] |

### The automatable setup

Export a cookie file once, with a browser extension such as *Get cookies.txt
LOCALLY*, while logged into instagram.com. Then:

```sh
chmod 600 ~/.igbulk-cookies.txt
export IGBULK_COOKIES=~/.igbulk-cookies.txt      # add to ~/.zshrc
```

Every front end picks that up through `engine.env_auth()`, so nothing has to
pass credentials per call. `IGBULK_COOKIES_FROM=firefox` does the same for the
browser route. Per-run flags (`--cookie-file`, `--cookies`) and API fields
(`cookie_file`, `cookies_from`) override the environment; a cookie file wins
over a browser when both are set.

That file holds a live login session, so treat it like a password: `chmod 600`,
do not commit it, re-export when Instagram invalidates it.

Keep `--jobs` at 3 or below. Hammering Instagram while logged in is what gets
accounts rate-limited; use 1 if you start seeing rate-limit errors. [CONFIRM:
3 is a conservative default chosen by hand, not a measured threshold.]

---

## Design decisions

### Standard library only, apart from yt-dlp

There is no `requirements.txt`, no virtualenv, no `pip install`. The web server
is `http.server.ThreadingHTTPServer`, the worker pool is `threading`, the MCP
server is a hand-written JSON-RPC 2.0 loop over stdin/stdout, and the UI is one
static HTML file. What that buys is a two-line install (`sh install.sh`, plus
`brew install yt-dlp`) and a tool that still runs in a year without a dependency
resolution problem — which matters for something you want to call from cron or
register as an agent tool and then forget about.

yt-dlp is deliberately the one exception, and it is called as a **subprocess,
not imported**. Instagram breaks extractors regularly; running it out of process
means `brew upgrade yt-dlp` picks up an extractor fix without touching this code
or pinning a Python dependency. The cost is that the integration surface is
stdout text rather than an API, which is why there is a progress-template parser
and an error classifier (below).

### Cookies are read at run time and never stored

igbulk never writes a credential anywhere. `Options` carries either a path to a
cookie file or the *name* of a browser; both are passed straight to yt-dlp as
`--cookies` / `--cookies-from-browser` at the moment of the call, and neither is
persisted, logged, or echoed in the JSON API (`Options.as_dict()` exposes the
cookie file path and a boolean `authenticated`, never cookie contents).

This is a security decision, not a convenience one. Storing a session cookie
means owning a credential store: file permissions, rotation, and a stolen-cookie
blast radius equal to full account access with no password and no second factor.
Delegating to a file the user manages themselves — or to the browser's own
store — means the tool has nothing worth stealing at rest. The related decision
is the MCP registration: putting `IGBULK_COOKIES` in the server registration
means the *model* never sees or handles the credential, only the process does.

`validate_cookie_file()` is the counterweight to that hands-off posture: it
checks the file exists, is tab-separated (a real Netscape jar rather than a JSON
export), and actually contains an `instagram` cookie — so a bad jar fails once,
up front, with an actionable message, instead of failing identically on all 100
links.

### Error classification, and why it is not a substring match

`engine.classify_error(text, authenticated)` maps raw yt-dlp output to
`(human message, is_retryable)` through an ordered list of `_Pattern` records.
Order matters and is load-bearing: the anonymous-block pattern is checked first
precisely *because* Instagram's block message itself mentions authentication and
rate limits, so a naive rate-limit check would match it and tell the user to
wait when waiting will never help.

Two subtler pieces:

**The `assumes_anonymous` flag.** Some messages' advice is "add cookies", which
is useless to a caller who already passed cookies. Those patterns are flagged,
and when `authenticated=True` the message is swapped for "Instagram rejected the
session — the cookies are probably expired." Same raw yt-dlp text, opposite
instruction, because the correct next action depends on state the classifier
knows and yt-dlp does not. Note that the "private account" pattern is *not*
flagged: being logged in does not help if you do not follow the account, so that
advice must survive authentication — and there is a test asserting exactly that.

**The cookie-read-failure pattern.** The word "cookies" appears in a large
fraction of Instagram's error text as generic advice. Matching on it would
report *"Couldn't read cookies from that browser"* for a session that is working
perfectly, sending the user off to quit Chrome and re-export a jar to fix a
problem that was actually a deleted post. So that pattern deliberately matches
only phrases describing an actual read failure — `could not copy`,
`failed to decrypt`, `keyring`, `cookies.sqlite`, `cookie database` — and the
test suite pins both halves of the distinction:

```python
def test_generic_cookie_advice_is_not_a_cookie_read_failure(self):
    raw = ("ERROR: [Instagram] nasa: Requested content is not available. "
           "Consider using --cookies-from-browser to pass cookies.")
    msg, _ = engine.classify_error(raw)
    self.assertNotIn("Couldn't read cookies", msg)
```

with the mirror-image test asserting that `could not copy Chrome cookie
database`, `Failed to decrypt with DPAPI` and `unable to open cookies.sqlite`
*do* produce that message. The failure being prevented is a diagnostic one: an
error message that sends the user to fix the wrong subsystem costs more time
than no error message at all.

Everything falls back to the last `ERROR:` line with the extractor prefix and
yt-dlp's "Confirm you are on the latest version" boilerplate stripped, truncated
to 300 characters — so a brand-new Instagram failure still surfaces something
readable instead of a stack trace.

The retryable flag drives behaviour, not just wording: rate-limit and network
errors are retried twice with exponential backoff plus random jitter (so a pool
of workers does not retry in lockstep), and everything else fails immediately
rather than making the user wait out three attempts on a deleted post.

### Block before you spend a request

Because the access investigation established that stories, highlights and
profile links cannot be served anonymously, `JobManager.create()` marks those
items `blocked` at creation time instead of issuing a request that will fail.
Profile links stay blocked even *with* cookies, because yt-dlp cannot enumerate
a profile reliably and attempting it only produces a confusing error. Blocked
counts as a terminal status, so a job of nothing but blocked links is
immediately complete rather than hanging. A failed request against a rate-limit
budget is not free; not spending it is the point.

### The job queue

`Job` is a list of `Item`s plus an `Options` and a `threading.Event` for
cancellation. `JobManager` holds all state in memory behind a single `RLock`
wrapped in a `Condition`, and every mutation calls `_touch()`, which increments
a monotonic version counter and notifies waiters.

That version counter is the whole concurrency design. `wait_for_change(since,
timeout)` blocks until the version passes `since`, which gives long-polling for
free: the web UI holds a request open with `?since=<version>&wait=25` and is
woken on the next change rather than polling on a timer, and the CLI and MCP
server use the exact same primitive to block until a job finishes
(`jobs.run_sync`). One mechanism, three consumers.

Work is executed by a small pool: `min(concurrency, len(queue))` daemon threads
share one list guarded by its own lock, each with its own `Engine`. Concurrency
is clamped to 1–8 at creation. Threads, rather than asyncio, because the actual
work is a blocking subprocess — there is nothing for an event loop to
interleave. Progress notifications are throttled to one every 0.25s per item so
a fast download cannot spin the pollers, and each yt-dlp process is supervised
by a `_Watchdog` thread that terminates it on either a wall-clock timeout or a
cancel, escalating `terminate()` to `kill()` after five seconds.

Retry-failed builds a *new* job from the failed and canceled links rather than
mutating the old one, which keeps the original job's history intact and means
"retry" is the same code path as "start".

### Why an MCP server

The CLI already emits `--json`, so an agent could shell out to it. An MCP server
buys two things a shell-out does not. First, the tools are self-describing: an
agent reading `tools/list` gets typed input schemas and descriptions that tell it
`instagram_parse_links` is free and offline and should be called first to show
the user what will be fetched — guidance that has nowhere to live in a
`--help` string. Second, and more importantly, credentials go in the *server
registration* (`--env IGBULK_COOKIES=…`), so every call is authenticated without
the model ever seeing, storing, or emitting the credential. A shell-out puts the
cookie path in the command the model writes.

The implementation is about 290 lines of hand-written JSON-RPC over stdin/stdout
with no MCP SDK — consistent with the zero-dependency rule, and small enough
that the protocol is not a black box. Downloads are capped at 100 links per
call, and `instagram_download` blocks until the job completes so the agent gets
file paths in the tool result rather than a job id it would have to poll.

### Front-end details worth noting

The HTTP server binds to `127.0.0.1` only *and* rejects cross-origin POSTs by
checking the `Host` and `Origin` headers — loopback binding alone does not stop
a page you have open from POSTing to your downloader. File previews are served
only from folders this server has actually downloaded into (a registered-root
whitelist), so a custom destination still previews while arbitrary filesystem
reads stay blocked.

Link parsing (`links.py`) is entirely pure string work, which is why it is the
most heavily tested module. It handles links wrapped in quotes, brackets or
markdown, comma-separated with no space, concatenated with no separator at all,
carrying `?igsh=` tracking parameters, and — because Instagram shortcodes are
globally unique — recognises `/p/CODE/` and `/reel/CODE/` as the same media so
the same post pasted twice downloads once.

---

## Measured results

There are no benchmarks. The project has no performance harness and no recorded
throughput, latency or success-rate numbers, and none should be inferred from
this README.

The one number that is reproducible is the test suite: **55 tests, 0.072s**, no
network and no subprocess (`python3 -m unittest discover -s tests`).

The codec section below quotes approximate timings and file sizes carried over
from the author's own runs. They are marked as such and are not measurements
this repository can reproduce. [CONFIRM: `~2s per Reel`, `11 MB → 21 MB`, and
`libx264 veryfast benchmarked faster than hardware h264_videotoolbox on Apple
silicon` are all from ad-hoc observation with no saved data.]

---

## Stack

- **Python 3** (type-annotated, `from __future__ import annotations`,
  dataclasses), standard library only — `http.server`, `threading`,
  `subprocess`, `argparse`, `json`, `re`, `pathlib`, `unittest`
- **yt-dlp** as an out-of-process extractor
- **ffmpeg / ffprobe** (optional) for codec detection and h264 re-encoding
- **HTML/CSS/JavaScript**, no framework and no build step — one static file
- **MCP** (Model Context Protocol) over stdio, JSON-RPC 2.0, hand-implemented
- macOS-specific integrations where present: `pbpaste` for `--clipboard`,
  `open` for `--open` and Finder reveal

---

## Install

```sh
cd ~/igbulk
sh install.sh          # symlinks ~/.local/bin/{igdl,igbulk-mcp}
brew install yt-dlp    # if you don't have it
```

If `~/.local/bin` is not on your `PATH`, add to `~/.zshrc`:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

---

## How to run

### Web UI

```sh
igdl --serve                       # opens http://localhost:8722
igdl --serve --port 9000 --no-browser
```

Paste any number of links into the box — one per line, comma-separated, or
buried in a chat log you copied wholesale. As you type it reports how many
distinct links it found and how many duplicates it dropped. Pick a folder,
concurrency, and cookie source, then start the job. While it runs you get a
per-link row with live percentage, speed and ETA; finished rows link to the
file. **Copy paths** puts every file path on the clipboard and **Retry failed**
re-runs only the ones that broke.

### CLI

```sh
igdl https://instagram.com/reel/ABC/ https://instagram.com/p/XYZ/ --cookies chrome
igdl links.txt --dest ~/Downloads/ig --jobs 2 --cookies chrome
pbpaste | igdl --cookies chrome            # whatever you just copied
igdl --clipboard --cookies safari
igdl --dry-run links.txt                   # show what it found, download nothing
igdl --resolve <url> --cookies chrome      # print direct CDN URLs, no download
```

With no arguments it reads stdin, so you can paste into the terminal and hit
Ctrl-D.

| Flag | Meaning |
| --- | --- |
| `-d, --dest DIR` | download folder (default `~/Downloads/instagram`, or `$IGBULK_DEST`) |
| `-j, --jobs N` | parallel downloads, 1–8 (default 3) |
| `-C, --cookie-file FILE` | authenticate with a Netscape `cookies.txt` (default `$IGBULK_COOKIES`) |
| `-c, --cookies BROWSER` | read login cookies from `chrome`, `safari`, `firefox`, `brave`, `edge`, … |
| `-a, --audio` | extract mp3 instead of keeping video |
| `--no-convert` | keep Instagram's original VP9 instead of re-encoding to h264 |
| `--flat` | don't create per-account subfolders |
| `--meta` | also write `.info.json` beside each file |
| `--no-skip` | re-download links already in the archive |
| `--resolve` | print direct media URLs instead of downloading |
| `--dry-run` | list the parsed links and exit |
| `--json` | one machine-readable object on stdout |
| `--open` | reveal the folder in Finder when finished |
| `--serve` / `--port` / `--no-browser` | run the web UI instead |

Exit code is `0` if nothing failed, `1` if any link failed, `2` if no links were
found in the input.

### JSON API

```sh
curl -s localhost:8722/api/parse   -d '{"text":"<pasted blob>"}'
curl -s localhost:8722/api/resolve -d '{"urls":["<url>"],"cookies_from":"chrome"}'
curl -s localhost:8722/api/jobs    -d '{"text":"<urls>","concurrency":3,"cookies_from":"chrome"}'
curl -s localhost:8722/api/jobs/<job_id>
curl -s "localhost:8722/api/jobs/<job_id>?wait_complete=600"          # block until finished
curl -s "localhost:8722/api/jobs/<job_id>?since=<version>&wait=25"    # long-poll for changes
curl -s -X POST localhost:8722/api/jobs/<job_id>/cancel
curl -s -X POST localhost:8722/api/jobs/<job_id>/retry
curl -s localhost:8722/api/health
```

`POST /api/jobs` body fields: `text` or `urls`, `dest`, `concurrency`,
`cookies_from`, `cookie_file`, `audio_only`, `flat`, `skip_existing`,
`write_metadata`, `ensure_h264`.

Job payloads carry `total`, `settled`, `counts`, `bytes`, `files`, `version`,
and an `items[]` array where each item has `status` (`queued`, `running`,
`waiting`, `done`, `skipped`, `failed`, `canceled`, `blocked`), `percent`,
`speed`, `eta`, `message`, `uploader`, and `files[]`. Add `?logs=1` for raw
yt-dlp output.

### MCP server

```sh
claude mcp add igbulk --env IGBULK_COOKIES_FROM=chrome -- ~/.local/bin/igbulk-mcp
```

Use the `igbulk-mcp` launcher, **not** `python3 -m igbulk.mcp` — agents start
MCP servers from an arbitrary working directory, and the bare module invocation
fails with `ModuleNotFoundError` anywhere outside the project root. The launcher
resolves its own path (symlink included) and sets `PYTHONPATH` itself.

| Tool | Does |
| --- | --- |
| `instagram_parse_links` | extract/de-dupe links from text — offline and free |
| `instagram_resolve` | metadata + direct CDN URLs, no download |
| `instagram_download` | download to disk, returns file paths; blocks until done |

`instagram_download` takes `urls`, `dest`, `concurrency`, `audio_only`, `flat`,
`skip_existing`, `ensure_h264`, `cookies_from`, `cookie_file`, and caps at 100
links per call.

### Tests

```sh
python3 -m unittest discover -s tests -v
```

---

## Codecs — why files get re-encoded

Instagram serves its **1080x1920** Reels as **VP9 only**; the h264 rendition
caps at **720x1280**. QuickTime, Final Cut and Premiere cannot decode VP9, which
shows up as *"file isn't compatible with this media player"* — often with
working audio and no picture, since the AAC track is fine. [CONFIRM: the
resolution/codec pairing is from the author's observation of Instagram's
formats, which can change.]

So by default any non-h264 video is re-encoded after download
(`libx264 -crf 20 -preset veryfast`, plus `yuv420p` because some players reject
10-bit, and `+faststart` so the file plays before it has fully loaded). You keep
the 1080p instead of dropping to 720p, and the result plays everywhere. Files
grow. `ensure_playable()` is written so a failed conversion leaves the original
file untouched — an unplayable download still beats no download — and a missing
ffmpeg is reported as a note on a successful item rather than as a failure.

`--no-convert` keeps Instagram's original file.

### Files on disk

```
~/Downloads/instagram/
  nasa/
    DSIjtEfiYK9.mp4
    DbYmqpplO_N.mp4
  .igbulk-archive.txt      # what's been fetched, so re-runs skip
```

Re-running a link already in the archive marks it *Already saved* rather than
downloading again. `--no-skip` overrides that; deleting the archive file resets
it.

---

## What it accepts

Reels, posts, IGTV, carousels (every image/video in the post), and `/share/`
redirect links. Tracking junk (`?igsh=…`), wrapping quotes, brackets and commas
are stripped; the same post pasted twice is downloaded once.

Stories, highlights and whole-profile links are flagged up front rather than
attempted — see "Block before you spend a request".

---

## Errors you'll actually see

| Message | Fix |
| --- | --- |
| *Instagram refused the request without a login* | set up cookies — see above |
| *Instagram rejected the session — cookies are probably expired* | log in again, re-export the cookie file |
| *Instagram rate-limited this IP* | wait a few minutes, drop `--jobs` to 1 |
| *Private account* | needs cookies from an account that follows them |
| *Post not found* | deleted, or region-blocked |
| *Couldn't read cookies from that browser* | quit the browser, or switch to a cookie file |
| *… doesn't look like a Netscape cookies.txt file* | re-export with a cookies.txt extension, not a JSON export |
| *ffmpeg not found — install it to fix VP9 playback* | `brew install ffmpeg`; the file downloaded but stayed VP9 |

---

## Layout

```
igbulk/
  links.py     URL extraction, canonicalisation, de-duplication (pure, unit-tested)
  engine.py    yt-dlp driver: resolve, download, progress parsing, error classification
  jobs.py      worker pool, live job state, cancel/retry
  server.py    stdlib HTTP server: web UI + JSON API
  ui.html      the paste-box front end
  cli.py       the igdl command
  mcp.py       MCP stdio server
tests/         55 unit tests: python3 -m unittest discover -s tests
```

---

## Known limitations, and what I'd do next

**It requires a logged-in session.** This is not a workaround waiting to be
found — the access investigation above is the evidence that no anonymous route
exists. Anyone using igbulk must supply their own Instagram cookies, which means
the tool is only as available as their account is. If Instagram invalidates the
session, every link fails until the jar is re-exported.

**Using your own session carries account risk.** Automated requests against a
logged-in account are exactly the pattern rate-limiting and account-flagging are
built to catch. The default concurrency of 3 and the backoff-with-jitter retry
are hedges against that, not guarantees. [CONFIRM: no account has been tested to
the point of a restriction, so the safe rate is unknown.]

**It depends on yt-dlp keeping pace with Instagram.** Every extraction path runs
through yt-dlp. When Instagram changes something, igbulk is broken until yt-dlp
ships a fix; the subprocess design means the fix arrives with `brew upgrade`
rather than a code change here, but it does not remove the dependency. There is
no fallback extractor and no pinned version, so a yt-dlp regression is also
inherited immediately.

**Downloading content you do not own has terms-of-service and copyright
implications.** Instagram's terms restrict automated collection, and the posts
themselves are the uploader's copyrighted work. This tool is built for saving
your own content, content you have rights to, and personal-use copies — it
deliberately does not implement the things that would make bulk scraping of
other people's accounts practical (no proxy rotation, no service-account pool,
no profile enumeration). That is a design position, not an oversight. Anyone
using it is responsible for staying inside those limits.

**Other gaps:**

- No integration tests against a real yt-dlp. Everything network-touching is
  tested only through pure functions; a change in yt-dlp's stdout format
  (the `--print-to-file` template, the progress template) would break parsing
  with no test to catch it. A recorded-fixture suite over captured yt-dlp
  output is the obvious next step.
- Job state is in memory only. Restarting the server loses history, and the
  manager keeps the last 40 jobs. Nothing resumes a partially-finished job.
- macOS-shaped in places: `pbpaste`, `open` and the Keychain notes assume macOS,
  though the engine, server and MCP layers are platform-neutral. [CONFIRM: not
  tested on Linux or Windows.]
- No authentication on the local HTTP API. It relies on loopback binding plus an
  origin check, which is appropriate for a single-user local tool and would not
  be if it were ever exposed.
- The error classifier is a regex ladder over a third party's prose. It degrades
  gracefully (unknown errors fall through to the last `ERROR:` line) but it will
  drift as yt-dlp's and Instagram's wording changes, and there is no mechanism
  that notices the drift.
