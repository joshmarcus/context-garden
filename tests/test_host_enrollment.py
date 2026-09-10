import datetime as dt
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import httpx
import pytest

import garden.hosts.enrollment as enrollment_module
from garden.hosts.enrollment import ProductionEnrollmentResolver
from garden.hosts.enrollment_clients import (
    BotoSecretsManagerClient,
    GitHubDeployKeyClient,
    TailscaleOAuthClient,
)
from garden.hosts.models import EnvironmentProfile, PoolDeclaration


class Secrets:
    def __init__(self):
        self.values = {}
        self.fail_delete = False
        self.fail_after_create = False
        self.fail_after_put = False
        self.envelopes = []
        self.puts = []

    def describe(self, ref):
        return self.values.get(ref) or next(
            (row for row in self.values.values() if row["arn"] == ref), None
        )

    def create(self, ref, token, envelope, tags):
        self.envelopes.append(envelope)
        row = {
            "arn": f"arn:aws:secretsmanager:us-east-1:350111791226:secret:{ref}-ABC123",
            "client_token": token,
            "tags": tags,
            "digest": hash(json.dumps(envelope, sort_keys=True)),
            "deleted": False,
        }
        self.values[ref] = row
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("lost response")
        return row

    def schedule_delete(self, ref, recovery_days):
        if self.fail_delete:
            raise RuntimeError("no")
        self.describe(ref)["deleted"] = recovery_days == 7

    def put(self, ref, token, envelope, tags):
        row = self.describe(ref)
        assert row["tags"] == tags
        row["client_token"] = token
        self.envelopes.append(envelope)
        self.puts.append(token)
        if self.fail_after_put:
            self.fail_after_put = False
            raise RuntimeError("lost put response")
        return row


class Tail:
    def __init__(self):
        self.rows = {}
        self.deleted = []
        self.fail_after_create = False
        self.created = 0

    def find_key(self, description):
        return self.rows.get(description)

    def create_key(self, **kw):
        self.created += 1
        row = {
            "id": str(self.created),
            "key": f"tskey-auth-private-{self.created}",
            "description": kw["description"],
            "expires": "2030-01-01T00:00:00+00:00",
            "capabilities": {"devices": {"create": {
                "reusable": kw["reusable"], "ephemeral": kw["ephemeral"],
                "preauthorized": kw["preauthorized"], "tags": [kw["tag"]],
            }}},
        }
        self.rows[kw["description"]] = row
        if self.fail_after_create:
            self.fail_after_create = False
            row.pop("key")
            raise RuntimeError("lost response")
        return row

    def delete_key(self, key):
        self.deleted.append(str(key))
        self.rows = {k: v for k, v in self.rows.items() if str(v["id"]) != str(key)}


class GitHub:
    def __init__(self):
        self.rows = {}
        self.deleted = []
        self.fail_after_create = False

    def find_deploy_key(self, repo, title, public):
        return self.rows.get(title)

    def create_deploy_key(self, repo, title, public, read_only):
        assert read_only is False
        row = {"id": len(self.rows) + 1}
        self.rows[title] = row
        if self.fail_after_create:
            self.fail_after_create = False
            raise RuntimeError("lost response")
        return row

    def delete_deploy_key(self, repo, key):
        self.deleted.append(str(key))
        self.rows = {k: v for k, v in self.rows.items() if str(v["id"]) != str(key)}


