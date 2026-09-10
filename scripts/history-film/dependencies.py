"""Extract historical Python import and template-reference edges without a checkout."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True)
parser.add_argument("--data", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
D = json.loads(args.data.read_text(encoding="utf-8"))
git_base = ["git", "-C", args.repo]


def git(*argv: str) -> bytes:
    return subprocess.check_output(git_base + list(argv), timeout=120)


files = D["files"]
indices = {p: i for i, p in enumerate(files)}
modules = {}
for i, p in enumerate(files):
    if p.endswith(".py"):
        module = p.removeprefix("src/").removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        modules.setdefault(module, set()).add(i)

changes = {}
all_changes = {}
previous = None
for commit in D["commits"]:
    current = commit["sha"]
    revisions = [previous, current] if previous else [current]
    fields = (
        git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "-r",
            "--raw",
            "-z",
            "--no-renames",
            "--abbrev=40",
            *revisions,
        )
        .decode()
        .rstrip("\0")
        .split("\0")
    )
    changes[current] = []
    all_changes[current] = []
    if fields != [""]:
        for offset in range(0, len(fields), 2):
            metadata, name = fields[offset : offset + 2]
            mode1, mode2, old, new, op = metadata[1:].split()
            if name in indices:
                all_changes[current].append((indices[name], new, op))
            if name in indices and (
                name.endswith(".py")
                or "/templates/" in name
                and name.endswith((".html", ".jinja", ".jinja2"))
            ):
                changes[current].append((indices[name], new, op))
    previous = current

blobs = sorted(
    {
        sha
        for entries in changes.values()
        for _, sha, op in entries
        if op != "D" and set(sha) != {"0"}
    }
)
proc = subprocess.Popen(
    git_base + ["cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
)
packed, _ = proc.communicate(("\n".join(blobs) + "\n").encode())
if proc.returncode:
    raise RuntimeError("git cat-file failed")
contents = {}
cursor = 0
for sha in blobs:
    end = packed.index(b"\n", cursor)
    header = packed[cursor:end].decode().split()
    length = int(header[-1])
    cursor = end + 1
    contents[sha] = packed[cursor : cursor + length].decode("utf-8", errors="replace")
    cursor += length + 1

all_blobs = sorted(
    {
        sha
        for entries in all_changes.values()
        for _, sha, op in entries
        if op != "D" and set(sha) != {"0"}
    }
)
sizes_proc = subprocess.Popen(
    git_base + ["cat-file", "--batch-check=%(objectname) %(objectsize)"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
size_output, _ = sizes_proc.communicate(("\n".join(all_blobs) + "\n").encode())
if sizes_proc.returncode:
    raise RuntimeError("git blob-size extraction failed")
blob_sizes = {line.split()[0]: int(line.split()[1]) for line in size_output.decode().splitlines()}


def resolve(module: str, aliases: list[str]) -> set[int]:
    targets = set()
    if module in modules:
        targets.update(modules[module])
    for name in aliases:
        child = module + "." + name if module else name
        if child in modules:
            targets.update(modules[child])
    return targets


errors = []
cache = {}


def dependencies(i: int, sha: str) -> list[list]:
    key = (i, sha)
    if key in cache:
        return cache[key]
    p = files[i]
    source = contents[sha]
    result = set()
    if p.endswith(".py"):
        try:
            tree = ast.parse(source, filename=p)
        except SyntaxError as exc:
            errors.append({"path": p, "blob": sha, "error": str(exc)})
            cache[key] = []
            return []
        own = p.removeprefix("src/").removesuffix(".py").replace("/", ".")
        package = own[:-9] if own.endswith(".__init__") else own.rpartition(".")[0]
        for node in ast.walk(tree):
            targets = set()
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets |= resolve(alias.name, [])
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    base = package.split(".")
                    base = base[: max(0, len(base) - node.level + 1)]
                    module = ".".join(base + ([module] if module else []))
                targets = resolve(module, [a.name for a in node.names])
            for target in targets:
                if target != i:
                    result.add((target, "import"))
            # Literal template names passed by Python identify inspectable UI dependencies.
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
                if name in {
                    "TemplateResponse",
                    "get_template",
                    "render_template",
                    "_render",
                    "render",
                }:
                    for arg in list(node.args) + [kw.value for kw in node.keywords]:
                        if (
                            isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.endswith(".html")
                        ):
                            target = indices.get("src/garden/web/templates/" + arg.value)
                            if target is not None and target != i:
                                result.add((target, "template"))
    else:
        for ref in re.findall(
            r"""\{%\s*(?:extends|include|import|from)\s+["']([^"']+)["']""", source
        ):
            target = indices.get("src/garden/web/templates/" + ref)
            if target is not None and target != i:
                result.add((target, "template"))
    cache[key] = [list(x) for x in sorted(result)]
    return cache[key]


versions = []
state = {}
existing = set()
edge_counts = []
for commit in D["commits"]:
    for i, op in commit["changes"]:
        if op == "D":
            existing.discard(i)
            state.pop(i, None)
        else:
            existing.add(i)
    updates = []
    for i, sha, op in changes.get(commit["sha"], []):
        values = [] if op == "D" else dependencies(i, sha)
        state[i] = values
        updates.append([i, values])
    sizes = [
        [i, 0 if op == "D" else blob_sizes[sha]]
        for i, sha, op in all_changes.get(commit["sha"], [])
    ]
    versions.append({"sha": commit["sha"], "updates": updates, "sizes": sizes})
    edge_counts.append(
        sum(1 for i, links in state.items() if i in existing for j, _ in links if j in existing)
    )

all_edges = sorted({(i, j, kind) for (i, _), links in cache.items() for j, kind in links})
result = {
    "sourceHead": D["sourceHead"],
    "method": "Python AST import resolution and literal template references, reconstructed for each first-parent commit. External packages and dynamic imports are omitted. File sizes use actual Git blob byte lengths.",
    "versions": versions,
    "edges": [list(e) for e in all_edges],
    "stats": {
        "uniqueBlobs": len(blobs),
        "parsedVersions": len(cache),
        "historicalEdges": len(all_edges),
        "finalEdges": edge_counts[-1],
        "parseErrors": len(errors),
        "sizedBlobs": len(blob_sizes),
    },
    "errors": errors,
}
args.output.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
print(json.dumps(result["stats"], indent=2))
