"""Read-only product design documents and run captures."""

from __future__ import annotations

import html
import mimetypes
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from ...runs import Run
from ...store import Store
from ..common import Site, product_checkout, product_design_root, render_md
from ..trust import safe_relative_path

# These are the artifacts a UI check or design task can render for a person.  Run directories
# also contain transcripts, briefs and run metadata; those are never captures merely because a
# worker mentions them in its result.
CAPTURE_SUFFIXES = frozenset({".gif", ".htm", ".html", ".jpeg", ".jpg", ".md", ".markdown", ".png", ".webp"})
INTERNAL_CAPTURE_NAMES = frozenset({"brief.md", "exit_code", "final.md", "run.json", "stderr.log", "stdout.json"})
CAPTURE_MEDIA_TYPES = {
    ".gif": "image/gif", ".htm": "text/html", ".html": "text/html",
    ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".md": "text/markdown",
    ".markdown": "text/markdown", ".png": "image/png", ".webp": "image/webp",
}


def _design_root(store: Store, product: str) -> Path:
    """The checkout for the requested product's design artifacts."""
    if product not in {item.name for item in store.products()}:
        raise HTTPException(404)
    return product_checkout(store, product)


def _git_file(repo: Path, ref: str, relative: str) -> bytes | None:
    try:
        return subprocess.run(["git", "show", f"{ref}:docs/design/{relative}"], cwd=repo,
                              capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def _media(path: str) -> str:
    # The OS MIME database is not consistent: a stock macOS install, for example, does
    # not identify Markdown. Captures support a closed suffix set, so make those responses
    # deterministic and leave mimetypes only for design documents outside that set.
    return CAPTURE_MEDIA_TYPES.get(Path(path).suffix.lower(),
                                   mimetypes.guess_type(path)[0] or "application/octet-stream")


def recorded_captures(run: Run) -> list[Path]:
    """Return only files explicitly reported as captures by the run.

    Checks report absolute paths while a worker may report paths relative to its run
    directory, so normalize both forms before applying the run-directory fence.
    """
    paths: set[Path] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "captures" and isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            candidate = Path(item)
                            if not candidate.is_absolute():
                                candidate = run.path / candidate
                            resolved = candidate.resolve()
                            if (run.path.resolve() in resolved.parents and resolved.is_file()
                                    and resolved.name not in INTERNAL_CAPTURE_NAMES
                                    and resolved.suffix.lower() in CAPTURE_SUFFIXES):
                                paths.add(resolved)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(run.result)
    return sorted(paths)


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/design")
    @app.get("/design/{path:path}")
    def design_file(request: Request, path: str = "", ref: str = "", product: str = ""):
        relative = safe_relative_path(path)
        if path and not relative:
            raise HTTPException(404)
        if not relative:
            store = hub.fresh()
            selected = product or store.products()[0].name
            root = product_design_root(store, selected)
            files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []
            links = "<h1>Design</h1><ul>" + "".join(
                f'<li><a href="/design/{html.escape(f, quote=True)}?product={html.escape(selected, quote=True)}">{html.escape(f)}</a></li>' for f in files
            ) + "</ul>"
            return templates.TemplateResponse(request, "design.html", ctx(request, page="design", name="Design", content=links, ref=ref))
        store = hub.fresh()
        selected = product or store.products()[0].name
        root = _design_root(store, selected)
        if ref.startswith("-"):
            raise HTTPException(404)
        if ref:
            data = _git_file(root, ref, relative)
            if data is None:
                raise HTTPException(404)
        else:
            target = (root / "docs" / "design" / relative).resolve()
            design_root = (root / "docs" / "design").resolve()
            if design_root not in target.parents or not target.is_file():
                raise HTTPException(404)
            data = target.read_bytes()
        media = _media(relative)
        if media == "text/markdown" or relative.lower().endswith((".md", ".markdown")):
            return templates.TemplateResponse(request, "design.html", ctx(
                request, page="design", name=relative, content=render_md(data.decode("utf-8")), ref=ref))
        return Response(data, media_type=media, headers={"Content-Security-Policy": "sandbox"}
                        if media == "text/html" else {})

    @app.get("/runs/{task_id}/{run_id}/captures/{path:path}")
    def capture_file(task_id: str, run_id: str, path: str):
        relative = safe_relative_path(path)
        if not relative:
            raise HTTPException(404)
        from ...runs import RunStore
        run = next((r for r in RunStore(hub.fresh().config.garden_dir).runs_for(task_id)
                    if r.run_id == run_id), None)
        if not run:
            raise HTTPException(404)
        target = (run.path / relative).resolve()
        if target not in recorded_captures(run):
            raise HTTPException(404)
        media = _media(relative)
        headers = {"Content-Security-Policy": "sandbox"} if media == "text/html" else {}
        return Response(target.read_bytes(), media_type=media, headers=headers)
