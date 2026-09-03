"""Pull Instagram links out of arbitrary pasted text and canonicalise them.

People paste messy things: links wrapped in quotes, separated by commas, with
`?igsh=` tracking junk, duplicated across a chat log. Everything here is pure
string work so it can be unit-tested without touching the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HOSTS = (
    "instagram.com",
    "instagr.am",
    "ig.me",
    # front-end mirrors people copy from Discord/Telegram
    "ddinstagram.com",
    "instagramez.com",
    "kkinstagram.com",
)

_HOST_ALT = "|".join(h.replace(".", r"\.") for h in HOSTS)
# Commas are excluded so `url1,url2` splits; nothing Instagram serves uses them.
_URL_RE = re.compile(rf"(?:https?://)?(?:[\w-]+\.)*(?:{_HOST_ALT})/[^\s<>\"'`,]*", re.I)
# Links pasted back-to-back with no separator at all: …/reel/A/https://…/p/B/
_SPLIT_RE = re.compile(r"(?=https?://)", re.I)

# /p/<code>, /reel/<code>, /reels/<code>, /tv/<code>, optionally prefixed by a
# username as in instagram.com/nasa/reel/<code>/
_POST_RE = re.compile(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]{4,})", re.I)
_SHARE_RE = re.compile(r"/share/(?:reel/|p/|video/)?([A-Za-z0-9_-]{4,})", re.I)
_STORY_RE = re.compile(r"/stories/([^/?#]+)/(\d+)", re.I)
_HIGHLIGHT_RE = re.compile(r"/stories/highlights/(\d+)", re.I)
_PROFILE_RE = re.compile(r"^/([A-Za-z0-9._]{1,30})/?$")

_TRAILING_JUNK = ".,;:!?)]}>'\"`’”"

# Path segments that are Instagram features rather than usernames.
_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "share", "explore", "accounts",
    "direct", "about", "developer", "legal", "web", "s", "graphql", "api",
}

KIND_LABELS = {
    "reel": "Reel",
    "post": "Post",
    "tv": "IGTV",
    "share": "Share link",
    "story": "Story",
    "highlight": "Highlight",
    "profile": "Profile",
    "unknown": "Unknown",
}

# Kinds that Instagram will not serve without an authenticated session.
NEEDS_LOGIN = {"story", "highlight", "profile"}


@dataclass
class Link:
    """One de-duplicated download target."""

    url: str                      # canonical URL handed to yt-dlp
    kind: str                     # reel | post | tv | share | story | highlight | profile | unknown
    key: str                      # de-dupe identity
    shortcode: str | None = None
    raw: str = ""                 # first form we saw it in
    note: str = ""                # why it may not work, if we can tell up front
    dupes: int = 0                # how many times it repeated in the paste

    @property
    def label(self) -> str:
        return self.shortcode or self.key

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def supported(self) -> bool:
        return self.kind not in NEEDS_LOGIN

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "key": self.key,
            "shortcode": self.shortcode,
            "label": self.label,
            "note": self.note,
            "dupes": self.dupes,
            "supported": self.supported,
        }


@dataclass
class ParseResult:
    links: list[Link] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)   # looked like a link, wasn't usable
    duplicates: int = 0

    def as_dict(self) -> dict:
        return {
            "links": [l.as_dict() for l in self.links],
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "count": len(self.links),
        }


def _clean(candidate: str) -> str:
    """Trim wrapping punctuation and tracking query strings."""
    url = candidate.strip().strip(_TRAILING_JUNK)
    # Balance parens: "(https://…/reel/x/)" already handled, but markdown
    # "[text](url)" can leave a leading bracket on the host.
    url = url.lstrip("([<\"'`")
    url, _, _ = url.partition("#")
    base, sep, query = url.partition("?")
    if sep:
        # Everything Instagram puts in the query string is tracking or a
        # carousel offset; dropping it means we fetch the whole carousel.
        url = base
    return url.rstrip(_TRAILING_JUNK)


def _path_of(url: str) -> str:
    stripped = re.sub(r"^https?://", "", url, flags=re.I)
    _, sep, path = stripped.partition("/")
    return ("/" + path) if sep else "/"


def classify(url: str) -> Link | None:
    """Turn one candidate URL into a `Link`, or None if it is not a target."""
    url = _clean(url)
    if not url:
        return None
    path = _path_of(url)

    # /share/… must be tested before /reel/… — a share URL contains both.
    m = _SHARE_RE.search(path)
    if m:
        # Opaque redirect; the real shortcode only appears after the request, so
        # hand the URL to yt-dlp untouched.
        return Link(
            url=_absolute(url),
            kind="share",
            key=f"share:{m.group(1)}",
            raw=url,
            note="Share link — resolved by following the redirect.",
        )

    m = _POST_RE.search(path)
    if m:
        kind = {"reel": "reel", "reels": "reel", "p": "post", "tv": "tv"}[m.group(1).lower()]
        code = m.group(2)
        prefix = "reel" if kind == "reel" else ("tv" if kind == "tv" else "p")
        return Link(
            url=f"https://www.instagram.com/{prefix}/{code}/",
            kind=kind,
            key=code,
            shortcode=code,
            raw=url,
        )

    m = _HIGHLIGHT_RE.search(path)
    if m:
        return Link(
            url=_absolute(url),
            kind="highlight",
            key=f"highlight:{m.group(1)}",
            raw=url,
            note="Highlights require a logged-in session.",
        )

    m = _STORY_RE.search(path)
    if m:
        return Link(
            url=_absolute(url),
            kind="story",
            key=f"story:{m.group(2)}",
            raw=url,
            note="Stories require a logged-in session.",
        )

    m = _PROFILE_RE.match(path)
    if m and m.group(1).lower() not in _RESERVED:
        return Link(
            url=_absolute(url),
            kind="profile",
            key=f"profile:{m.group(1).lower()}",
            raw=url,
            note="Whole-profile links need a login. Paste individual post links instead.",
        )

    return None


def _absolute(url: str) -> str:
    if re.match(r"^https?://", url, flags=re.I):
        return url
    return "https://" + url.lstrip("/")


def parse(text: str) -> ParseResult:
    """Extract every distinct Instagram target from a blob of pasted text.

    Order is preserved (first occurrence wins) so the UI list matches the paste.
    """
    result = ParseResult()
    seen: dict[str, Link] = {}

    candidates = [
        piece
        for match in _URL_RE.findall(text or "")
        for piece in _SPLIT_RE.split(match)
        if piece
    ]
    for candidate in candidates:
        link = classify(candidate)
        if link is None:
            cleaned = _clean(candidate)
            if cleaned and cleaned not in result.rejected:
                result.rejected.append(cleaned)
            continue
        existing = seen.get(link.key)
        if existing is not None:
            existing.dupes += 1
            result.duplicates += 1
            continue
        seen[link.key] = link
        result.links.append(link)

    return result


def parse_urls(items) -> ParseResult:
    """Same as `parse` but for an already-split list of strings."""
    return parse("\n".join(str(i) for i in items))
