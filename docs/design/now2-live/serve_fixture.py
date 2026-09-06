"""Serve a synthetic Now 2 garden without starting a controller.
Run with PYTHONPATH=src .venv/bin/python docs/design/now2-live/serve_fixture.py.
"""
from pathlib import Path
import datetime as dt
import json
import os
import shutil

import uvicorn
import yaml

from garden.events import EventLog
from garden.runs import RunStore
from garden.store import Store
from garden.web.app import create_app

root = Path('.pytest_cache/now2-live-fixture').resolve()
if root.exists():
    shutil.rmtree(root)
root.mkdir(parents=True)
(root / 'garden.yaml').write_text(yaml.safe_dump({'name': 'Now 2 · demonstration', 'max_parallel': 4, 'review_parallel': 2, 'harness': 'codex', 'products': {'demo': {'repo': str(root), 'base_branch': 'main'}}}))
(root / 'demo').mkdir()
(root / 'demo/product.md').write_text('# Demo\nSynthetic capture fixture.\n')
phase = root / 'demo/phase-05'
(phase / 'tasks').mkdir(parents=True)
(phase / 'goals.md').write_text('---\nplant: foxglove\n---\n# Adoption\n\n## Goals\n\n1. A second team can start a garden.\n2. Compare the cost of accepted work.\n3. Let workers run on any machine.\n')
closed = root / 'demo/phase-04'
closed.mkdir()
(closed / 'goals.md').write_text('---\nplant: pea\nclosed: 2026-09-05\n---\n# Leave the loop running\n')
now = dt.datetime.now(dt.UTC)
def iso(minutes):
    return (now - dt.timedelta(minutes=minutes)).isoformat()
names = ['Onboard an existing project', 'Record accepted-task cost', 'Make remote workers claim runs', 'Guard the worker boundary', 'Keep the merge queue moving', 'Recover a paused account', 'Route work by difficulty', 'Preserve task edits', 'Prepare the phase retrospective', 'Measure work throughput', 'Make costs reproducible', 'Keep brief context complete', 'Observe the idle garden', 'Record review outcomes']
for i, title in enumerate(names):
    state = 'running' if i < 2 else 'in_review' if i == 2 else 'waiting_human' if i == 3 else 'done' if i >= 5 else 'ready'
    (phase / f'tasks/DM-{i+1}.md').write_text(yaml.safe_dump({'id': f'DM-{i+1}', 'title': title, 'status': state, 'difficulty': ['easy','medium','hard'][i%3], 'priority': i%3+1, 'depends_on': []}).join(['---\n', '---\n\n## Goal\n\nSynthetic demonstration.\n']))
s = Store(root)
rs = RunStore(s.config.garden_dir)
log = EventLog(s.config.garden_dir / 'events.jsonl')
for i in range(3):
    r = rs.new_run(f'DM-{i+1}', 'local', mode=['work','revise','review'][i], run_id=f'live-{i}')
    r.started_at, r.harness, r.model, r.difficulty = iso([25,6,1][i]), 'codex', ['luna','terra','sol'][i], ['easy','medium','easy'][i]
    r.pid, r.cost_usd = os.getpid(), [.35,1.48,.14][i]
    r.save()
    (r.path / 'stdout.json').write_text(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':['The project structure is clear. Building the first onboarding draft.','The acceptance cohort now includes supporting review costs.','Checking that a remote result follows the same review gate.'][i]}})+'\n')
for i in range(27):
    tid = f'DM-{6+i%9}'
    model = ['luna','terra','sol'][(i%9)//3]
    r = rs.new_run(tid,'local',run_id=f'history-{i}')
    r.started_at,r.finished_at,r.status,r.model,r.harness,r.difficulty,r.cost_usd=iso(50+i),iso(30+i),'done',model,'codex',['easy','medium','hard'][i%3],.5+((i%9)//3)*1.2
    r.save()
    log.emit('dispatch',tid,at=r.started_at,mode='work',run=r.run_id,model=model,harness='codex')
    log.emit('run_finished',tid,at=r.finished_at,mode='work',run=r.run_id,model=model,harness='codex',cost_usd=r.cost_usd)
    if i<9:
        log.emit('review',tid,at=iso(15-i),verdict='approve' if i%2 else 'request_changes')
        log.emit('transition',tid,at=iso(10-i),to='done',note='PR merged by the garden')
log.emit('profile_changed',at=iso(22),**{'from':'efficient','to':'fast'})
(s.config.garden_dir/'state.json').write_text(json.dumps({'DM-3':{'merge_head':True,'automerge_candidate':True,'automerge_ready_at':iso(5),'checks':'pending','review_rounds':1},'DM-4':{'question':'Which repository should be the adoption fixture?','automerge_blocked':'A decision about the target repository is needed'},'_control':{'paused_harnesses':{'claude':{'reason':'Account quota reached','at':iso(20)}}},'_retro_verdicts':{'demo/phase-04':{'verdict':'close','status':'accepted'}}}))
from garden.operator_spend import default_path
ledger=default_path(root)
ledger.parent.mkdir(parents=True)
ledger.write_text(json.dumps({'at':iso(5),'session':'demo','list_price_usd':2.25})+'\n')
uvicorn.run(create_app(s, watch=False, host='127.0.0.1', port=8769),host='0.0.0.0',port=8769,log_level='warning')
