"""Local web UI + JSON API.

Stdlib `http.server` on purpose: no pip install, nothing to keep in sync. Binds
to loopback only, and rejects cross-origin POSTs so a random web page you have
open can't drive your downloader (DNS-rebinding style abuse).
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import links as linkmod
from .engine import Engine, EngineError, Options, validate_cookie_file, ytdlp_version
from .jobs import JobManager, ensure_dest

HERE = Path(__file__).resolve().parent
UI_FILE = HERE / "ui.html"
MAX_BODY = 4 * 1024 * 1024   # a paste of ~40k links is plenty

AGENT_HINT = """\
# Authenticate once (Instagram refuses anonymous requests)
export IGBULK_COOKIES=~/.igbulk-cookies.txt   # Netscape cookies.txt — no prompts, works unattended
export IGBULK_COOKIES_FROM=firefox            # or read from a browser instead
# Per-request overrides: "cookie_file" or "cookies_from" in any POST body.

# Drive it over HTTP (all POST bodies are JSON)

curl -s localhost:{port}/api/parse   -d '{{"text":"<pasted blob>"}}'
curl -s localhost:{port}/api/resolve -d '{{"urls":["<url>"]}}'      # direct CDN URLs, no download
curl -s localhost:{port}/api/jobs    -d '{{"text":"<urls>","concurrency":3}}'
curl -s localhost:{port}/api/jobs/<job_id>            # poll; add ?wait=25&since=<version> to long-poll
curl -s localhost:{port}/api/jobs/<job_id>?wait_complete=600   # block until finished
curl -s -X POST localhost:{port}/api/jobs/<job_id>/cancel
curl -s -X POST localhost:{port}/api/jobs/<job_id>/retry       # re-run only the failures

# Or skip the server
igdl <url> <url> ... --json
igdl --resolve <url> --json
igdl links.txt --dest ~/Downloads/ig --jobs 3

