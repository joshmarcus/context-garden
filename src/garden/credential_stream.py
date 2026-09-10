"""Write a launch script with one environment credential substituted on stdout.

The resulting stream goes directly to a remote shell.  The source script kept in the run
record contains only a marker, so the credential is never written to an artifact or argv.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    script, marker, environment_name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    value = os.environ.get(environment_name, "") if environment_name else ""
    sys.stdout.write(script.read_text().replace(marker, shlex.quote(value), 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
