from __future__ import annotations

import asyncio
import datetime as dt
import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from garden.cli import app as cli
from garden.events import EventLog
from garden.now2 import next_queues, snapshot
from garden.runs import RunStore
from garden.scheduler import Scheduler
from garden.scheduler.selection import worker_candidates
from garden.store import Store
from garden.web.app import create_app
from tests.test_now2_metrics import NOW


def test_now2_page_all_runs_attention_clock_and_read_only(garden):
    s = Store(garden)
    run = RunStore(s.config.garden_dir).new_run("D-1", "local", run_id="clock-contract")
    run.started_at, run.harness, run.model = NOW.isoformat(), "fake", "test-model"
    run.save()
    (run.path / "stdout.json").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "<script>escaped</script>"}}))
    review = RunStore(s.config.garden_dir).new_run("D-1", "local", mode="review", run_id="review-contract")
    app = create_app(s)
    before = {p: p.read_bytes() for p in s.config.garden_dir.rglob('*') if p.is_file()}
    with app.state.hub.lock, TestClient(app) as client:
        response = client.get('/now2')
        assert response.status_code == 200
        assert f'data-started-at="{NOW.isoformat()}"' in response.text
        assert 'clock-contract' in response.text and review.run_id in response.text
        assert '&lt;script&gt;escaped&lt;/script&gt;' in response.text
        for title in ('Next', 'Where we are', 'The last period', 'n2-metric', 'n2-motion'):
            assert title in response.text
    assert all(p.read_bytes() == value for p, value in before.items())


def test_sse_event_produces_changed_dom_fragment_without_hub_lock(garden):
    s = Store(garden)
    app = create_app(s)
    endpoint = next(r.endpoint for r in app.routes if getattr(r, 'path', '') == '/api/events/now2')

    class Request:
        async def is_disconnected(self):
            return False

    async def drive():
        response = endpoint(Request(), 'hour', '')
        it = response.body_iterator
        first = await anext(it)
        assert 'event: snapshot' in first
        run = RunStore(s.config.garden_dir).new_run('D-1', 'local', run_id='arriving')
        run.started_at = dt.datetime.now(dt.UTC).isoformat()
        run.save()
        EventLog(s.config.garden_dir / 'events.jsonl').emit('dispatch', 'D-1', run=run.run_id, mode='work')
        message = await anext(it)
        payload = json.loads(message.split('data: ', 1)[1])
        assert 'data-run-id="arriving"' in payload['fragments']['run-D-1-arriving']
        assert 'now2-where' not in payload['fragments']
        await it.aclose()
    with app.state.hub.lock:
        asyncio.run(drive())


def test_queue_uses_dispatch_selection_and_cli_same_regions(garden, monkeypatch):
    s = Store(garden)
    sched = Scheduler(s)
    selected = worker_candidates(s.tasks(), sched.state, 2, sched.stack_enabled, sched._edit_pending)
    q = next_queues(s, sched)
    assert [r['task'].id for r in q['workers']] == [t.id for t, mode in selected]
    monkeypatch.chdir(garden)
    result = CliRunner().invoke(cli, ['now', '--page', '2'])
    assert result.exit_code == 0, result.output
    for title in ('Now', 'Next', 'Where we are', 'The last period'):
        assert title in result.output
    assert snapshot(s)['period']['matrices']['accepted_count'] == 0


def test_walkthrough_includes_now2(garden):
    from garden.walkthrough import pages_for
    s = Store(garden)
    ph = s.products()[0].phases[0]
    assert any(p.url == '/now2' for p in pages_for(s, ph))


def test_metrics_cli_renders_windowed_and_established_tables(garden, monkeypatch):
    monkeypatch.chdir(garden)
    result = CliRunner().invoke(cli, ['metrics', '--since', '1h'])
    assert result.exit_code == 0, result.output
    output = ' '.join(result.output.split())
    assert 'Total cost / accepted task' in output
    assert 'Median lead time' in output
    assert 'rebases (their own mode' in output


