# -*- coding: utf-8 -*-
"""Backend-owned exploration profiles for candidate generation."""
from __future__ import print_function


PROFILES = {
    "quick": {
        "key": "quick", "label": "快速探索", "estimate": "预计 10～20 秒",
        "generation_budget": 160, "generation_rounds": 4,
        "time_budget_seconds": 18, "beam_width": 8, "seed_count": 8,
    },
    "standard": {
        "key": "standard", "label": "标准探索", "estimate": "预计约 30 秒",
        "generation_budget": 360, "generation_rounds": 7,
        "time_budget_seconds": 35, "beam_width": 10, "seed_count": 12,
    },
    "deep": {
        "key": "deep", "label": "深度探索", "estimate": "预计 1～2 分钟",
        "generation_budget": 900, "generation_rounds": 12,
        "time_budget_seconds": 100, "beam_width": 14, "seed_count": 16,
    },
}


def public_profiles():
    return [dict(PROFILES[key]) for key in ("quick", "standard", "deep")]


def apply_generation_profile(request):
    req = dict(request or {})
    # Canonicalization is intentionally idempotent: request_generation() and
    # GenerationTaskManager.start() both call it before computing batch identity.
    if req.get("exploration_profile_definition") and req.get("effective_exploration_profile"):
        return req
    requested = str(req.get("exploration_profile") or "standard").lower()
    if requested not in PROFILES:
        requested = "standard"
    profile = dict(PROFILES[requested])
    overridden = False
    for field in ("generation_budget", "generation_rounds", "time_budget_seconds",
                  "beam_width", "seed_count"):
        if req.get(field) not in (None, ""):
            profile[field] = int(req[field])
            overridden = True
        req[field] = profile[field]
    req["exploration_profile"] = requested
    req["effective_exploration_profile"] = "custom" if overridden else requested
    req["exploration_profile_definition"] = profile
    return req
