"""Command line entry point: `igdl`.

Human mode prints a live one-line-per-link view to stderr; `--json` prints one
machine-readable object to stdout so agents can pipe it straight into `jq`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import engine as engine_mod
from . import links as linkmod
from .engine import Engine, EngineError, Options, env_auth, validate_cookie_file, ytdlp_version
from .jobs import DONE, FAILED, SKIPPED, JobManager, ensure_dest

DEFAULT_DEST = Path(os.environ.get("IGBULK_DEST") or (Path.home() / "Downloads" / "instagram"))
BROWSERS = ("chrome", "safari", "firefox", "brave", "edge", "chromium", "opera", "vivaldi")

_ICON = {"done": "✔", "skipped": "•", "failed": "✗", "blocked": "!", "canceled": "–"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="igdl",
        description="Bulk-download public Instagram Reels and posts.",
        epilog=(
            "examples:\n"
            "  igdl https://instagram.com/reel/ABC/ https://instagram.com/p/XYZ/\n"
            "  igdl links.txt --dest ~/Downloads/ig\n"
            "  pbpaste | igdl                 # whatever you just copied\n"
            "  igdl --clipboard --jobs 5\n"
            "  igdl --serve                   # web UI with a paste box\n"
            "  igdl --resolve URL --json      # direct media URL, no download\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("inputs", nargs="*",
                   help="URLs, or paths to text files containing URLs. Reads stdin if empty.")
    p.add_argument("-d", "--dest", default=str(DEFAULT_DEST),
                   help=f"download folder (default: {DEFAULT_DEST})")
    p.add_argument("-j", "--jobs", type=int, default=3,
                   help="links to download at once, 1-8 (default: 3)")
    p.add_argument("-c", "--cookies", metavar="BROWSER", choices=BROWSERS,
                   help=f"read login cookies from a browser ({', '.join(BROWSERS)})")
    p.add_argument("-C", "--cookie-file", metavar="FILE",
                   help="Netscape cookies.txt to authenticate with. Preferred for "
                        "unattended use: no Keychain prompt, no Full Disk Access. "
                        f"Defaults to ${engine_mod.ENV_COOKIE_FILE} if set.")
    p.add_argument("-a", "--audio", action="store_true", help="extract audio as mp3")
    p.add_argument("--no-convert", action="store_true",
                   help="keep Instagram's original codec. Its 1080p Reels are VP9, "
                        "which QuickTime and most editors can't play — by default "
                        "they're re-encoded to h264 (~2s per Reel)")
    p.add_argument("--flat", action="store_true",
                   help="save everything directly in the folder (no per-account subfolders)")
    p.add_argument("--meta", action="store_true", help="also write .info.json metadata")
    p.add_argument("--no-skip", action="store_true",
                   help="re-download links already recorded in the archive")
    p.add_argument("--clipboard", action="store_true", help="read links from the clipboard")
    p.add_argument("--resolve", action="store_true",
                   help="print direct media URLs instead of downloading")
    p.add_argument("--dry-run", action="store_true", help="list the links that would be fetched")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="emit a single JSON object on stdout")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress progress output")
    p.add_argument("--open", action="store_true", help="reveal the folder in Finder when done")
    p.add_argument("--serve", action="store_true", help="start the local web UI instead")
    p.add_argument("--port", type=int, default=8722, help="port for --serve (default: 8722)")
    p.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    p.add_argument("--version", action="store_true", help="print versions and exit")
    return p


def gather_text(args, parser) -> str:
    chunks: list[str] = []
    for raw in args.inputs:
        path = Path(raw).expanduser()
        # Only treat it as a file if it exists — bare URLs pass straight through.
        if path.is_file():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
                continue
            except OSError as exc:
                parser.error(f"cannot read {raw}: {exc}")
        chunks.append(raw)

    if args.clipboard:
        pbpaste = shutil.which("pbpaste")
        if not pbpaste:
            parser.error("--clipboard needs pbpaste (macOS)")
        chunks.append(subprocess.run([pbpaste], capture_output=True, text=True).stdout)

    if not chunks and not sys.stdin.isatty():
        chunks.append(sys.stdin.read())
    elif not chunks:
        print("Paste Instagram links, then press Ctrl-D:", file=sys.stderr)
        try:
            chunks.append(sys.stdin.read())
        except KeyboardInterrupt:
            raise SystemExit(130)
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"igbulk 1.0 · yt-dlp {ytdlp_version() or 'not found'} "
              f"· python {sys.version.split()[0]}")
        return 0

    env_file, env_browser = env_auth()
    cookie_file = Path(args.cookie_file).expanduser() if args.cookie_file else env_file
    if cookie_file:
        try:
            cookie_file = validate_cookie_file(cookie_file)
        except EngineError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1

    opts = Options(
        dest=Path(args.dest).expanduser(),
        audio_only=args.audio,
        cookies_from=args.cookies or (None if cookie_file else env_browser),
        cookie_file=cookie_file,
        write_metadata=args.meta,
        skip_existing=not args.no_skip,
        flat=args.flat,
        ensure_h264=not args.no_convert,
    )
    # --serve users pick a cookie source in the UI, which nags on its own.
    if not opts.authenticated and not (args.dry_run or args.version or args.serve):
        print("Note: no cookies configured — Instagram refuses anonymous requests, "
              "so this will fail. Use --cookie-file FILE or --cookies chrome.",
              file=sys.stderr)

    if args.serve:
        from .server import serve
        serve(opts, port=args.port, open_browser=not args.no_browser)
        return 0

    text = gather_text(args, parser)
    parsed = linkmod.parse(text)
    if not parsed.links:
        msg = "No Instagram links found in the input."
        if args.as_json:
            print(json.dumps({"ok": False, "error": msg, "count": 0}))
        else:
            print(msg, file=sys.stderr)
        return 2

    if args.dry_run:
        payload = parsed.as_dict()
        if args.as_json:
            print(json.dumps(payload, indent=2))
        else:
            for link in parsed.links:
                note = f"  ({link.note})" if link.note else ""
                print(f"{link.kind_label:<11} {link.url}{note}")
            print(f"\n{len(parsed.links)} link(s), {parsed.duplicates} duplicate(s) removed",
                  file=sys.stderr)
        return 0

    if args.resolve:
        return _do_resolve(parsed, opts, args)

    return _do_download(parsed, opts, args)


def _do_resolve(parsed, opts: Options, args) -> int:
    engine = Engine(opts)
    results = []
    failures = 0
    for link in parsed.links:
        entry = {"url": link.url, "shortcode": link.shortcode, "kind": link.kind}
        try:
            entry.update(engine.resolve(link.url))
            entry["ok"] = True
        except EngineError as exc:
            entry["ok"] = False
            entry["error"] = str(exc)
            failures += 1
        results.append(entry)
        if not args.as_json and not args.quiet:
            if entry["ok"]:
                for media in entry.get("media", []):
                    print(media.get("url") or "")
            else:
                print(f"# {link.label}: {entry['error']}", file=sys.stderr)
    if args.as_json:
        print(json.dumps({"ok": failures == 0, "count": len(results),
                          "results": results}, indent=2))
    return 0 if failures == 0 else 1


def _do_download(parsed, opts: Options, args) -> int:
    try:
        opts.dest = ensure_dest(opts.dest)
    except OSError as exc:
        print(f"Cannot use {opts.dest}: {exc}", file=sys.stderr)
        return 1

    manager = JobManager()
    job = manager.create(parsed, opts, concurrency=args.jobs)
    live = sys.stderr.isatty() and not args.quiet and not args.as_json

    if not args.quiet and not args.as_json:
        print(f"{len(job.items)} link(s) → {opts.dest}", file=sys.stderr)

    printed: set[str] = set()
    version = 0
    try:
        while True:
            version = manager.wait_for_change(version, timeout=0.5)
            if live:
                _print_settled(job, printed)
            if job.is_complete and job.finished:
                break
    except KeyboardInterrupt:
        manager.cancel(job.id)
        print("\nCanceling…", file=sys.stderr)
        deadline = time.monotonic() + 10
        while not job.is_complete and time.monotonic() < deadline:
            time.sleep(0.2)

    if live:
        _print_settled(job, printed)

    counts = job.counts
    ok = counts[DONE] + counts[SKIPPED]

    if args.as_json:
        print(json.dumps({
            "ok": counts[FAILED] == 0,
            "dest": str(opts.dest),
            "total": len(job.items),
            "succeeded": ok,
            "failed": counts[FAILED],
            "blocked": counts["blocked"],
            "bytes": sum(i.bytes for i in job.items),
            "files": [f["path"] for i in job.items for f in i.files],
            "items": [
                {"url": i.link.url, "shortcode": i.link.shortcode, "status": i.status,
                 "uploader": i.uploader, "message": i.message,
                 "files": [f["path"] for f in i.files]}
                for i in job.items
            ],
        }, indent=2))
    elif not args.quiet:
        parts = [f"{ok} ok"]
        if counts[FAILED]:
            parts.append(f"{counts[FAILED]} failed")
        if counts["blocked"]:
            parts.append(f"{counts['blocked']} need login")
        if counts["canceled"]:
            parts.append(f"{counts['canceled']} canceled")
        total_bytes = sum(i.bytes for i in job.items)
        print(f"\n{' · '.join(parts)} · {_human(total_bytes)} → {opts.dest}", file=sys.stderr)
        if counts[FAILED]:
            print("Retry the failures with the same command, or add "
                  "`--cookies chrome` for private posts.", file=sys.stderr)

    if args.open:
        subprocess.Popen(["open", str(opts.dest)])

    return 0 if counts[FAILED] == 0 else 1


def _print_settled(job, printed: set[str]) -> None:
    """Print each link once, as soon as it reaches a terminal state."""
    total = len(job.items)
    for n, item in enumerate(job.items, 1):
        if item.status in ("queued", "running", "waiting") or item.link.key in printed:
            continue
        printed.add(item.link.key)
        icon = _ICON.get(item.status, "?")
        who = f" @{item.uploader}" if item.uploader else ""
        detail = item.message if item.status != "done" else _human(item.bytes)
        print(f"[{n}/{total}] {icon} {item.link.label}{who}  {detail}", file=sys.stderr)


def _human(n: int) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


if __name__ == "__main__":
    sys.exit(main())
