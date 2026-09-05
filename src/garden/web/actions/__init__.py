"""The actions (POST routes). Task actions are a registry: one function per action,
registered by name with `@action("...")` in `tasks.py`, and `POST /tasks/{id}/{action}` is
a table lookup. Adding an action means adding one function, not a branch in a chain.
The other action modules (control, phases, decisions, friction) register plain routes."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI

from ...model import Task
from ...scheduler import Scheduler
from ...store import Store
from ..common import Site

# (store, scheduler, task, note, applies_to) -> an optional warning to flash after a
# successful action; raise RuntimeError for a message the person should see instead as a
# flash on failure, HTTPException for a bad request.
TaskAction = Callable[[Store, Scheduler, Task, str, str], "str | None"]

ACTIONS: dict[str, TaskAction] = {}


def action(name: str) -> Callable[[TaskAction], TaskAction]:
    """Register a task action under the name it is posted to (`POST /tasks/{id}/<name>`)."""

    def register_action(fn: TaskAction) -> TaskAction:
        if name in ACTIONS:
            raise ValueError(f"web action {name!r} is registered twice")
        ACTIONS[name] = fn
        return fn

    return register_action


def register(app: FastAPI, site: Site) -> None:
    from . import control, decisions, friction, phases, tasks

    for module in (control, tasks, decisions, phases, friction):
        module.register(app, site)
