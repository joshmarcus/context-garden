from __future__ import annotations

import hashlib
import json

import pytest

from garden.transcripts import TranscriptError, TranscriptStore


def test_attempt_stream_is_ordered_idempotent_and_durably_finalized(tmp_path):
    store = TranscriptStore(tmp_path, "generation-one")
    first = b'{"sequence":0,"channel":"stdout","data":"hello\\n"}\n'
    second = b'{"sequence":1,"channel":"stderr","data":"warning\\n"}\n'

    receipt = store.append(0, first, hashlib.sha256(first).hexdigest())
    replay = store.append(0, first, hashlib.sha256(first).hexdigest())
    receipt = store.append(receipt.offset, second, hashlib.sha256(second).hexdigest())
    metadata = store.finalize(receipt.offset, receipt.sha256, {
        "task_id": "CG-506", "run_id": "run-1", "event_count": 2,
    })

    assert replay.offset == len(first)
    assert store.stream.read_bytes() == first + second
    assert metadata["status"] == "complete" and metadata["event_count"] == 2
    assert json.loads(store.metadata.read_text())["sha256"] == receipt.sha256


def test_attempt_stream_rejects_gap_conflict_corruption_and_limit(tmp_path):
    store = TranscriptStore(tmp_path, "generation-one", max_bytes=4)
    data = b"abcd"
    store.append(0, data, hashlib.sha256(data).hexdigest())

    with pytest.raises(TranscriptError, match="ahead"):
        store.append(8, b"x", hashlib.sha256(b"x").hexdigest())
    with pytest.raises(TranscriptError, match="conflicts"):
        store.append(0, b"z", hashlib.sha256(b"z").hexdigest())
    with pytest.raises(TranscriptError, match="checksum"):
        store.append(4, b"x", "bad")
    with pytest.raises(TranscriptError, match="limit"):
        store.append(4, b"x", hashlib.sha256(b"x").hexdigest())


def test_reclaimed_generation_preserves_each_attempt(tmp_path):
    old = TranscriptStore(tmp_path, "old-generation")
    new = TranscriptStore(tmp_path, "new-generation")
    old.append(0, b"partial", hashlib.sha256(b"partial").hexdigest())
    new.append(0, b"replacement", hashlib.sha256(b"replacement").hexdigest())

    assert old.stream.read_bytes() == b"partial"
    assert new.stream.read_bytes() == b"replacement"
    assert old.root != new.root
