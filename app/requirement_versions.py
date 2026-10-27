# -*- coding: utf-8 -*-
"""Immutable, canonical versions of the user's business demand."""
from __future__ import print_function

import hashlib
import json

from .store import now_iso


DEMAND_FIELDS = (
    "scenario", "scenario_options", "optimization_intensity", "selected_tags",
    "max_price", "min_capability", "indicator_filter_mode", "indicator_filters",
    "target_protocol",
)


def canonical_demand(request):
    source = request or {}
    demand = dict((key, source.get(key)) for key in DEMAND_FIELDS)
    demand["scenario"] = demand.get("scenario") or "balanced"
    demand["scenario_options"] = demand.get("scenario_options") or {}
    demand["selected_tags"] = sorted(set(demand.get("selected_tags") or []))
    demand["indicator_filter_mode"] = demand.get("indicator_filter_mode") or "all"
    filters = []
    for raw in demand.get("indicator_filters") or []:
        item = dict((key, raw.get(key)) for key in
                    ("parameter_id", "operator", "value1", "value2"))
        if item.get("parameter_id") and item.get("operator"):
            filters.append(item)
    demand["indicator_filters"] = filters
    return demand


def demand_fingerprint(request):
    raw = json.dumps(canonical_demand(request), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def demand_changes(before, after):
    before, after = canonical_demand(before), canonical_demand(after)
    return [
        {"field": key, "before": before.get(key), "after": after.get(key)}
        for key in DEMAND_FIELDS if before.get(key) != after.get(key)
    ]


class RequirementVersionService(object):
    def __init__(self, store):
        self.store = store

    def capture(self, request, created_by="system", parent_version_id=None,
                change_source="recommend", force=False):
        demand = canonical_demand(request)
        fingerprint = demand_fingerprint(demand)
        product_code = self.store.current_product_code()
        with self.store.lock:
            conn = self.store.connect()
            try:
                previous = conn.execute(
                    "SELECT * FROM requirement_versions WHERE product_code=? "
                    "ORDER BY version_no DESC LIMIT 1", (product_code,)
                ).fetchone()
                if previous and previous["demand_fingerprint"] == fingerprint and not force:
                    return self._public(dict(previous))
                previous_demand = json.loads(previous["demand_json"]) if previous else {}
                next_no = int(previous["version_no"] if previous else 0) + 1
                parent = parent_version_id
                if parent is None and previous:
                    parent = previous["id"]
                changes = demand_changes(previous_demand, demand)
                if change_source:
                    changes.insert(0, {"field": "_source", "after": change_source})
                cur = conn.execute(
                    "INSERT INTO requirement_versions(product_code,version_no,parent_version_id,"
                    "demand_json,demand_fingerprint,change_summary_json,target_protocol,created_at,created_by) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (product_code, next_no, parent, json.dumps(demand, ensure_ascii=False),
                     fingerprint, json.dumps(changes, ensure_ascii=False),
                     demand.get("target_protocol"), now_iso(), created_by or "system")
                )
                conn.commit()
                row = conn.execute("SELECT * FROM requirement_versions WHERE id=?", (cur.lastrowid,)).fetchone()
                return self._public(dict(row))
            finally:
                conn.close()

    def list(self, limit=50):
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM requirement_versions WHERE product_code=? "
                "ORDER BY version_no DESC LIMIT ?",
                (self.store.current_product_code(), max(1, min(int(limit), 200)))
            ).fetchall()
            return [self._public(dict(row)) for row in rows]
        finally:
            conn.close()

    def restore(self, version_id, created_by="system"):
        conn = self.store.connect()
        try:
            row = conn.execute("SELECT * FROM requirement_versions WHERE id=?", (int(version_id),)).fetchone()
            if not row:
                raise ValueError("需求版本不存在。")
            demand = json.loads(row["demand_json"])
        finally:
            conn.close()
        restored = self.capture(demand, created_by=created_by,
                                parent_version_id=int(version_id), change_source="restore", force=True)
        restored["restored_from_version_id"] = int(version_id)
        return restored

    @staticmethod
    def _public(row):
        item = dict(row)
        item["demand"] = json.loads(item.pop("demand_json") or "{}")
        item["changes"] = json.loads(item.pop("change_summary_json") or "[]")
        return item
