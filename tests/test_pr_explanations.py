from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from garden.pr_explanations import generate, render
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
+def demo(): # <script>alert(1)</script>
 context()
""")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "<script>alert(1)</script>" not in report
    assert 'class="added"' in report
    assert 'class="removed"' in report
    assert "Base revision" in report
    assert "Code walkthrough" in report
    assert "Responsibility." in report
    assert "Why this change matters." in report
    assert "component inventory, not an execution flow" in report
    assert 'class="kw">' in report
    assert "conservative inferences" in report


def test_report_explains_source_evidenced_responsibilities_and_interactions(garden):
    task = Store(garden).task("DM-001")
    diff = """diff --git a/core.py b/core.py
--- a/core.py
+++ b/core.py
@@ -0,0 +1,2 @@
+def build_report():
+    return "ready"
diff --git a/page.py b/page.py
--- a/page.py
+++ b/page.py
@@ -0,0 +1,4 @@
+from .core import build_report
+@app.post("/explain")
+def explain():
+    return build_report()
"""

    report = render(task, base="main", head="a" * 40, diff=diff, sources={
        "core.py": 'def build_report():\n    return "ready"\n',
        "page.py": 'from .core import build_report\n@app.post("/explain")\ndef explain():\n    return build_report()\n',
    })

    assert "Defines the changed application behavior in `build_report`." in report
    assert "Handles the HTTP endpoint `/explain`." in report
    assert "Observable changes evidenced by additions" in report
    assert "page.py adds the endpoint `/explain`" in report
    assert "core.py adds `build_report`" in report
    assert "page.py</a> → <a href=\"#file-1\">core.py" in report
    assert "references `build_report`" in report
    assert "delegates work to `build_report`" in report


def test_generation_refuses_a_checkout_other_than_recorded_pr_head(garden, monkeypatch):
    task = Store(garden).task("DM-001")
    monkeypatch.setattr("garden.pr_explanations.gitops.head_sha", lambda _repo: "b" * 40)

    with pytest.raises(RuntimeError, match="source moved"):
        generate(task, garden_dir=garden / ".garden", repo=garden, base="main",
                 expected_head="a" * 40)


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


def test_explain_button_generates_and_opens_report(garden, monkeypatch):
    store = Store(garden)
    task = store.task("DM-001")
    task.pr = "https://github.example.test/acme/demo/pull/7"
    store.save(task)
    report = garden / ".garden" / "pr-explanations" / "DM-001" / "abc.html"
    report.parent.mkdir(parents=True)
    report.write_text("<!doctype html><h1>Explanation</h1>")
    monkeypatch.setattr("garden.pr_explanations.generate", lambda *_args, **_kwargs: {
        "status": "ready", "path": str(report), "base": "main", "head": "abc", "generated_at": "now",
    })
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    assert "Explain to me" in client.get("/tasks/DM-001").text
    response = client.post("/tasks/DM-001/explain-pr", headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    assert "Open explanation" in response.text
    assert client.get("/tasks/DM-001/pr-explanation").status_code == 200


def test_explain_button_materializes_the_recorded_pr_head(garden, monkeypatch):
    store = Store(garden)
    task = store.task("DM-001")
    task.pr = "https://github.example.test/acme/demo/pull/7"
    task.branch = "garden/dm-001"
    store.save(task)
    state = State(garden / ".garden" / "state.json")
    state.get(task.id).update({"head_sha": "a" * 40, "pr_number": 7})
    state.save()
    seen = {}

    def prepare(repo, worktree, branch, base, expected):
        seen.update(branch=branch, base=base, expected=expected)
        return worktree

    monkeypatch.setattr("garden.gitops.prepare_review_worktree", prepare)
    monkeypatch.setattr("garden.pr_explanations.generate", lambda *_args, **kwargs: {
        "status": "ready", "path": str(garden / "report.html"), "base": kwargs["base"],
        "head": kwargs["expected_head"], "generated_at": "now",
    })
    client = TestClient(create_app(Store(garden), watch=False, host="testserver"))

    response = client.post(f"/tasks/{task.id}/explain-pr", headers={"Origin": "http://testserver"})

    assert response.status_code == 200
    assert seen == {"branch": task.branch, "base": "main", "expected": "a" * 40}
