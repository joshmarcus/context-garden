#!/usr/bin/env python3
"""Token-free tmux stand-in: detached sessions with retained terminal metadata."""

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["TMUX_TMPDIR"])
args = sys.argv[1:]
# Emulate a host with base-index 1: selecting its first window as :0 is invalid.
if "-t" in args and args[args.index("-t") + 1].endswith(":0"):
    sys.stderr.write("no such window: 0\n")
    sys.exit(1)
if args[0] == "--run":
    session, command = args[1:]
    with (root / (session + ".log")).open("w") as log:
        child = subprocess.Popen(["sh", "-c", command], stdout=log, stderr=log)
        (root / session).write_text(json.dumps({"pid": child.pid}))
        child.wait()
        (root / (session + ".dead")).touch()
elif args[0] == "new-session":
    session = args[args.index("-s") + 1]
    command = args[args.index("-s") + 2]
    subprocess.Popen([sys.executable, __file__, "--run", session, command],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
elif args[0] in {"has-session", "display-message"}:
    session = args[args.index("-t") + 1].lstrip("=").split(":")[0]
    if args[0] == "display-message":
        print("1" if (root / (session + ".dead")).exists() else "0")
    sys.exit(0 if (root / session).exists() else 1)
else:
    sys.exit(2)
