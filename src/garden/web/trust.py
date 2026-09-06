"""Trust at the web UI's edges: what a page renders and who may post to it.

Much of what the pages show was written by an agent or arrived from GitHub (task bodies
from the planner, PR feedback, review verdicts, persona reports, friction harvested from PR
descriptions), and markdown passes raw HTML through. `sanitize_html` reduces rendered
markdown to an allowlist of tags and attributes with safe link targets, so nothing a
worker or a commenter wrote can run script in the person's browser. `safe_json` makes a
JSON blob safe to inline in an attribute or a script.

Every state-changing route is a plain form POST with no token, and the server listens on
localhost, so a page on any site could post a form at it from the person's browser.
`OriginCheck` refuses a POST whose `Origin` (or, failing that, `Referer`) is not an
allowed origin: the origins the server binds to (`server_origins`) plus `web.trusted_origins`.
The request's own `Host` header is never consulted — a page whose name was rebound to the
loopback address carries the attacker's own `Origin`, which is not an allowed one, so the
POST is refused even though `Host` and `Origin` agree. A request with neither header is not
a browser's and is let through: there is no ambient credential to forge with.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def safe_relative_path(value: str) -> str:
    """Return a normalized relative POSIX path, rejecting traversal and absolute paths."""
    raw = (value or "").replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or "\x00" in part for part in parts):
        return ""
    return "/".join(parts)

# ---- rendered markdown -------------------------------------------------------

ALLOWED_TAGS: frozenset[str] = frozenset({
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code", "kbd",
    "em", "strong", "b", "i", "u", "s", "del", "ins", "sup", "sub", "small", "mark",
    "a", "img", "span", "div", "details", "summary",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
})
# Attributes kept per tag, plus `class` everywhere (markdown's `language-*` on code blocks).
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "th": frozenset({"align", "colspan", "rowspan"}),
    "td": frozenset({"align", "colspan", "rowspan"}),
    "ol": frozenset({"start"}),
    "details": frozenset({"open"}),
}
URL_ATTRS = frozenset({"href", "src"})
SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
VOID_TAGS = frozenset({"br", "hr", "img"})
# Tags dropped with their content: text inside them is not prose.
DROP_WITH_CONTENT = frozenset({"script", "style", "iframe", "object", "embed", "svg", "math",
                               "template", "textarea", "title", "noscript", "xmp", "plaintext", "head"})
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")


def safe_url(value: str) -> str:
    """`value` when it is a relative URL or uses an http(s)/mailto scheme, else ''."""
    cleaned = _CONTROL_RE.sub("", value or "")
    if not cleaned:
        return ""
    scheme = urlsplit(cleaned).scheme.lower()
    return value.strip() if not scheme or scheme in SAFE_SCHEMES else ""


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._dropping: list[str] = []  # open tags whose whole content is being dropped

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropping:
            if tag in DROP_WITH_CONTENT:
                self._dropping.append(tag)
            return
        if tag in DROP_WITH_CONTENT:
            self._dropping.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return  # the tag goes, its text stays
        keep = ALLOWED_ATTRS.get(tag, frozenset())
        parts = [f"<{tag}"]
        for name, value in attrs:
            name = name.lower()
            if name != "class" and name not in keep:
                continue
            value = value or ""
            if name in URL_ATTRS:
                value = safe_url(value)
                if not value:
                    continue
            parts.append(f' {name}="{html.escape(value, quote=True)}"')
        parts.append(" />" if tag in VOID_TAGS else ">")
        self.out.append("".join(parts))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._dropping:
            if tag == self._dropping[-1]:
                self._dropping.pop()
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self.out.append(html.escape(data, quote=False))

    # comments, doctypes, processing instructions and unknown declarations are dropped
    def handle_comment(self, data: str) -> None:
        pass

    def handle_decl(self, decl: str) -> None:
        pass

    def handle_pi(self, data: str) -> None:
        pass

    def unknown_decl(self, data: str) -> None:
        pass


def sanitize_html(fragment: str) -> str:
    """Reduce an HTML fragment to the allowlisted tags and attributes above. Disallowed tags
    lose their markup and keep their text (`script`, `style` and the like lose both);
    attributes not on the list, event handlers among them, and `javascript:`/`data:` links
    are removed; every text node and attribute value is re-escaped."""
    p = _Sanitizer()
    p.feed(fragment or "")
    p.close()
    return "".join(p.out)


def safe_json(value: Any) -> str:
    """JSON for inlining in an HTML attribute or script: the characters that could end the
    attribute or the script block are written as JSON escapes, which every parser accepts."""
    return (json.dumps(value)
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("'", "\\u0027"))


# ---- who may post ----------------------------------------------------------------

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_of(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.scheme and parts.netloc else ""


# Bind addresses reachable over the loopback interface; a bind to any of these is served to
# a browser as localhost, 127.0.0.1 or [::1], whatever address string was passed.
_LOOPBACK_BINDS = frozenset({"", "0.0.0.0", "::", "[::]", "*", "localhost", "127.0.0.1", "::1", "[::1]"})


def server_origins(host: str, port: int | None = None) -> list[str]:
    """The http origins that address `garden serve` itself, for the origin allowlist.

    A loopback or wildcard bind is reached as localhost, 127.0.0.1 or [::1]; a specific host
    is reached as itself. The request's own `Host` header is deliberately not used to derive
    this (that is exactly what a DNS-rebound page forges), so a bind to any other name needs
    its public origin listed in `web.trusted_origins`."""
    h = (host or "").strip().lower()
    hosts = ["localhost", "127.0.0.1", "[::1]"] if h in _LOOPBACK_BINDS else [h]
    suffix = f":{port}" if port else ""
    return [f"http://{name}{suffix}" for name in hosts]


def origin_problem(headers: Headers, allowed: Iterable[str] = ()) -> str:
    """Why a state-changing request must be refused, or '' when its source is an allowed origin.

    `Origin` is checked first (browsers send it on every cross-site POST); an older browser
    without it sends `Referer`. A request with neither is not a browser's form and is
    accepted. The source must be one of `allowed`: the origins the server binds to
    (`server_origins`) plus `web.trusted_origins`. The request's own `Host` header is never
    consulted, so a page whose name was rebound to the loopback address is still refused —
    its Origin is the attacker's name, which is not an allowed origin."""
    allowed_set = {a.strip().rstrip("/").lower() for a in allowed if a and a.strip()}
    origin = (headers.get("origin") or "").strip()
    via, source = ("Origin", origin) if origin else ("Referer", (headers.get("referer") or "").strip())
    if not source:
        return ""
    if source.lower() == "null":
        return "request refused: it comes from an opaque origin (Origin: null), not from this server"
    if _origin_of(source) in allowed_set:
        return ""
    return f"request refused: {via} {source!r} is not this server; see web.trusted_origins"


class OriginCheck:
    """ASGI middleware: refuse a POST (or any unsafe method) whose Origin/Referer is not an allowed origin."""

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str] = ()):
        self.app = app
        self.allowed = [str(o) for o in allowed_origins]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("method", "GET")).upper() not in SAFE_METHODS:
            problem = origin_problem(Headers(scope=scope), self.allowed)
            if problem:
                await PlainTextResponse(problem, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
