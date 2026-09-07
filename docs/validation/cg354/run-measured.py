from pathlib import Path
import ast, datetime, hashlib, json, os, subprocess, tarfile
root=Path("/home/joshua/work/operator-test-tmp/cg354-benchmark-20260907")
root.mkdir(exist_ok=False)
work=Path("/home/joshua/work/worktrees/CG-354")
base="58e13b99ddaf62b751b01771e5660039a421d46c"
after="1f3cbb29270119fc84630e8f8abf00ce4077409b"
def git(*args):
    return subprocess.run(["git","-C",str(work),*args],capture_output=True,text=True,check=True).stdout.strip()
assert git("rev-parse","HEAD")==after
assert git("rev-parse","4cfcc8c^")==base
assert not git("status","--porcelain")
archive=root/"baseline.tar"
subprocess.run(["git","-C",str(work),"archive","--format=tar","--output",str(archive),base],check=True)
baseline=root/"baseline";baseline.mkdir()
with tarfile.open(archive) as tf:tf.extractall(baseline,filter="data")
cgrel=next(l.split(":",2)[2] for l in Path("/proc/self/cgroup").read_text().splitlines() if l.startswith("0::"))
cg=Path("/sys/fs/cgroup")/cgrel.lstrip("/")
caps={n:(cg/n).read_text().strip() for n in ["cpu.max","memory.high","memory.max","memory.swap.max","pids.max"]}
assert caps["cpu.max"]=="200000 100000",caps
assert caps["memory.max"]==str(1024**3),caps
results=[]
for label,cwd,suite,rev in [("before",baseline,"tests/test_retro.py",base),("after",work,"tests/test_retro_documents.py",after)]:
    result=root/label;result.mkdir();aux=result/"aux-tmp";aux.mkdir()
    env=os.environ.copy();env.update(PYTHONPATH=str(cwd/"src"),PYTHONDONTWRITEBYTECODE="1",TMPDIR=str(aux))
    env["PATH"]="/home/joshua/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    command=["/usr/bin/time","-f","elapsed=%e\npeak_rss_kib=%M","-o",str(result/"time.txt"),"/home/joshua/garden/.venv/bin/python","-m","pytest",suite,"-q","--basetemp="+str(result/"pytest")]
    with (result/"pytest.log").open("w") as log:
        proc=subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=120)
    timing=dict(l.split("=",1) for l in (result/"time.txt").read_text().splitlines() if "=" in l)
    sizes={n:int(subprocess.run(["du","-sk",str(result/n)],capture_output=True,text=True,check=True).stdout.split()[0]) for n in ["pytest","aux-tmp"]}
    row={"selection":label,"revision":rev,"cwd":str(cwd),"suite":suite,"command":command,"env":{"PYTHONPATH":env["PYTHONPATH"],"TMPDIR":str(aux),"PYTHONDONTWRITEBYTECODE":"1"},"exit_code":proc.returncode,"timing":timing,"temp_kib":sizes,"pytest_summary":(result/"pytest.log").read_text().strip().splitlines()[-1],"test_source_sha256":hashlib.sha256((cwd/suite).read_bytes()).hexdigest()}
    results.append(row);print(json.dumps(row),flush=True)
    if proc.returncode:raise RuntimeError(row)
report={"at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"caps":caps,"measurement_scope":"serial focused selections; per-process max RSS and exact pytest basetemp allocation; archive/log/auxiliary temp excluded from pytest temp figure", "results":results,"unit_memory_peak_bytes":int((cg/"memory.peak").read_text()),"unit_memory_events":(cg/"memory.events").read_text(),"unit_memory_swap_bytes":int((cg/"memory.swap.current").read_text())}
(root/"report.json").write_text(json.dumps(report,indent=2)+"\n")
print("REPORT",root/"report.json",flush=True)
