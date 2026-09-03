"""yt-dlp driver: resolve direct media URLs and download files.

yt-dlp runs as a subprocess rather than an import so the tool picks up
`brew upgrade yt-dlp` extractor fixes without a pinned Python dependency —
Instagram breaks extractors often enough that this matters.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PROGRESS_SENTINEL = "@IGP@"

# yt-dlp writes one of these lines per finished file when we ask it to.
_PRINT_TEMPLATE = "after_move:@IGF@%(id)s|%(channel,uploader,uploader_id,playlist_uploader)s|%(filepath)s"
_PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_SENTINEL}%(progress._percent_str)s|%(progress._speed_str)s"
    "|%(progress._eta_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes,progress.total_bytes_estimate)s"
)

_ALREADY_RE = re.compile(r"^\[download\] (?P<path>.+) has already been downloaded")
_ARCHIVE_RE = re.compile(r"has already been recorded in the archive")
_DEST_RE = re.compile(r"^\[download\] Destination: (?P<path>.+)$")


class EngineError(RuntimeError):
    pass


def ytdlp_path() -> str:
    exe = os.environ.get("IGBULK_YTDLP") or shutil.which("yt-dlp")
    if not exe:
        raise EngineError(
            "yt-dlp not found. Install it with `brew install yt-dlp` "
            "(or set IGBULK_YTDLP to its path)."
        )
    return exe


def ytdlp_version() -> str | None:
    try:
        out = subprocess.run(
            [ytdlp_path(), "--version"], capture_output=True, text=True, timeout=20
        )
        return out.stdout.strip() or None
    except (EngineError, OSError, subprocess.SubprocessError):
        return None


@dataclass
class Options:
    dest: Path = field(default_factory=lambda: Path.home() / "Downloads" / "instagram")
    audio_only: bool = False
    cookies_from: str | None = None      # chrome | safari | firefox | brave | edge …
    cookie_file: Path | None = None      # Netscape cookies.txt — the automatable path
    write_metadata: bool = False         # keep the .info.json next to each file
    skip_existing: bool = True           # honour the download archive
    flat: bool = False                   # all files in dest, no per-account folders
    ensure_h264: bool = True             # convert VP9/AV1 so QuickTime can play it
    retries: int = 2                     # our own retries on rate-limit errors
    timeout: int = 900                   # per-link wall clock, seconds

    @property
    def authenticated(self) -> bool:
        return bool(self.cookie_file or self.cookies_from)

    def as_dict(self) -> dict:
        return {
            "dest": str(self.dest),
            "audio_only": self.audio_only,
            "cookies_from": self.cookies_from,
            "cookie_file": str(self.cookie_file) if self.cookie_file else None,
            "authenticated": self.authenticated,
            "write_metadata": self.write_metadata,
            "skip_existing": self.skip_existing,
            "flat": self.flat,
            "ensure_h264": self.ensure_h264,
        }


# Environment defaults, so an agent, cron job or MCP registration can be
# authenticated once instead of passing credentials on every call.
ENV_COOKIE_FILE = "IGBULK_COOKIES"
ENV_COOKIES_FROM = "IGBULK_COOKIES_FROM"


def env_auth() -> tuple[Path | None, str | None]:
    """Read the default cookie source from the environment."""
    raw = os.environ.get(ENV_COOKIE_FILE)
    cookie_file = Path(raw).expanduser() if raw else None
    return cookie_file, (os.environ.get(ENV_COOKIES_FROM) or None)


def validate_cookie_file(path: Path) -> Path:
    """Fail early and clearly rather than letting yt-dlp fail per link."""
    path = Path(path).expanduser()
    if not path.exists():
        raise EngineError(f"Cookie file not found: {path}")
    if not path.is_file():
        raise EngineError(f"Cookie file is not a file: {path}")
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        raise EngineError(f"Cannot read cookie file: {exc}") from exc
    if "\t" not in head:
        raise EngineError(
            f"{path} doesn't look like a Netscape cookies.txt file "
            "(tab-separated). Export it with a browser extension such as "
            "“Get cookies.txt LOCALLY”."
        )
    if "instagram" not in head.lower():
        raise EngineError(
            f"{path} has no instagram.com cookies in it. Export the cookie file "
            "while on instagram.com, logged in."
        )
    return path


# --- error classification -----------------------------------------------------
# Instagram's failure messages are noisy; collapse them into something a human
# can act on. Order matters: first match wins. The last field marks messages
# whose advice is "add cookies" — useless to a caller who already passed some,
# so those get replaced by _AUTH_FAILED instead.
@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern
    message: str
    retryable: bool = False
    assumes_anonymous: bool = False


_ERROR_PATTERNS: list[_Pattern] = [
    # Instagram's current anonymous-block response. Checked first because its
    # text also mentions authentication and rate limits.
    _Pattern(re.compile(r"empty media response", re.I),
             "Instagram refused the request without a login. Add cookies "
             "(--cookie-file FILE or --cookies chrome) — anonymous downloads no longer work.",
             assumes_anonymous=True),
    _Pattern(re.compile(r"rate.?limit|429|too many requests", re.I),
             "Instagram rate-limited this IP. Wait a few minutes or lower concurrency.",
             retryable=True),
    _Pattern(re.compile(r"login required|requires? login|you need to log in|authentication|not logged in", re.I),
             "Login required — this post isn't public. Add cookies to fetch it.",
             assumes_anonymous=True),
    _Pattern(re.compile(r"private|only accessible to|restricted", re.I),
             "Private account — needs cookies from an account that follows it."),
    _Pattern(re.compile(r"unavailable|removed|deleted|404|not found|no longer exists", re.I),
             "Post not found — deleted, renamed, or region-blocked."),
    _Pattern(re.compile(r"age.?restricted|age confirmation", re.I),
             "Age-restricted — needs cookies from a logged-in account.",
             assumes_anonymous=True),
    _Pattern(re.compile(r"unsupported url|no video|nothing to download", re.I),
             "Nothing downloadable found at this URL."),
    _Pattern(re.compile(r"timed out|timeout|connection|network|resolve host|ssl", re.I),
             "Network problem reaching Instagram.", retryable=True),
    # Must describe an actual cookie *read* failure. A bare mention of the word
    # "cookies" appears in half of Instagram's errors as generic advice, and
    # matching that misreports working sessions as broken ones.
    _Pattern(re.compile(r"could not copy|failed to decrypt|unable to decrypt|keyring"
                        r"|cookie database|cookies\.sqlite|cookie file", re.I),
             "Couldn't read cookies from that browser. Quit it and retry, or "
             "export a cookies.txt file instead."),
]

_AUTH_FAILED = (
    "Instagram rejected the session — the cookies are probably expired. Log in "
    "again in the browser, then re-export the cookie file.", False,
)


def classify_error(text: str, authenticated: bool = False) -> tuple[str, bool]:
    """Map raw yt-dlp output to (human message, is_retryable)."""
    blob = text or ""
    for entry in _ERROR_PATTERNS:
        if entry.regex.search(blob):
            if authenticated and entry.assumes_anonymous:
                return _AUTH_FAILED
            return entry.message, entry.retryable
    # Fall back to the last ERROR: line, trimmed.
    errors = [l for l in blob.splitlines() if l.startswith("ERROR:")]
    if errors:
        msg = errors[-1][len("ERROR:"):].strip()
        msg = re.sub(r"^\[[^\]]+\]\s*", "", msg)
        msg = re.sub(r"\s*;\s*Confirm you are on the latest version.*$", "", msg, flags=re.I)
        return (msg[:300] or "Download failed."), False
    return "Download failed — see the yt-dlp log for details.", False


# --- codec compatibility ------------------------------------------------------
# Instagram serves its highest resolution (1080x1920) as VP9 only; the h264
# rendition caps at 720x1280. QuickTime, Final Cut and Premiere can't decode
# VP9, which shows up as "file isn't compatible" — or audio with no picture.
# Rather than settle for 720p we keep the 1080p and re-encode, which costs about
# two seconds per Reel.
PLAYABLE_CODECS = {"h264", "avc1", "hevc", "h265"}
_CRF = "20"          # visually near-transparent; artefacts survive re-editing
_PRESET = "veryfast" # benchmarked faster than h264_videotoolbox on Apple silicon


def _tool(name: str) -> str | None:
    return shutil.which(name)


def video_codec(path: str) -> str | None:
    """Return the video stream's codec name, or None if there isn't one."""
    ffprobe = _tool("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip().split(",")[0] or None) if out.returncode == 0 else None


def convert_cmd(ffmpeg: str, src: str, dst: str) -> list[str]:
    """Build the re-encode command. Split out so it can be unit-tested."""
    return [
        ffmpeg, "-v", "error", "-y", "-i", src,
        "-c:v", "libx264", "-crf", _CRF, "-preset", _PRESET,
        "-pix_fmt", "yuv420p",          # some players reject 10-bit/4:2:2
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",      # playable before the whole file loads
        dst,
    ]


def ensure_playable(path: str, on_progress=None) -> tuple[bool, str | None]:
    """Re-encode `path` to h264 in place if its codec isn't widely playable.

    Returns (converted, error). A failure leaves the original file untouched —
    an unplayable download still beats no download.
    """
    codec = video_codec(path)
    if codec is None or codec.lower() in PLAYABLE_CODECS:
        return False, None
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found — install it with `brew install ffmpeg` to fix VP9 playback"
    if on_progress:
        on_progress({"phase": "converting",
                     "message": f"Converting {codec} → h264",
                     "file": os.path.basename(path)})
    stem, ext = os.path.splitext(path)
    tmp = f"{stem}.h264{ext or '.mp4'}"
    try:
        proc = subprocess.run(convert_cmd(ffmpeg, path, tmp),
                              capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        _unlink(tmp)
        return False, f"Conversion failed: {exc}"
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _unlink(tmp)
        detail = (proc.stderr or "").strip().splitlines()
        return False, f"Conversion failed: {detail[-1] if detail else 'ffmpeg error'}"
    try:
        os.replace(tmp, path)      # atomic; keeps the original filename
    except OSError as exc:
        _unlink(tmp)
        return False, f"Could not replace original: {exc}"
    return True, None


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@dataclass
class MediaFile:
    path: str
    size: int = 0
    kind: str = "other"        # video | image | audio | other

    def as_dict(self) -> dict:
        return {"path": self.path, "name": os.path.basename(self.path),
                "size": self.size, "kind": self.kind}


def _kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if ext in {"mp4", "mov", "mkv", "webm"}:
        return "video"
    if ext in {"jpg", "jpeg", "png", "webp", "heic", "avif"}:
        return "image"
    if ext in {"mp3", "m4a", "aac", "opus", "wav", "flac"}:
        return "audio"
    return "other"


class Engine:
    """Runs yt-dlp for one link at a time."""

    def __init__(self, opts: Options):
        self.opts = opts
        self.exe = ytdlp_path()

    # -- shared argument construction -----------------------------------------
    def _base_args(self) -> list[str]:
        args = [self.exe, "--no-color", "--no-warnings", "--ignore-config"]
        # A cookie file wins over a browser: it needs no Keychain prompt and no
        # Full Disk Access, so it's the one that works unattended.
        if self.opts.cookie_file:
            args += ["--cookies", str(self.opts.cookie_file)]
        elif self.opts.cookies_from:
            args += ["--cookies-from-browser", self.opts.cookies_from]
        return args

    def _output_template(self) -> str:
        dest = str(self.opts.dest)
        if self.opts.flat:
            return os.path.join(dest, "%(id)s.%(ext)s")
        # Prefer the @username (channel) over the numeric uploader_id.
        return os.path.join(dest, "%(channel,uploader,uploader_id)s", "%(id)s.%(ext)s")

    # -- resolve (snapinsta-style: metadata + direct CDN URLs, no download) ----
    def resolve(self, url: str) -> dict:
        args = self._base_args() + ["-J", "--no-playlist-reverse", url]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=self.opts.timeout)
        if proc.returncode != 0 or not proc.stdout.strip():
            message, retryable = classify_error(proc.stderr or proc.stdout, self.opts.authenticated)
            raise EngineError(message) from None
        try:
            info = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise EngineError(f"Could not parse yt-dlp metadata: {exc}") from exc
        return _summarise(info)

    # -- download -------------------------------------------------------------
    def download(self, url: str, on_progress=None, cancel: threading.Event | None = None) -> dict:
        """Download every file behind `url`.

        Returns {"files": [MediaFile…], "skipped": bool, "uploader": str|None,
                 "log": str}. Raises EngineError with a human message on failure.
        """
        self.opts.dest.mkdir(parents=True, exist_ok=True)
        attempt = 0
        last_error: EngineError | None = None

        while attempt <= self.opts.retries:
            if cancel is not None and cancel.is_set():
                raise EngineError("Canceled.")
            if attempt:
                # Back off before retrying a rate-limit; jitter so a batch of
                # workers doesn't retry in lockstep.
                delay = min(60, 6 * (3 ** (attempt - 1))) + random.uniform(0, 3)
                if on_progress:
                    on_progress({"phase": "waiting", "message": f"Rate-limited — retrying in {delay:.0f}s"})
                if _sleep_unless_canceled(delay, cancel):
                    raise EngineError("Canceled.")
            try:
                return self._run_download(url, on_progress, cancel)
            except EngineError as exc:
                last_error = exc
                if not getattr(exc, "retryable", False):
                    raise
                attempt += 1

        raise last_error or EngineError("Download failed.")

    def _run_download(self, url, on_progress, cancel) -> dict:
        fd, printed = tempfile.mkstemp(prefix="igbulk-", suffix=".txt")
        os.close(fd)
        args = self._base_args() + [
            "--newline",
            "--no-simulate",
            "--no-overwrites",
            "--retries", "3",
            "--fragment-retries", "5",
            "--concurrent-fragments", "4",
            "--progress-delta", "0.4",
            "--progress-template", _PROGRESS_TEMPLATE,
            "--print-to-file", _PRINT_TEMPLATE, printed,
            "-o", self._output_template(),
        ]
        if self.opts.skip_existing:
            args += ["--download-archive", str(self.opts.dest / ".igbulk-archive.txt")]
        if self.opts.write_metadata:
            args += ["--write-info-json"]
        if self.opts.audio_only:
            args += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        args.append(url)

        log_lines: list[str] = []
        already = False
        recovered: list[str] = []

        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        watchdog = _Watchdog(proc, self.opts.timeout, cancel)
        watchdog.start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                if not line:
                    continue
                if PROGRESS_SENTINEL in line:
                    if on_progress:
                        payload = _parse_progress(line)
                        if payload:
                            on_progress(payload)
                    continue
                log_lines.append(line)
                if len(log_lines) > 400:
                    del log_lines[:200]
                m = _ALREADY_RE.match(line)
                if m:
                    already = True
                    recovered.append(m.group("path"))
                    continue
                if _ARCHIVE_RE.search(line):
                    already = True
                    continue
                m = _DEST_RE.match(line)
                if m and on_progress:
                    on_progress({"phase": "downloading",
                                 "file": os.path.basename(m.group("path"))})
        finally:
            watchdog.stop()
            proc.wait()

        log = "\n".join(log_lines)
        files = _read_printed(printed)
        try:
            os.unlink(printed)
        except OSError:
            pass

        if cancel is not None and cancel.is_set():
            raise EngineError("Canceled.")
        if watchdog.timed_out:
            raise EngineError(f"Timed out after {self.opts.timeout}s.")

        uploader = files[0][1] if files else None
        media = [
            MediaFile(path=p, size=_size_of(p), kind=_kind_of(p))
            for _, _, p in files
            if os.path.exists(p)
        ]

        if not media and (already or recovered):
            # Nothing new, but the file is on disk from a previous run.
            existing = [
                MediaFile(path=p, size=_size_of(p), kind=_kind_of(p))
                for p in recovered if os.path.exists(p)
            ]
            return {"files": [f.as_dict() for f in existing], "skipped": True,
                    "uploader": uploader, "log": log}

        if proc.returncode != 0 and not media:
            message, retryable = classify_error(log, self.opts.authenticated)
            err = EngineError(message)
            err.retryable = retryable  # type: ignore[attr-defined]
            err.log = log              # type: ignore[attr-defined]
            raise err

        if not media:
            message, retryable = classify_error(log, self.opts.authenticated)
            err = EngineError(message)
            err.retryable = retryable  # type: ignore[attr-defined]
            err.log = log              # type: ignore[attr-defined]
            raise err

        notes: list[str] = []
        if self.opts.ensure_h264 and not self.opts.audio_only:
            for f in media:
                if f.kind != "video":
                    continue
                converted, error = ensure_playable(f.path, on_progress)
                if converted:
                    f.size = _size_of(f.path)   # re-encoding changes the size
                elif error and error not in notes:
                    notes.append(error)

        return {"files": [f.as_dict() for f in media], "skipped": False,
                "uploader": uploader, "log": log, "notes": notes}


class _Watchdog(threading.Thread):
    """Kills a yt-dlp process on timeout or cancel."""

    def __init__(self, proc, timeout, cancel):
        super().__init__(daemon=True)
        self.proc, self.timeout, self.cancel = proc, timeout, cancel
        self.timed_out = False
        self._done = threading.Event()

    def run(self):
        deadline = time.monotonic() + self.timeout
        while not self._done.wait(0.5):
            if self.proc.poll() is not None:
                return
            if self.cancel is not None and self.cancel.is_set():
                self._kill()
                return
            if time.monotonic() > deadline:
                self.timed_out = True
                self._kill()
                return

    def _kill(self):
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except OSError:
            pass

    def stop(self):
        self._done.set()


def _sleep_unless_canceled(seconds: float, cancel) -> bool:
    """Sleep, returning True if canceled partway through."""
    if cancel is None:
        time.sleep(seconds)
        return False
    return cancel.wait(seconds)


def _size_of(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _read_printed(path: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("@IGF@"):
                    continue
                parts = line[len("@IGF@"):].split("|", 2)
                if len(parts) == 3:
                    vid, uploader, filepath = parts
                    out.append((vid, "" if uploader == "NA" else uploader, filepath))
    except OSError:
        pass
    return out


def _parse_progress(line: str) -> dict | None:
    _, _, payload = line.partition(PROGRESS_SENTINEL)
    parts = payload.split("|")
    if len(parts) < 5:
        return None
    percent, speed, eta, done, total = (p.strip() for p in parts[:5])
    try:
        pct = float(percent.rstrip("%"))
    except ValueError:
        pct = None
    return {
        "phase": "downloading",
        "percent": pct,
        "speed": None if speed in ("NA", "Unknown", "") else speed,
        "eta": None if eta in ("NA", "Unknown", "") else eta,
        "downloaded_bytes": _int_or_none(done),
        "total_bytes": _int_or_none(total),
    }


def _int_or_none(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# --- metadata summarising for /resolve ---------------------------------------
def _summarise(info: dict) -> dict:
    entries = info.get("entries")
    if entries:
        media = [_summarise_one(e) for e in entries if isinstance(e, dict)]
    else:
        media = [_summarise_one(info)]
    first = info if not entries else (entries[0] if entries else {})
    return {
        "id": info.get("id"),
        "title": (info.get("title") or "").strip() or None,
        "description": info.get("description"),
        "uploader": info.get("channel") or info.get("uploader") or first.get("channel"),
        "timestamp": info.get("timestamp") or first.get("timestamp"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "is_carousel": bool(entries) and len(entries) > 1,
        "media": media,
    }


def _summarise_one(info: dict) -> dict:
    url = info.get("url")
    ext = info.get("ext")
    if not url:
        # Pick the best progressive format we can hand straight to a browser.
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        best = None
        for fmt in formats:
            if fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") not in (None, "none"):
                if best is None or (fmt.get("height") or 0) > (best.get("height") or 0):
                    best = fmt
        if best is None and formats:
            best = formats[-1]
        if best:
            url, ext = best.get("url"), best.get("ext")
    return {
        "id": info.get("id"),
        "kind": "image" if (info.get("vcodec") == "none" and info.get("acodec") == "none")
                or (ext or "") in ("jpg", "jpeg", "png", "webp") else "video",
        "ext": ext,
        "width": info.get("width"),
        "height": info.get("height"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "url": url,
    }