def setup(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, parents=True)
    model = tmp_path / "model.json"
    model.write_text('{"tokens":{"access_token":"a","refresh_token":"r"}}')
    model.chmod(0o600)
    profile = EnvironmentProfile(
        "p",
        "a" * 40,
        "ami-1",
        "bootstrap-v1",
        4,
        1024,
        20,
        endpoint="https://garden",
        enrollment_secret_ref="context-garden/phase05/renew-{host_id}",
        source_head="b" * 40,
    )
    pool = PoolDeclaration(
        "workers",
        "owner",
        "work",
        "ec2",
        profile,
        True,
        0,
        2,
        2,
        spend_limit_usd=10,
        provider_options={"bootstrap_sha256": "c" * 64},
    )
    op = tmp_path / "operation.json"
    op.write_text(
        json.dumps(
            {
                "operation_id": "scale-id",
                "source_head": "b" * 40,
                "desired": 2,
                "deadline": "2030-01-01T00:00:00+00:00",
                "admitted_declaration": asdict(pool),
            }
        )
    )
    cfg = {
        "model_auth_files": {"workers-0": str(model)},
        "model_identities": {"workers-0": "model-0"},
        "github_repo": "o/r",
        "endpoint": "https://garden",
        "secret_tags": {
            "ManagedBy": "context-garden",
            "Pool": "phase05",
            "Purpose": "worker-bootstrap",
            "OperationId": "renew-six-scale-id",
        },
    }
    clients = Secrets(), Tail(), GitHub()
    resolver = ProductionEnrollmentResolver(
        private,
        cfg,
        pool,
        op,
        secrets_client=clients[0],
        tailscale_client=clients[1],
        github_client=clients[2],
        keygen=lambda h: ("PRIVATE", "ssh-ed25519 PUBLIC " + h),
        now=lambda: dt.datetime(2029, 1, 1, tzinfo=dt.UTC),
    )
    return resolver, clients, model


