"""Disposable HTTP benchmark with three executing local load workers, no model calls.
Run in a systemd unit with CPUQuota=200%, MemoryHigh=3G, MemoryMax=4G,
MemorySwapMax=512M. Uses only an operator-test-tmp fixture, never live state.
"""
import argparse, datetime as dt, json, math, multiprocessing as mp, os, pathlib, time, urllib.request
import uvicorn, yaml
from garden.runs import Run, RunStore
from garden.store import Store
from garden.web.app import create_app

def worker(root, n, stop):
    p=pathlib.Path(root)/'.garden/runs'/f'LIVE-{n}'/'benchmark-work'
    r=Run(task_id=f'LIVE-{n}',run_id=p.name,dir=str(p),runner='local',mode='work',pid=os.getpid(),started_at=dt.datetime.now(dt.UTC).isoformat(),status='running')
    memory=bytearray(16*1024*1024); count=0
    while not stop.is_set():
        for i in range(40000): count=(count+i*i)%10000019
        if time.monotonic()%1 < .1:
            r.cost_usd=count/1000000000;r.save()
            (p/'stdout.json').write_text(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':f'Local benchmark worker computing batch {count}'}})+'\n')
        memory[count%len(memory)]=count%256
    r.status='done';r.finished_at=dt.datetime.now(dt.UTC).isoformat();r.save()

def serve(root, port):
    app=create_app(Store(pathlib.Path(root)),watch=False)
    @app.get('/benchmark-stats')
    def stats():
        rs=RunStore(pathlib.Path(root)/'.garden');return {'scans':rs.scan_count,'reads':rs.read_count}
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--history',type=int,required=True);ap.add_argument('--port',type=int,default=8783);ap.add_argument('--hold',type=int,default=0);a=ap.parse_args()
    root=pathlib.Path('/home/joshua/work/operator-test-tmp')/f'cg357-served-{a.history}-{os.getpid()}';root.mkdir()
    (root/'garden.yaml').write_text(yaml.safe_dump({'name':'Incident recovery benchmark','max_parallel':3,'review_parallel':1,'products':{'demo':{'repo':str(root),'base_branch':'main'}}}))
    phase=root/'demo/phase-05';(phase/'tasks').mkdir(parents=True);(root/'demo/product.md').write_text('# Benchmark project\n');(phase/'goals.md').write_text('# Responsive operation\n')
    for tid in [f'DM-{i}' for i in range(100)]+[f'LIVE-{i}' for i in range(3)]:
        data={'id':tid,'title':f'Benchmark {tid}','status':'running' if tid.startswith('LIVE') else 'done','product':'demo','phase':'phase-05','priority':1,'difficulty':'easy'}
        (phase/'tasks'/f'{tid}.md').write_text('---\n'+yaml.safe_dump(data)+'---\n## Goal\nExercise served pages.\n')
    for n in range(a.history):
        p=root/'.garden/runs'/f'DM-{n%100}'/f'history-{n}'
        Run(task_id=f'DM-{n%100}',run_id=p.name,dir=str(p),runner='local',started_at='2026-01-01T00:00:00+00:00',finished_at='2026-01-01T00:01:00+00:00',status='done',cost_usd=.01).save()
    stop=mp.Event();workers=[mp.Process(target=worker,args=(str(root),n,stop)) for n in range(3)]
    for p in workers:p.start()
    server=mp.Process(target=serve,args=(str(root),a.port));server.start();base=f'http://127.0.0.1:{a.port}'
    def get(path):
        with urllib.request.urlopen(base+path,timeout=30) as r:return r.status,r.read()
    try:
        for _ in range(100):
            try:get('/now2/time');break
            except Exception:time.sleep(.2)
        before=json.loads(get('/benchmark-stats')[1]);samples=[];by_route={}
        routes=['/','/board','/partials/board','/now2','/now2/period']
        for interval in range(3):
            for route in routes*4:
                t=time.perf_counter();status,_=get(route);elapsed=time.perf_counter()-t;assert status==200
                samples.append(elapsed);by_route.setdefault(route,[]).append(elapsed)
            time.sleep(1.1)
        result={'history':a.history,'workers':[p.pid for p in workers],'workers_alive':all(p.is_alive() for p in workers),'samples':len(samples),'p95':sorted(samples)[math.ceil(.95*len(samples))-1],'max':max(samples),'before':before,'after':json.loads(get('/benchmark-stats')[1]),'routes':{k:{'max':max(v),'samples':len(v)} for k,v in by_route.items()},'fixture':str(root),'limitation':'Three executing local CPU/metadata workload processes, not model harness sessions; HTTP over TCP, no TestClient.'}
        pathlib.Path(__file__).with_name(f'result-{a.history}.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
        if a.hold:time.sleep(a.hold)
    finally:
        stop.set()
        for p in workers:p.join(5)
        for p in workers:
            if p.is_alive():p.terminate();p.join()
        server.terminate();server.join(5)
if __name__=='__main__':main()
