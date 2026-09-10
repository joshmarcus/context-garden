"""Inert responses for files produced outside the operator application.

Design files and run captures are useful to inspect, but are never application assets.
Keep their response policy here so every route makes the same conservative decision.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.responses import Response

from .common import render_md

# A sandbox without any allow-* token gives HTML and SVG an opaque origin, blocks scripts,
# forms, popups, and top-level navigation. The remaining directives make that contract clear.
PREVIEW_CSP = "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
SAFE_PREVIEW_TYPES = {
    ".gif": "image/gif", ".htm": "text/html", ".html": "text/html",
    ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".png": "image/png",
    ".svg": "image/svg+xml", ".webp": "image/webp",
}
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})


def _disposition(filename: str, *, attachment: bool) -> str:
    """A fixed fallback plus percent-encoded extended filename cannot split a header."""
    encoded = quote(Path(filename).name, safe="") or "artifact"
    kind = "attachment" if attachment else "inline"
    return f"{kind}; filename=\"artifact\"; filename*=UTF-8''{encoded}"


def artifact_response(data: bytes, filename: str) -> Response:
    """Render a known preview type inertly; download every other byte sequence.

    Filename extensions are an allowlist, not a trust signal: ``nosniff`` ensures a payload
    masquerading as an image is not promoted to executable markup by the browser.
    """
    suffix = Path(filename).suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        try:
            content = render_md(data.decode("utf-8"))
        except UnicodeDecodeError:
            return _download(data, filename)
        return Response(content, media_type="text/html", headers=_preview_headers(filename))
    media_type = SAFE_PREVIEW_TYPES.get(suffix)
    if media_type is None:
        return _download(data, filename)
    return Response(data, media_type=media_type, headers=_preview_headers(filename))


def _preview_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": _disposition(filename, attachment=False),
        "Content-Security-Policy": PREVIEW_CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def _download(data: bytes, filename: str) -> Response:
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": _disposition(filename, attachment=True),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Referrer-Policy": "no-referrer",
    })
