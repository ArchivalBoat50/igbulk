"""MCP stdio server so an agent can download Instagram media as a tool call.

Speaks JSON-RPC 2.0 over stdin/stdout with no third-party dependencies. Register
it with:

    claude mcp add igbulk -- /path/to/python -m igbulk.mcp

Tools: instagram_parse_links, instagram_resolve, instagram_download.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from . import links as linkmod
from .engine import (Engine, EngineError, Options, env_auth, validate_cookie_file,
                     ytdlp_version)
from .jobs import JobManager, ensure_dest

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_DEST = Path.home() / "Downloads" / "instagram"
MAX_LINKS_PER_CALL = 100

_write_lock = threading.Lock()

TOOLS = [
    {
        "name": "instagram_parse_links",
        "description": (
            "Extract and de-duplicate Instagram Reel/post links from a blob of text. "
            "Cheap and offline — call it first to show the user what will be fetched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Any text containing Instagram URLs."}
            },
            "required": ["text"],
        },
    },
    {
        "name": "instagram_resolve",
        "description": (
            "Return metadata and direct CDN media URLs for public Instagram Reels/posts "
            "without downloading anything. Use when the caller only needs a playable/"
            "downloadable URL, dimensions, duration, or caption."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "Instagram post/Reel URLs."},
                "cookies_from": {"type": "string",
                                 "description": "Browser to read login cookies from "
                                                "(chrome, safari, firefox…)."},
                "cookie_file": {"type": "string",
                                "description": "Path to a Netscape cookies.txt. Usually "
                                               "unnecessary: set IGBULK_COOKIES instead."},
            },
            "required": ["urls"],
        },
    },
    {
        "name": "instagram_download",
        "description": (
            "Bulk-download Instagram Reels/posts (video, image carousels, or audio) to "
            "local disk and return the file paths. Blocks until finished. Public posts "
            "need no login; private ones need cookies_from."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "Instagram post/Reel URLs. Up to "
                                        f"{MAX_LINKS_PER_CALL} per call."},
                "dest": {"type": "string",
                         "description": f"Download folder. Default {DEFAULT_DEST}."},
                "concurrency": {"type": "integer", "minimum": 1, "maximum": 8,
                                "description": "Parallel downloads (default 3). Higher "
                                               "risks Instagram rate-limiting."},
                "audio_only": {"type": "boolean",
                               "description": "Extract mp3 audio instead of video."},
                "flat": {"type": "boolean",
                         "description": "Save all files directly in dest without "
                                        "per-account subfolders."},
                "skip_existing": {"type": "boolean",
                                  "description": "Skip links already downloaded (default true)."},
                "ensure_h264": {"type": "boolean",
                                "description": "Re-encode to h264 so the file plays in "
                                               "QuickTime and video editors (default true). "
                                               "Instagram's 1080p Reels are VP9, which most "
                                               "players can't decode."},
                "cookies_from": {"type": "string",
                                 "description": "Browser to read login cookies from "
                                                "(chrome, firefox, safari…)."},
                "cookie_file": {"type": "string",
                                "description": "Path to a Netscape cookies.txt. Preferred "
                                               "for unattended use. Usually unnecessary: "
                                               "set the IGBULK_COOKIES env var once instead."},
            },
            "required": ["urls"],
        },
    },
]


# --- JSON-RPC plumbing --------------------------------------------------------
def send(message: dict) -> None:
    data = json.dumps(message)
    with _write_lock:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


def reply(req_id, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def tool_result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
        "isError": is_error,
    }


# --- tool implementations ----------------------------------------------------
def _links_from(args: dict) -> linkmod.ParseResult:
    urls = args.get("urls") or args.get("text") or []
    if isinstance(urls, str):
        parsed = linkmod.parse(urls)
    else:
        parsed = linkmod.parse_urls(urls)
    if len(parsed.links) > MAX_LINKS_PER_CALL:
        parsed.links = parsed.links[:MAX_LINKS_PER_CALL]
    return parsed


def _options_from(args: dict) -> Options:
    """Build options, falling back to the environment.

    An agent registration can set IGBULK_COOKIES once (see README) so every tool
    call is authenticated without the model handling credentials at all.
    """
    env_file, env_browser = env_auth()
    raw = args.get("cookie_file")
    cookie_file = validate_cookie_file(Path(str(raw))) if raw else env_file
    if cookie_file and not raw:
        cookie_file = validate_cookie_file(cookie_file)
    return Options(
        dest=ensure_dest(args.get("dest") or DEFAULT_DEST),
        audio_only=bool(args.get("audio_only")),
        cookies_from=(args.get("cookies_from") or (None if cookie_file else env_browser)),
        cookie_file=cookie_file,
        skip_existing=bool(args.get("skip_existing", True)),
        flat=bool(args.get("flat")),
        ensure_h264=bool(args.get("ensure_h264", True)),
    )


def tool_parse(args: dict) -> dict:
    parsed = linkmod.parse(str(args.get("text") or ""))
    return tool_result({
        "count": len(parsed.links),
        "duplicates_removed": parsed.duplicates,
        "links": [
            {"url": l.url, "kind": l.kind, "shortcode": l.shortcode,
             "needs_login": not l.supported}
            for l in parsed.links
        ],
        "ignored": parsed.rejected,
    })


def tool_resolve(args: dict) -> dict:
    parsed = _links_from(args)
    if not parsed.links:
        return tool_result({"error": "No Instagram links found."}, is_error=True)
    engine = Engine(_options_from(args))
    results = []
    for link in parsed.links:
        entry = {"url": link.url, "shortcode": link.shortcode}
        try:
            entry.update(engine.resolve(link.url))
            entry["ok"] = True
        except EngineError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
        results.append(entry)
    return tool_result({"count": len(results), "results": results})


def tool_download(args: dict) -> dict:
    parsed = _links_from(args)
    if not parsed.links:
        return tool_result({"error": "No Instagram links found."}, is_error=True)
    opts = _options_from(args)
    manager = JobManager()
    job = manager.create(parsed, opts,
                         concurrency=int(args.get("concurrency") or 3))
    version = 0
    while not (job.is_complete and job.finished):
        version = manager.wait_for_change(version, timeout=5.0)

    counts = job.counts
    return tool_result({
        "ok": counts["failed"] == 0,
        "dest": str(opts.dest),
        "succeeded": counts["done"] + counts["skipped"],
        "failed": counts["failed"],
        "needs_login": counts["blocked"],
        "total_bytes": sum(i.bytes for i in job.items),
        "files": [f["path"] for i in job.items for f in i.files],
        "items": [
            {"url": i.link.url, "shortcode": i.link.shortcode, "status": i.status,
             "uploader": i.uploader, "detail": i.message,
             "files": [f["path"] for f in i.files]}
            for i in job.items
        ],
    }, is_error=counts["failed"] > 0 and (counts["done"] + counts["skipped"]) == 0)


HANDLERS = {
    "instagram_parse_links": tool_parse,
    "instagram_resolve": tool_resolve,
    "instagram_download": tool_download,
}


# --- main loop ---------------------------------------------------------------
def handle(request: dict) -> None:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        reply(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "igbulk", "version": "1.0",
                           "ytdlp": ytdlp_version()},
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        reply(req_id, {})
        return
    if method == "tools/list":
        reply(req_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            reply_error(req_id, -32602, f"Unknown tool: {name}")
            return
        try:
            reply(req_id, handler(params.get("arguments") or {}))
        except EngineError as exc:
            reply(req_id, tool_result({"error": str(exc)}, is_error=True))
        except Exception as exc:  # pragma: no cover
            reply(req_id, tool_result(
                {"error": f"{type(exc).__name__}: {exc}"}, is_error=True))
        return
    if req_id is not None:
        reply_error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(request, list):          # batch
            for sub in request:
                handle(sub)
        elif isinstance(request, dict):
            handle(request)
    return 0


if __name__ == "__main__":
    sys.exit(main())