def test_next_preserves_revision_priority_and_merge_head_state(garden):
    from garden.model import Status

    s = Store(garden)
    sched = Scheduler(s)
    first, second = s.task('DM-001'), s.task('DM-002')
    first.status = second.status = Status.CHANGES_REQUESTED
    sched.state.get(first.id).update(pending_feedback='Fix verification', revisions=0,
                                    automerge_candidate=True, automerge_ready_at='2026-09-01')
    sched.state.get(second.id).update(rebase_pending=True, merge_head=True,
                                     automerge_candidate=True, automerge_ready_at='2026-09-02',
                                     checks='pending', review_rounds=2)
    queues = next_queues(s, sched)
    assert [(r['task'].id, r['mode']) for r in queues['workers']] == [
        (second.id, 'rebase'), (first.id, 'revise')]
    assert [r['task'].id for r in queues['merges']] == [second.id, first.id]
    assert queues['merges'][0]['checks'] == 'pending'
    assert queues['merges'][0]['round'] == 2
    assert queues['merges'][0]['reason'] == 'Queue head; awaiting rollup'
    assert all(r['reason'] for r in queues['workers'])


def test_fake_harness_live_output_and_run_record_join(garden):
    from garden.events import with_run_records
    from garden.now2 import running_rows
    from tests.fake_codex import run as fake_codex

    s = Store(garden)
    run = RunStore(s.config.garden_dir).new_run('DM-001', 'local')
    run.harness, run.model, run.difficulty = 'codex', 'gpt-5.6-luna', 'easy'
    run.started_at = NOW.isoformat()
    # The suite's real fake harness generates the usage/output consumed by Now.
    stdout, _, _ = fake_codex(['-m', run.model], 'GARDEN_REVIEW: review this fixture', run.path, {})
    (run.path / 'stdout.json').write_text(stdout)
    row = running_rows([run], s.tasks(), NOW, s)[0]
    assert row['spend'] is not None and row['spend'] >= 0
    assert row['freshness'] == 'latest reported usage'
    run.finished_at, run.status, run.cost_usd = (NOW+dt.timedelta(seconds=180)).isoformat(), 'done', 2
    run.result = {'verdict': 'approve'}
    assert running_rows([run], s.tasks(), NOW+dt.timedelta(seconds=184), s)[0]['verdict'] == 'approve'
    assert running_rows([run], s.tasks(), NOW+dt.timedelta(seconds=189), s) == []
    events = with_run_records([], [run])
    assert {e['kind'] for e in events} == {'dispatch', 'run_finished'}
    assert events[-1]['model'] == run.model
    assert len(with_run_records(events + events, [run])) == 2


def test_phase_specimens_goals_attention_and_held_reason(garden):
    from garden.model import Status
    from garden.now2 import phase_rows

    s = Store(garden)
    sched = Scheduler(s)
    task = s.task('DM-001')
    # Fixture facts only: production status writes remain scheduler-owned.
    task.status = Status.DONE
    ph = s.products()[0].phases[0]
    ph.goals_path.write_text('# Phase\n\n## Goals\n\n1. Make adoption easy.\n2. Measure cost.\n')
    p = phase_rows(s, sched)[0]
    assert p['stage'] == 'bud' and p['done'] == 1 and p['total'] == 2
    assert p['goals'] == ['Make adoption easy.', 'Measure cost.']
    ph.meta['closed'] = '2026-09-06'
    s.task('DM-002').status = Status.CANCELLED
    assert phase_rows(s, sched)[0]['stage'] == 'fruit'
    ph.tasks = []
    assert phase_rows(s, sched)[0]['stage'] == 'seed'
    sched.state.get('_control')['paused_harnesses'] = {'claude': {'reason': 'Quota limit'}}
    sched.state.get('DM-002')['automerge_blocked'] = 'CI pending'
    sched.state.save()
    # Re-read fixture task state from disk for a live page snapshot.
    data = snapshot(Store(garden))
    assert any(a['title'] == 'claude paused' and a['why'] == 'Quota limit' for a in data['attention'])
    assert any('Merge held: CI pending' in a['why'] for a in data['attention'])


def test_stream_observes_transcript_and_ledger_without_domain_event(garden):
    from garden.now2_stream import versions
    from garden.operator_spend import default_path

    s = Store(garden)
    before = versions(s.root, s.config.garden_dir)
    ledger = default_path(s.root)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{}\n')
    assert versions(s.root, s.config.garden_dir) != before
    run = RunStore(s.config.garden_dir).new_run('DM-001', 'local')
    before = versions(s.root, s.config.garden_dir)
    (run.path / 'stdout.json').write_text('new output\n')
    assert versions(s.root, s.config.garden_dir) != before
