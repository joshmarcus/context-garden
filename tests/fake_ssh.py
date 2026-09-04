#!/usr/bin/env python3
"""Stand-in for `ssh host sh -s`: runs the script from stdin locally with sh."""

import os
import subprocess
import sys

# argv: [opts...] host sh -s  -> we ignore everything and run `sh -s` locally
proc = subprocess.run(["sh", "-s"], stdin=sys.stdin, env=os.environ)
sys.exit(proc.returncode)
