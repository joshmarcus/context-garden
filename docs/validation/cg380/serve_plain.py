"""Matched uninstrumented server used to quantify CG-380 profiler overhead."""

import os
from pathlib import Path

from garden.store import Store
from garden.web.app import create_app

app = create_app(Store(Path(os.environ["CG380_GARDEN"])), watch=False, host="127.0.0.1")