def test_ensure_is_resumable_private_and_registry_has_only_hash(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    first = resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    second = resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    assert (
        first == second
        and len(clients[0].values) == len(clients[1].rows) == len(clients[2].rows) == 1
    )
    registry = json.loads((resolver.root / "controller-hosts.json").read_text())
    assert set(registry) == {"hosts"} and "token_sha256" in registry["hosts"][0]
    assert "worker_token" not in (resolver.root / "workers-0.json").read_text()
    assert resolver.resolve("workers-0") == first


def test_missing_expired_or_shared_model_identity_is_actionable(tmp_path):
    resolver, _, model = setup(tmp_path)
    resolver.config["model_auth_files"]["workers-1"] = str(model)
    resolver.config["model_identities"]["workers-1"] = "model-0"
    with pytest.raises(RuntimeError, match="unique"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    resolver.config["model_auth_files"] = {}
    with pytest.raises(RuntimeError, match="owner handoff"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")


def test_wrong_existing_secret_is_never_relabelled(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    clients[0].values[ref] = {"arn": ref, "client_token": "other", "tags": {"ManagedBy": "other"}}
    with pytest.raises(RuntimeError, match="unrelated"):
        resolver.ensure("workers-0", ref)


def test_direct_resolver_rejects_pool_different_from_admitted_declaration(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    resolver.pool = replace(resolver.pool, owner="different")
    with pytest.raises(RuntimeError, match="admitted scale operation"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    assert all(not getattr(client, "rows", {}) for client in clients[1:])


def test_repository_name_and_operation_tag_are_validated_before_their_mutation(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    resolver.config["github_repo"] = "owner/repo/extra"
    with pytest.raises(RuntimeError, match="OWNER/REPO"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    assert clients[2].rows == {}

    resolver, clients, _ = setup(tmp_path / "second")
    resolver.config["secret_tags"]["OperationId"] = "renew-six-stale"
    with pytest.raises(RuntimeError, match="durable authority"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    assert clients[0].values == {}


def test_model_expiry_must_cover_operation_deadline(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    resolver.config["model_expires_at"] = {"workers-0": "2029-12-31T00:00:00+00:00"}
    with pytest.raises(RuntimeError, match="operation deadline"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")
    assert clients[0].values == {}


def test_tailnet_response_must_prove_exact_scope_and_expiry(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    clients[1].create_key = lambda **kwargs: {
        "id": "unsafe",
        "key": "wrong-prefix",
        "description": kwargs["description"],
        "expires": "2030-01-01T00:00:00+00:00",
        "capabilities": {"devices": {"create": {
            "reusable": False, "ephemeral": False,
            "preauthorized": True, "tags": ["tag:garden-worker"],
        }}},
    }
    with pytest.raises(RuntimeError, match="outside the admitted scope"):
        resolver.ensure("workers-0", "context-garden/phase05/renew-workers-0")


def test_partial_revoke_reports_only_pending_and_retains_model_input(tmp_path):
    resolver, clients, model = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    clients[0].fail_delete = True
    pending = resolver.revoke("workers-0")
    assert pending == (clients[0].values[ref]["arn"],) and model.exists()
    assert clients[1].deleted == ["1"] and clients[2].deleted == ["1"]
    assert json.loads((resolver.root / "controller-hosts.json").read_text())["hosts"] == []


def test_lost_secret_create_response_resumes_with_same_owned_identity(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    clients[0].fail_after_create = True
    with pytest.raises(RuntimeError, match="lost response"):
        resolver.ensure("workers-0", ref)
    enrollment = resolver.ensure("workers-0", ref)
    assert enrollment.secret_ref == ref and len(clients[0].values) == 1


def test_lost_create_then_model_change_never_reuses_token_for_new_payload(tmp_path):
    resolver, clients, model = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    clients[0].fail_after_create = True
    with pytest.raises(RuntimeError, match="lost response"):
        resolver.ensure("workers-0", ref)
    model.write_text('{"tokens":{"access_token":"new","refresh_token":"new-r"}}')
    with pytest.raises(RuntimeError, match="prior model input"):
        resolver.ensure("workers-0", ref)
    enrollment = resolver.ensure("workers-0", ref)
    state = json.loads((resolver.root / ".workers-0.private.json").read_text())
    assert enrollment.secret_ref == ref
    assert len(clients[0].values) == len(clients[1].rows) == len(clients[2].rows) == 1
    assert len(clients[0].puts) == 1
    assert clients[0].envelopes[-1]["codex_auth"]["tokens"]["access_token"] == "new"
    assert state["steps"]["bootstrap"]["envelope_sha256"] == hashlib.sha256(
        json.dumps(clients[0].envelopes[-1], sort_keys=True).encode()
    ).hexdigest()


def test_intents_survive_lost_repository_and_tailnet_responses(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    clients[2].fail_after_create = True
    with pytest.raises(RuntimeError, match="lost response"):
        resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    assert state["steps"]["repository_intent"]["private_key"] == "PRIVATE"
    clients[1].fail_after_create = True
    with pytest.raises(RuntimeError, match="lost response"):
        resolver.ensure("workers-0", ref)
    enrollment = resolver.ensure("workers-0", ref)
    assert enrollment.tailnet_identity == "tailscale-auth-key:2"
    assert clients[1].deleted == ["1"]


def test_bootstrap_contains_exact_admitted_host_identity_and_renewal_values(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    envelope = clients[0].envelopes[0]
    expected = hashlib.sha256(("garden.hosts/v1\0owner\0workers\0" + "0").encode()).hexdigest()[:32]
    assert envelope["operation_id"] == expected
    assert envelope["source_head"] == "b" * 40
    assert envelope["bootstrap_sha256"] == "c" * 64
    assert envelope["deadline_utc"] == "2030-01-01T00:00:00+00:00"
    assert (envelope["cpu"], envelope["memory_mib"], envelope["disk_gib"]) == (4, 1024, 20)
    registry = json.loads((resolver.root / "controller-hosts.json").read_text())["hosts"][0]
    assert registry["operation_id"] == expected
    assert registry["max_parallel"] == 1
    assert registry["deadline_utc"] == "2030-01-01T00:00:00+00:00"
    private = json.loads((resolver.root / ".workers-0.private.json").read_text())
    assert private["steps"]["repository"]["private_key"] == "PRIVATE"
    assert private["steps"]["tailnet"]["key"] == "tskey-auth-private-1"
    assert private["steps"]["controller"]["token"]


def test_per_host_lock_prevents_duplicate_side_effects(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resolver.ensure("workers-0", ref), range(2)))
    assert results[0] == results[1]
    assert len(clients[0].values) == len(clients[1].rows) == len(clients[2].rows) == 1


def test_expired_completed_enrollment_refreshes_owned_secret_and_tailnet(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    state["steps"]["tailnet"]["expires_at"] = "2028-01-01T00:00:00+00:00"
    private.write_text(json.dumps(state))
    original = resolver.resolve("workers-0")
    refreshed = resolver.ensure("workers-0", ref)
    assert refreshed.repository_identity == original.repository_identity
    assert refreshed.controller_identity == original.controller_identity
    assert refreshed.tailnet_identity == "tailscale-auth-key:2"
    assert len(clients[0].puts) == 1
    assert clients[1].deleted == ["1"]


def test_lost_bootstrap_refresh_response_reconciles_awscurrent(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    state["steps"]["tailnet"]["expires_at"] = "2028-01-01T00:00:00+00:00"
    private.write_text(json.dumps(state))
    clients[0].fail_after_put = True
    with pytest.raises(RuntimeError, match="lost put response"):
        resolver.ensure("workers-0", ref)
    refreshed = resolver.ensure("workers-0", ref)
    assert refreshed.secret_ref == ref
    assert len(clients[0].puts) == 1


def test_lost_put_then_model_change_uses_distinct_version_tokens(tmp_path):
    resolver, clients, model = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    model.write_text('{"tokens":{"access_token":"middle","refresh_token":"middle-r"}}')
    clients[0].fail_after_put = True
    with pytest.raises(RuntimeError, match="lost put response"):
        resolver.ensure("workers-0", ref)
    model.write_text('{"tokens":{"access_token":"latest","refresh_token":"latest-r"}}')
    with pytest.raises(RuntimeError, match="prior model input"):
        resolver.ensure("workers-0", ref)
    resolver.ensure("workers-0", ref)
    assert len(clients[0].puts) == 2
    assert len(set(clients[0].puts)) == 2
    assert clients[0].envelopes[-1]["codex_auth"]["tokens"]["access_token"] == "latest"
    assert len(clients[0].values) == len(clients[1].rows) == len(clients[2].rows) == 1


def test_renewed_model_file_refreshes_same_operation_envelope(tmp_path):
    resolver, clients, model = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.config["model_expires_at"] = {"workers-0": "2031-01-01T00:00:00+00:00"}
    resolver.ensure("workers-0", ref)
    model.write_text('{"tokens":{"access_token":"new-a","refresh_token":"new-r"}}')
    refreshed = resolver.ensure("workers-0", ref)
    assert refreshed.model_expires_at == "2031-01-01T00:00:00+00:00"
    assert len(clients[0].puts) == 1
    assert clients[0].envelopes[-1]["codex_auth"]["tokens"]["access_token"] == "new-a"


def test_corrupted_public_metadata_recovers_checkpoint_without_put(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    public = resolver.root / "workers-0.json"
    enrollment = json.loads(public.read_text())
    enrollment["model_expires_at"] = "corrupted"
    public.write_text(json.dumps(enrollment))
    recovered = resolver.ensure("workers-0", ref)
    assert recovered.model_expires_at == ""
    assert clients[0].puts == []


def test_refresh_without_put_secret_value_capability_is_actionable(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    state["steps"]["tailnet"]["expires_at"] = "2028-01-01T00:00:00+00:00"
    private.write_text(json.dumps(state))
    clients[0].put = None
    with pytest.raises(RuntimeError, match="PutSecretValue"):
        resolver.ensure("workers-0", ref)


def test_refresh_resumes_after_next_tailnet_attempt_was_journaled(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    old = state["steps"].pop("tailnet")
    clients[1].delete_key(old["id"])
    state["steps"]["tailnet_attempt"] = {
        "generation": 2,
        "description": "Garden scale-id workers-0 attempt 2",
        "tag": "tag:garden-worker",
        "reusable": False,
    }
    private.write_text(json.dumps(state))
    refreshed = resolver.ensure("workers-0", ref)
    assert refreshed.tailnet_identity == "tailscale-auth-key:2"
    assert clients[0].envelopes[-1]["tailscale_auth_key"] == "tskey-auth-private-2"


def test_confirmed_refresh_recovers_publication_without_another_put(tmp_path, monkeypatch):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    resolver.ensure("workers-0", ref)
    private = resolver.root / ".workers-0.private.json"
    state = json.loads(private.read_text())
    state["steps"]["tailnet"]["expires_at"] = "2028-01-01T00:00:00+00:00"
    private.write_text(json.dumps(state))
    original_atomic = enrollment_module._atomic

    def fail_publication(path, value, mode=0o600):
        if path.name == "workers-0.json":
            raise RuntimeError("publication interrupted")
        return original_atomic(path, value, mode)

    monkeypatch.setattr(enrollment_module, "_atomic", fail_publication)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        resolver.ensure("workers-0", ref)
    monkeypatch.setattr(enrollment_module, "_atomic", original_atomic)
    recovered = resolver.ensure("workers-0", ref)
    assert recovered.tailnet_identity == "tailscale-auth-key:2"
    assert clients[0].envelopes[-1]["tailscale_auth_key"] == "tskey-auth-private-2"
    assert len(clients[0].puts) == 1


def test_revoke_reconciles_lost_repository_attempt(tmp_path):
    resolver, clients, _ = setup(tmp_path)
    ref = "context-garden/phase05/renew-workers-0"
    clients[2].fail_after_create = True
    with pytest.raises(RuntimeError, match="lost response"):
        resolver.ensure("workers-0", ref)
    assert resolver.revoke("workers-0") == ()
    assert clients[2].rows == {}


def test_boto_shape_selects_current_version_and_rejects_wrong_account():
    client = BotoSecretsManagerClient.__new__(BotoSecretsManagerClient)
    client.region, client.account = "us-east-1", "350111791226"
    row = {
        "ARN": "arn:aws:secretsmanager:us-east-1:350111791226:secret:context-garden/phase05/renew-one-ABC123",
        "Tags": [{"Key": "ManagedBy", "Value": "context-garden"}],
        "VersionIdsToStages": {"old": ["AWSPREVIOUS"], "token": ["AWSCURRENT"]},
    }
    assert client._row(row)["client_token"] == "token"
    row["ARN"] = row["ARN"].replace("350111791226", "999999999999")
    with pytest.raises(RuntimeError, match="account/region/prefix"):
        client._row(row)


def test_http_clients_accept_comment_difference_and_idempotent_delete(tmp_path):
    github_token = tmp_path / "github-token"
    github_token.write_text("token")
    github_token.chmod(0o600)

    def github_handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[{"id": 7, "title": "worker", "key": "ssh-ed25519 AAAA remote"}],
            )
        return httpx.Response(404)

    github = GitHubDeployKeyClient(
        github_token, client=httpx.Client(transport=httpx.MockTransport(github_handler))
    )
    assert github.find_deploy_key("o/r", "worker", "ssh-ed25519 AAAA local")["id"] == 7
    github.delete_deploy_key("o/r", "7")

    oauth = tmp_path / "tailscale.json"
    oauth.write_text(json.dumps({"client_id": "id", "client_secret": "secret"}))
    oauth.chmod(0o600)

    def tailscale_handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "access"})
        return httpx.Response(404)

    tailscale = TailscaleOAuthClient(
        oauth, client=httpx.Client(transport=httpx.MockTransport(tailscale_handler))
    )
    tailscale.delete_key("already-gone")
