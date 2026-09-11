from pathlib import Path

import pytest

from scripts.pytest_shards import shards


def write_test(root: Path, name: str, size: int = 1) -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size)
    return path


def test_shards_partition_all_test_modules_once_and_balance_by_size(tmp_path):
    files = {
        write_test(tmp_path, "test_large.py", 9),
        write_test(tmp_path, "test_medium.py", 5),
        write_test(tmp_path, "nested/test_small.py", 4),
    }
    write_test(tmp_path, "helper.py", 100)

    groups = shards(tmp_path, 2)

    assert set().union(*map(set, groups)) == files
    assert sum(map(len, groups)) == len(files)
    assert [sum(path.stat().st_size for path in group) for group in groups] == [9, 9]


def test_new_test_module_is_assigned_automatically(tmp_path):
    original = write_test(tmp_path, "test_original.py")
    assert shards(tmp_path, 1) == [[original]]

    added = write_test(tmp_path, "test_added.py")

    assert set(shards(tmp_path, 1)[0]) == {original, added}


@pytest.mark.parametrize("count", [0, 3])
def test_invalid_or_empty_shard_configuration_fails(tmp_path, count):
    write_test(tmp_path, "test_only.py")
    with pytest.raises(ValueError, match="shard count"):
        shards(tmp_path, count)
