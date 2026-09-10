"""Saved configuration edits from the Configuration page."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse

from ...config import Config
from ...configuration import CONFIG_FIELDS, audit_value
from ..common import Site, _flash_url


def _parse_value(key: str, raw: str) -> Any:
    field = CONFIG_FIELDS[key]
    if field.secret and not raw:
        raise ValueError("enter a new value; the saved secret is never shown")
    if field.value_type.startswith("optional_") and not raw.strip():
        return None
    expected = field.value_type.removeprefix("optional_")
    if expected in {"string", "string_or_list"}:
        return raw
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML value: {exc}") from None
    field.validate(value)
    return value


def _parse_collection(key: str, kind: str, keys: list[str], values: list[str]) -> Any:
    field = CONFIG_FIELDS[key]
    expected = field.value_type.removeprefix("optional_")
    if kind == "list":
        if expected not in {"list", "string_or_list", "any"}:
            raise ValueError(f"{key} does not accept a list")
        value: Any = values
    elif kind == "mapping":
        if expected not in {"mapping", "any"}:
            raise ValueError(f"{key} does not accept a mapping")
        if len(keys) != len(values):
            raise ValueError(f"{key} has an incomplete key/value row")
        value = {}
        for index, (item_key, raw_value) in enumerate(zip(keys, values, strict=True), 1):
            item_key = item_key.strip()
            if not item_key:
                raise ValueError(f"{key} row {index} needs a key")
            if item_key in value:
                raise ValueError(f"{key} has duplicate key {item_key!r}")
            try:
                value[item_key] = yaml.safe_load(raw_value)
            except yaml.YAMLError as exc:
                raise ValueError(f"{key}.{item_key} has invalid YAML: {exc}") from None
    else:
        return _parse_value(key, values[0] if values else "")
    field.validate(value)
    return value


def register(app: FastAPI, site: Site) -> None:
    hub = site.hub

    @app.post("/config/save")
    async def save_configuration(
        request: Request,
        key: str = Form(...),
        value: str = Form(""),
        product: str = Form(""),
        revision: str = Form(...),
        reset: bool = Form(False),
    ):
        back = request.headers.get("referer", "/config")
        try:
            if key not in CONFIG_FIELDS:
                raise ValueError(f"unknown editable setting {key!r}")
            form = await request.form()
            collection_kind = str(form.get("collection_kind") or "")
            collection_keys = [str(item) for item in form.getlist("collection_key")]
            collection_values = [str(item) for item in form.getlist("collection_value")]
            scope = product.strip() or None
            with hub.action_lock:
                # Read the file, not the scheduler's accepted snapshot. This permits a
                # second edit before the next tick while the revision token still rejects
                # another tab's stale form.
                current = Config.load(hub.store.root, hub.store.config.env)
                changes = {key: None if reset else _parse_collection(
                    key, collection_kind, collection_keys, collection_values,
                ) if collection_kind else _parse_value(key, value)}
                current.save_changes(
                    changes, product=scope, expected_revision=revision, reset=reset,
                )
                hub.reader().events.emit(
                    "config_saved", "", key=key,
                    value="<reset>" if reset else audit_value(key, changes[key]),
                    scope=f"project:{scope}" if scope else "global", by="web",
                )
            action = "reset to inherited value" if reset else "saved"
            return RedirectResponse(_flash_url(back, f"{key} {action}; effective on {CONFIG_FIELDS[key].apply.value.replace('_', ' ')}"), status_code=303)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return RedirectResponse(_flash_url(back, str(exc)), status_code=303)
