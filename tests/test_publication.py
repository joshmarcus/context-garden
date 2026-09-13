from __future__ import annotations

import asyncio
import json

import yaml
from fastapi.testclient import TestClient

from garden.publication import build_public_projection, write_public_projection
from garden.store import Store
from garden.web.public import create_public_app, projection_events


def _configure(garden, fields):
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    product = next(iter(config["products"]))
    config["publication"] = {"projects": {product: {"fields": fields}}}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    return product


def test_publication_is_default_empty_and_rejects_unknown_fields(garden):
    assert build_public_projection(Store(garden))["projects"] == []
    product = _configure(garden, ["task.summary", "task.secret"])

    try:
        build_public_projection(Store(garden))
    except ValueError as exc:
        assert str(exc) == f"unknown public fields for {product}: task.secret"
    else:
        raise AssertionError("unknown publication field was accepted")


def test_projection_omits_private_fields_and_requires_free_form_opt_in(garden, tmp_path):
    product = _configure(
        garden, ["project.summary", "phase.summary", "task.summary", "task.dependencies"],
    )
    projection = build_public_projection(Store(garden))
    encoded = json.dumps(projection)

    assert projection["projects"][0]["id"] == product
    assert projection["projects"][0]["phases"][0]["tasks"]
    for forbidden in ("body", "content", "owner", "pr", "path", "repo", "reading", "cost", "log"):
        assert f'"{forbidden}"' not in encoded

    _configure(garden, ["task.summary", "task.content"])
    with_content = build_public_projection(Store(garden))
    assert "content" in with_content["projects"][0]["phases"][0]["tasks"][0]
    assert "## Log" not in json.dumps(with_content)


def test_public_app_has_only_positive_read_routes_and_reflects_revocation(garden, tmp_path):
    product = _configure(garden, ["project.summary", "phase.summary", "task.summary"])
    output = tmp_path / "public"
    write_public_projection(Store(garden), output)
    client = TestClient(create_public_app(output))

    assert client.get("/healthz").json() == {"ok": True, "mode": "public-viewer"}
    assert client.get("/api/projects").json()["projects"][0]["id"] == product
    assert client.get("/api/search", params={"q": "CG-"}).status_code == 200
    assert client.get("/api/projects", params={"member": "admin"}).status_code == 404
    for method, path in (
        ("post", "/tick"), ("head", "/config"), ("options", "/api/projects"),
        ("put", "/tasks/CG-001"), ("get", "/runs"), ("get", "/static/plates/x"),
    ):
        assert getattr(client, method)(path).status_code == 404

    _configure(garden, [])
    # Removing the project, rather than merely its fields, is the revocation operation.
    config = yaml.safe_load((garden / "garden.yaml").read_text())
    config["publication"]["projects"] = {}
    (garden / "garden.yaml").write_text(yaml.safe_dump(config))
    write_public_projection(Store(garden), output)
    assert client.get("/api/projects").json()["projects"] == []
    assert client.get(f"/projects/{product}").status_code == 404


def test_public_app_preserves_unavailable_state(tmp_path):
    client = TestClient(create_public_app(tmp_path / "missing"))

    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 503


def test_existing_subscription_receives_revocation(garden, tmp_path):
    product = _configure(garden, ["task.summary"])
    output = tmp_path / "public"
    write_public_projection(Store(garden), output)

    async def observe_replacement():
        stream = projection_events(output, interval=0)
        initial = await anext(stream)
        config = yaml.safe_load((garden / "garden.yaml").read_text())
        config["publication"]["projects"] = {}
        (garden / "garden.yaml").write_text(yaml.safe_dump(config))
        write_public_projection(Store(garden), output)
        revoked = await anext(stream)
        await stream.aclose()
        return initial, revoked

    initial, revoked = asyncio.run(observe_replacement())
    assert product in initial
    assert '"projects":[]' in revoked
