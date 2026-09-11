"""Select and verify deterministic file shards for the ordinary pytest suite."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def test_files(root: Path) -> list[Path]:
    """Return every conventionally collected test module."""
    return sorted((root / "tests").rglob("test_*.py"))


def shards(root: Path, count: int) -> list[list[Path]]:
    """Balance modules by source size while assigning every module exactly once."""
    if count < 1:
        raise ValueError("shard count must be positive")
    files = sorted(test_files(root), key=lambda path: (-path.stat().st_size, str(path)))
    if count > len(files):
        raise ValueError(f"shard count {count} exceeds the {len(files)} test modules")
    buckets: list[tuple[int, list[Path]]] = [(0, []) for _ in range(count)]
    for path in files:
        index = min(range(count), key=lambda item: (buckets[item][0], item))
        weight, members = buckets[index]
        members.append(path)
        buckets[index] = weight + path.stat().st_size, members
    return [members for _weight, members in buckets]


def collect(root: Path, files: list[Path] | None = None) -> set[str]:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if files is not None:
        command.extend(str(path.relative_to(root)) for path in files)
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=120)
    if result.returncode not in {0, 5}:
        raise RuntimeError(result.stdout + result.stderr)
    return {line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line}


def verify(root: Path, count: int) -> None:
    groups = shards(root, count)
    ordinary = collect(root)
    inventories = [collect(root, group) for group in groups]
    union = set().union(*inventories)
    overlap = sum(map(len, inventories)) - len(union)
    if not ordinary:
        raise RuntimeError("ordinary pytest collection is empty")
    if overlap or union != ordinary or any(not inventory for inventory in inventories):
        missing = ordinary - union
        extra = union - ordinary
        raise RuntimeError(
            f"invalid shard inventory: overlap={overlap}, missing={len(missing)}, "
            f"extra={len(extra)}, empty={sum(not item for item in inventories)}"
        )
    counts = ", ".join(str(len(inventory)) for inventory in inventories)
    print(f"verified {len(ordinary)} ordinary cases exactly once across {count} shards: {counts}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("files", "verify"))
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--index", type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        groups = shards(root, args.count)
        if args.command == "verify":
            verify(root, args.count)
        else:
            if args.index is None or not 1 <= args.index <= args.count:
                parser.error("files requires --index between 1 and --count")
            group = groups[args.index - 1]
            if not group:
                raise ValueError(f"shard {args.index}/{args.count} is empty")
            print("\n".join(str(path.relative_to(root)) for path in group))
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
