# -*- coding: utf-8 -*-
"""Same-origin routes for the read-only cost-effectiveness page."""
from __future__ import print_function

from pathlib import Path


PREFIX = "/cost-effectiveness"
_STATIC = {"index.html": "text/html; charset=utf-8",
           "app.js": "application/javascript; charset=utf-8",
           "styles.css": "text/css; charset=utf-8"}


def mount_config(model_config, price_output, database_path):
    """Use the recommendation system's price unit, not a second config file."""
    output = {"unit": price_output["unit"], "scale": price_output["scale"]}
    return {
        "host": "127.0.0.1",
        "port": 0,
        "database": {"path": str(database_path)},
        "services": {
            "price": {"url": str(model_config["price_service_url"]).rstrip("/")},
            "effectiveness": {"url": str(model_config["effectiveness_service_url"]).rstrip("/")},
        },
        "price_output": output,
        "timeout_seconds": float(model_config["timeout_seconds"]),
        "recommendation_url": "/portal",
    }


def classify(method, path):
    """Return a small route description, or None when this page does not own the path."""
    if path == PREFIX:
        return ("redirect", PREFIX + "/")
    if path == PREFIX + "/":
        return ("static", "index.html")
    if not path.startswith(PREFIX + "/"):
        return None
    rest = path[len(PREFIX) + 1:]
    if rest in _STATIC:
        return ("static", rest)
    if rest == "health":
        return ("health",)
    if rest == "api/schemes" and method == "GET":
        return ("schemes",)
    if rest.startswith("api/schemes/") and method == "GET":
        scheme_id = rest[len("api/schemes/"):]
        if scheme_id and "/" not in scheme_id:
            return ("scheme", scheme_id)
        return ("missing",)
    if rest == "api/analyze" and method == "POST":
        return ("analyze",)
    return ("missing",)


def read_static(name):
    if name not in _STATIC:
        raise KeyError(name)
    relative = "cost_effectiveness_analysis/static/" + name
    try:
        import sys
        if getattr(sys, "frozen", False):
            from product.resources import read_asset
            return read_asset(relative)
    except Exception:
        pass
    path = Path(__file__).resolve().parent / "static" / name
    if not path.is_file():
        raise KeyError(name)
    return path.read_bytes()


def content_type(name):
    return _STATIC[name]
