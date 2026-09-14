from __future__ import annotations

from fastapi.testclient import TestClient

from garden.pr_explanations import render
from garden.scheduler import State
from garden.store import Store
from garden.web.app import create_app


def test_report_escapes_patch_and_marks_changed_lines(garden):
    task = Store(garden).task("DM-001")
    report = render(task, base="main", head="a" * 40, diff="""diff --git a/demo.py b/demo.py
index 0000000..1111111 100644
--- a/demo.py
+++ b/demo.py
@@ -1,2 +1,2 @@
-old()
+<script>alert(1)</script>
 context()
""")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "<script>alert(1)</script>" not in report
    assert 'class="added"' in report
    assert 'class="removed"' in report
    assert "Base revision" in report
    assert "Code walkthrough" in report


def test_completed_report_is_inert_preview_and_download(garden):
    report = garden / ".garden" / "pr-explanations" / "DM-001" / "abc.html"
    report.parent.mkdir(parents=True)
    report.write_text("<!doctype html><style>body{color:green}</style><script>alert(1)</script>")
    state = State(garden / ".garden" / "state.json")
    state.get("DM-001")["pr_explanation"] = {"status": "ready", "path": str(report)}
    state.save()
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    preview = client.get("/tasks/DM-001/pr-explanation")
    assert preview.status_code == 200
    assert "sandbox" in preview.headers["content-security-policy"]
    assert "style-src 'unsafe-inline'" in preview.headers["content-security-policy"]
    assert preview.headers["content-disposition"].startswith("inline")
    download = client.get("/tasks/DM-001/pr-explanation?download=1")
    assert download.headers["content-disposition"].startswith("attachment")
