"""The Configuration page."""

from __future__ import annotations

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...config import RESTART_KEYS, Config
from ...configuration import CONFIG_FIELDS, ConfigScope, product_configuration, revision
from ...observe import BUILTIN_PROFILES
from ...profiles import describe as describe_stop
from ...scheduler import WORKER_MODES, State
from ..common import Site


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request):
        s = hub.fresh()
        cfg = s.config
        saved_cfg = Config.load(s.root, cfg.env)
        editable_cfg = saved_cfg.editable()
        sched = hub.reader()
        observe_cfg = dict(cfg.get("observe") or {})
        effective = {
            "review_parallel": sched.review_parallel_limit(),
            "auto_dispatch": cfg.get("auto_dispatch"),
            "auto_revise": cfg.get("auto_revise"),
            "review.enabled": cfg.get("review.enabled"),
            "review.max_rounds": cfg.review_max_rounds() if cfg.review_max_rounds() is not None else "unlimited",
            "review.friction_after": cfg.review_friction_after() if cfg.review_friction_after() is not None else "disabled",
            "review.difficulty": sched.effective("review.difficulty") or "(task tier)",
            "review.ladder": ", ".join(str(x) for x in (cfg.get("review.ladder") or [])) or "(tier rule)",
            "retro.difficulty": sched.effective("retro.difficulty") or "hard",
            "github.draft_pr": cfg.get("github.draft_pr"),
            "stack": cfg.get("stack"),
            "observe.interval": observe_cfg.get("interval"),
            "observe.digest_window": observe_cfg.get("digest_window"),
            "observe.events": ", ".join(observe_cfg.get("events") or []),
            "observe.stuck_after": observe_cfg.get("stuck_after"),
            "observe.phases": observe_cfg.get("phases"),
        }
        budgets = dict(cfg.get("budgets") or {})
        for pname, pdata in (cfg.data.get("products") or {}).items():
            if isinstance(pdata, dict) and pdata.get("budget_usd"):
                budgets.setdefault(pname, pdata["budget_usd"])
        # runtime overrides from state.json (set via the phase page or `garden budget`) win
        overrides = dict(State(cfg.garden_dir / "state.json").get("_budgets"))
        budgets.update(overrides)
        profile_names = sorted(set(BUILTIN_PROFILES) | set(observe_cfg.get("profiles") or {}))
        config_hold = sched.config_hold()
        stops = sched.operating_profile_stops()
        active = sched.operating_profile_name()
        maintenance = sched.maintenance_readiness()
        stop_rows = [{"name": name, "active": name == active, "meaning": describe_stop(stop),
                     **{f: stop.get(f) for f in ("workers", "reviews", "review_difficulty", "retro_difficulty", "observe")}}
                    for name, stop in stops.items()]
        selected_product = request.query_params.get("product", "")
        products = sorted((saved_cfg.data.get("products") or {}).keys())
        if selected_product not in products:
            selected_product = ""
        project_overrides, _ = product_configuration(editable_cfg.data, selected_product) if selected_product else ({}, {})
        editor_rows = []
        for field in CONFIG_FIELDS.values():
            if ConfigScope.DERIVED in field.scopes:
                continue
            saved_provenance = editable_cfg.setting(field.key, selected_product or None)
            layered_provenance = saved_cfg.setting(field.key, selected_product or None)
            accepted_provenance = cfg.setting(field.key, selected_product or None)
            can_project = ConfigScope.PROJECT in field.scopes
            editable = not selected_product or (can_project and not layered_provenance.locked)
            saved_value = saved_provenance.value
            if field.secret:
                rendered = ""
                display = "••••••••" if saved_value not in (None, "", [], {}) else "not set"
            else:
                serialized = yaml.safe_dump(saved_value, default_flow_style=True, sort_keys=False).strip().removesuffix("...").strip()
                rendered = saved_value if isinstance(saved_value, str) else serialized
                display = serialized or "empty"
            effective_value = sched.effective(field.key, field.default, product=selected_product or None)
            effective_display = ("protected" if field.secret and effective_value not in (None, "", [], {})
                                 else yaml.safe_dump(effective_value, default_flow_style=True, sort_keys=False).strip().removesuffix("...").strip())
            scheduler_source = sched.effective_source(field.key)
            if accepted_provenance.source != "global":
                effective_source = cfg.setting_source(field.key, selected_product)
            elif scheduler_source == "override":
                effective_source = "runtime override"
            elif scheduler_source == "profile":
                effective_source = f"operating profile {sched.operating_profile_name()}"
            else:
                effective_source = cfg.setting_source(field.key)
            saved_source = editable_cfg.setting_source(field.key, selected_product or None)
            editor_rows.append({
                "field": field, "editable": editable, "rendered": rendered,
                "display": display, "effective": effective_display,
                "source": saved_source, "locked": layered_provenance.locked,
                "effective_source": effective_source,
                "masked": saved_source != effective_source,
                "reason": layered_provenance.reason,
                "policy_source": layered_provenance.policy_source,
                "overridden": field.key in project_overrides,
                "collection_kind": ("mapping" if isinstance(saved_value, dict) else
                                    "list" if isinstance(saved_value, list) else
                                    "unset" if field.value_type == "optional_string_or_list" and saved_value is None else
                                    "scalar" if field.value_type.removeprefix("optional_") in {"string_or_list", "any"}
                                    else ""),
                "collection_choice": field.value_type.removeprefix("optional_") in {"string_or_list", "any"},
                "list_items": [str(value) for value in saved_value] if isinstance(saved_value, list) else [],
                "mapping_items": [
                    {"key": str(key), "value": yaml.safe_dump(value, default_flow_style=True, sort_keys=False).strip().removesuffix("...").strip()}
                    for key, value in saved_value.items()
                ] if isinstance(saved_value, dict) else [],
                "choices": field.choices,
            })
        return templates.TemplateResponse(request, "config.html", ctx(
            request, page="config", sources=cfg.sources, effective=effective, budgets=budgets,
            budget_overrides=sorted(overrides), restart_keys=RESTART_KEYS, config_hold=config_hold,
            max_parallel_file=cfg.get("max_parallel"), max_parallel_override=sched.overrides().get("max_parallel"),
            max_parallel_value=sched.effective_max_parallel(), max_parallel_source=sched.effective_source("max_parallel"),
            worker_slot_modes=", ".join(mode for mode in ("work", "revise", "resume", "trial", "rebase")
                                        if mode in WORKER_MODES),
            observe_profile_file=observe_cfg.get("profile") or "", observe_profile_names=profile_names,
            observe_profile_override=sched.overrides().get("observe.profile"),
            observe_profile_source=sched.effective_source("observe.profile"),
            observe_profile_effective=sched.effective("observe.profile"),
            tool_build=sched.upgrade_status(),
            operating_profile_file=str(cfg.get("operating_profile") or ""),
            maintenance=maintenance,
            operating_profile_override=sched.overrides().get("operating_profile"),
            operating_profile_active=active, operating_profile_stop_names=list(stops),
            operating_profile_rows=stop_rows, editor_rows=editor_rows, configuration_products=products,
            selected_product=selected_product, config_revision=revision(saved_cfg.data),
            saved_pending=revision(saved_cfg.data) != revision(cfg.data)))