# Or as an MCP server (tools: instagram_download, instagram_resolve, instagram_parse_links)
claude mcp add igbulk -- {python} -m igbulk.mcp
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "igbulk"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console for our own output
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    @property
    def manager(self) -> JobManager:
        return self.server.manager  # type: ignore[attr-defined]

    @property
    def defaults(self) -> Options:
        return self.server.defaults  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code: int = 200):
        self._send(code, json.dumps(payload, default=str).encode(),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message: str):
        self._json({"error": message}, code)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("Request body too large.")
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "application/x-www-form-urlencoded":
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Body must be a JSON object.")
        return data

    def _origin_ok(self) -> bool:
        """Reject cross-site POSTs; loopback binding alone isn't enough."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "[::1]", "::1", ""):
            return False
        origin = self.headers.get("Origin")
        if origin:
            netloc = urllib.parse.urlparse(origin).hostname
            if netloc not in ("localhost", "127.0.0.1", "::1"):
                return False
        return True

    # -- routing --------------------------------------------------------------
    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = urllib.parse.parse_qs(query)
        try:
            if path == "/":
                return self._serve_ui()
            if path == "/api/health":
                return self._json(self._health())
            if path == "/api/jobs":
                return self._json({"jobs": [self._job_payload(j, brief=True)
                                            for j in self.manager.list()]})
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})", path)
            if m:
                return self._get_job(m.group(1), q)
            if path.startswith("/files/"):
                return self._serve_file(urllib.parse.unquote(path[len("/files/"):]))
            return self._error(404, "Not found.")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self._error(500, f"{type(exc).__name__}: {exc}")

    do_HEAD = do_GET

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        if not self._origin_ok():
            return self._error(403, "Cross-origin requests are not allowed.")
        try:
            body = self._body()
        except ValueError as exc:
            return self._error(400, str(exc))
        try:
            if path == "/api/parse":
                return self._json(linkmod.parse(self._text_of(body)).as_dict())
            if path == "/api/jobs":
                return self._create_job(body)
            if path == "/api/resolve":
                return self._resolve(body)
            if path == "/api/reveal":
                return self._reveal(body)
            m = re.fullmatch(r"/api/jobs/([0-9a-f]{6,32})/(cancel|retry)", path)
            if m:
                return self._job_action(m.group(1), m.group(2), body)
            return self._error(404, "Not found.")
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- handlers -------------------------------------------------------------
    def _serve_ui(self):
        try:
            body = UI_FILE.read_bytes()
        except OSError:
            return self._error(500, "UI file missing.")
        self._send(200, body, "text/html; charset=utf-8")

    def _health(self) -> dict:
        return {
            "ok": True,
            "ytdlp_version": ytdlp_version(),
            "dest": str(self.defaults.dest),
            "authenticated": self.defaults.authenticated,
            "cookie_file": str(self.defaults.cookie_file) if self.defaults.cookie_file else None,
            "cookies_from": self.defaults.cookies_from,
            "cookies_recommended": not self.defaults.authenticated,
            "cookies_notice": (
                "Instagram now blocks downloads that aren't tied to a logged-in "
                "session, so pick your browser under “Cookies from” before starting."
            ),
            "port": self.server.server_address[1],
            "agent_hint": AGENT_HINT.format(
                port=self.server.server_address[1],
                python=self.server.python_exe,  # type: ignore[attr-defined]
            ),
        }

    @staticmethod
    def _text_of(body: dict) -> str:
        if body.get("urls"):
            urls = body["urls"]
            if isinstance(urls, str):
                return urls
            return "\n".join(str(u) for u in urls)
        return str(body.get("text") or "")

    def _options_from(self, body: dict) -> Options:
        base = self.defaults
        dest = body.get("dest") or base.dest
        try:
            dest_path = ensure_dest(dest)
        except OSError as exc:
            raise ValueError(f"Cannot use that folder: {exc}") from exc
        cookie_file = body.get("cookie_file")
        if cookie_file:
            try:
                cookie_file = validate_cookie_file(Path(str(cookie_file)))
            except EngineError as exc:
                raise ValueError(str(exc)) from exc
        else:
            cookie_file = base.cookie_file
        return Options(
            dest=dest_path,
            audio_only=bool(body.get("audio_only", base.audio_only)),
            cookies_from=(body.get("cookies_from") or
                          (None if cookie_file else base.cookies_from)),
            cookie_file=cookie_file,
            write_metadata=bool(body.get("write_metadata", base.write_metadata)),
            skip_existing=bool(body.get("skip_existing", base.skip_existing)),
            flat=bool(body.get("flat", base.flat)),
            ensure_h264=bool(body.get("ensure_h264", base.ensure_h264)),
        )

    def _create_job(self, body: dict):
        parsed = linkmod.parse(self._text_of(body))
        if not parsed.links:
            return self._error(400, "No Instagram links found in that text.")
        try:
            opts = self._options_from(body)
        except ValueError as exc:
            return self._error(400, str(exc))
        job = self.manager.create(parsed, opts,
                                  concurrency=int(body.get("concurrency") or 3))
        self._json(self._job_payload(job), 201)

    def _get_job(self, job_id: str, q: dict):
        job = self.manager.get(job_id)
        if job is None:
            return self._error(404, "Unknown job id.")
        wait_complete = _first_float(q.get("wait_complete"))
        if wait_complete:
            deadline_version = self.manager.version
            import time as _t
            end = _t.monotonic() + min(wait_complete, 3600)
            while not (job.is_complete and job.finished) and _t.monotonic() < end:
                deadline_version = self.manager.wait_for_change(
                    deadline_version, timeout=min(5.0, max(0.1, end - _t.monotonic())))
        else:
            wait = _first_float(q.get("wait"))
            since = int(_first_float(q.get("since")) or 0)
            if wait and not (job.is_complete and job.finished):
                self.manager.wait_for_change(since, timeout=min(wait, 60))
        self._json(self._job_payload(job, include_logs=_first_float(q.get("logs")) == 1))

    def _job_action(self, job_id: str, action: str, body: dict):
        if action == "cancel":
            if not self.manager.cancel(job_id):
                return self._error(404, "Unknown job id.")
            job = self.manager.get(job_id)
            return self._json(self._job_payload(job))
        new_job = self.manager.retry_failed(job_id, body.get("concurrency"))
        if new_job is None:
            return self._error(400, "Nothing to retry.")
        self._json(self._job_payload(new_job), 201)

    def _resolve(self, body: dict):
        parsed = linkmod.parse(self._text_of(body))
        if not parsed.links:
            return self._error(400, "No Instagram links found in that text.")
        try:
            opts = self._options_from(body)
        except ValueError as exc:
            return self._error(400, str(exc))
        engine = Engine(opts)
        out = []
        for link in parsed.links:
            entry = {"url": link.url, "shortcode": link.shortcode, "kind": link.kind}
            try:
                entry["ok"] = True
                entry.update(engine.resolve(link.url))
            except EngineError as exc:
                entry["ok"] = False
                entry["error"] = str(exc)
            out.append(entry)
        self._json({"results": out, "count": len(out)})

    def _reveal(self, body: dict):
        target = Path(str(body.get("path") or self.defaults.dest)).expanduser()
        try:
            resolved = target.resolve()
        except OSError as exc:
            return self._error(400, str(exc))
        if not resolved.exists():
            return self._error(404, "That folder doesn't exist yet.")
        cmd = ["open", "-R", str(resolved)] if resolved.is_file() else ["open", str(resolved)]
        try:
            subprocess.Popen(cmd)
        except OSError as exc:
            return self._error(500, f"Could not open Finder: {exc}")
        self._json({"ok": True, "opened": str(resolved)})

    def _serve_file(self, rel: str):
        """Serve a downloaded file so the UI can preview it.

        Confined to folders this server has actually downloaded into — the
        default dest plus any per-job dest — so a custom folder still previews
        while arbitrary filesystem reads stay blocked.
        """
        # The UI sends the absolute path with its leading slash stripped, so
        # restoring it is unambiguous on POSIX.
        candidate = Path("/" + rel.lstrip("/"))
        try:
            candidate = candidate.resolve()
        except OSError:
            return self._error(404, "Not found.")
        if not self.server.is_served_path(candidate) or not candidate.is_file():
            return self._error(404, "Not found.")
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        size = candidate.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        if self.command == "HEAD":
            return
        with candidate.open("rb") as fh:
            while chunk := fh.read(256 * 1024):
                self.wfile.write(chunk)

    # -- serialisation --------------------------------------------------------
    def _job_payload(self, job, brief: bool = False, include_logs: bool = False) -> dict:
        payload = job.as_dict(include_logs=include_logs)
        payload["version"] = self.manager.version
        self.server.register_root(job.opts.dest)
        for f in payload["files"]:
            _add_rel(f)
        for item in payload["items"]:
            for f in item.get("files", []):
                _add_rel(f)
        if brief:
            payload.pop("items", None)
        return payload


def _add_rel(f: dict) -> None:
    """Attach the /files/ path the UI links to. Absolute, so any job dest works."""
    if f.get("path"):
        f["rel"] = f["path"].lstrip("/")


def _first_float(values) -> float | None:
    if not values:
        return None
    try:
        return float(values[0])
    except (TypeError, ValueError):
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, defaults: Options, verbose: bool = False):
        super().__init__(address, Handler)
        self.manager = JobManager()
        self.defaults = defaults
        self.verbose = verbose
        import sys
        self.python_exe = sys.executable
        self._roots: set[Path] = set()
        self._roots_lock = threading.Lock()
        self.register_root(defaults.dest)

    def register_root(self, dest: Path) -> None:
        """Whitelist a folder for /files/ previews."""
        try:
            resolved = Path(dest).resolve()
        except OSError:
            return
        with self._roots_lock:
            self._roots.add(resolved)

    def is_served_path(self, candidate: Path) -> bool:
        with self._roots_lock:
            roots = tuple(self._roots)
        return any(
            candidate == root or str(candidate).startswith(str(root) + os.sep)
            for root in roots
        )


def serve(defaults: Options, port: int = 8722, open_browser: bool = True,
          verbose: bool = False) -> None:
    ensure_dest(defaults.dest)
    for candidate in range(port, port + 12):
        try:
            httpd = Server(("127.0.0.1", candidate), defaults, verbose)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"No free port in {port}–{port + 11}.")

    url = f"http://localhost:{httpd.server_address[1]}/"
    print(f"Instagram Bulk Downloader → {url}")
    print(f"Saving to {defaults.dest}")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
