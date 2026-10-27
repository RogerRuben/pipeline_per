# -*- coding: utf-8 -*-
"""Narrow read-only access to persisted recommendation schemes."""
from __future__ import print_function

import json
import sqlite3
from pathlib import Path
from urllib.parse import quote


class DatabaseUnavailable(RuntimeError):
    pass


def _object(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class ReadOnlySchemeRepository(object):
    """Only exposes scheme reads; every SQLite connection is URI mode=ro."""

    def __init__(self, db_path):
        self.db_path = Path(db_path).resolve()

    def _connect(self):
        if not self.db_path.is_file():
            raise DatabaseUnavailable("暂无法读取方案数据库。")
        uri = "file:%s?mode=ro" % quote(self.db_path.as_posix(), safe="/:")
        try:
            conn = sqlite3.connect(uri, timeout=10, uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return conn
        except sqlite3.Error as exc:
            raise DatabaseUnavailable("暂无法读取方案数据库：%s" % exc)

    @staticmethod
    def _source(agreement_source):
        if agreement_source in ("historical", "imported"):
            return "historical"
        return "persisted"

    def list_schemes(self, source=None, search=None):
        source = str(source or "").strip().lower()
        needle = str(search or "").strip().lower()
        conn = self._connect()
        try:
            rows = []
            for row in conn.execute(
                "SELECT agreement_id,agreement_name,agreement_source,historical_price_wan,source_year "
                "FROM agreements WHERE enabled=1 ORDER BY agreement_name,agreement_id"
            ):
                mapped = self._source(row["agreement_source"])
                rows.append({
                    "scheme_id": "agreement:%s" % row["agreement_id"],
                    "scheme_name": row["agreement_name"], "source": mapped,
                    "source_detail": row["agreement_source"],
                    "historical_price_wan": row["historical_price_wan"],
                    "source_year": row["source_year"],
                })
            for row in conn.execute(
                "SELECT id,scheme_name,source_type,created_at FROM saved_schemes ORDER BY id DESC"
            ):
                rows.append({
                    "scheme_id": "saved:%s" % row["id"],
                    "scheme_name": row["scheme_name"], "source": "expert_saved",
                    "source_detail": row["source_type"], "historical_price_wan": None,
                    "created_at": row["created_at"],
                })
        except sqlite3.Error as exc:
            raise DatabaseUnavailable("暂无法读取方案数据库：%s" % exc)
        finally:
            conn.close()
        if source in ("historical", "expert_saved", "persisted"):
            rows = [item for item in rows if item["source"] == source]
        if needle:
            rows = [item for item in rows if needle in str(item["scheme_name"] or "").lower()
                    or needle in item["scheme_id"].lower()]
        return rows

    def get_scheme(self, scheme_id):
        scheme_id = str(scheme_id or "")
        conn = self._connect()
        try:
            if scheme_id.startswith("agreement:"):
                key = scheme_id.split(":", 1)[1]
                row = conn.execute(
                    "SELECT * FROM agreements WHERE agreement_id=? AND enabled=1", (key,)
                ).fetchone()
                if not row:
                    return None
                return {
                    "scheme_id": scheme_id, "scheme_name": row["agreement_name"],
                    "source": self._source(row["agreement_source"]),
                    "source_detail": row["agreement_source"],
                    "parameters": _object(row["params_json"]),
                    "historical_price_wan": row["historical_price_wan"],
                    "stored_capability_score": row["capability_score"],
                    "source_year": row["source_year"],
                }
            if scheme_id.startswith("saved:"):
                key = scheme_id.split(":", 1)[1]
                if not key.isdigit():
                    return None
                row = conn.execute("SELECT * FROM saved_schemes WHERE id=?", (int(key),)).fetchone()
                if not row:
                    return None
                stored = _object(row["evaluation_json"])
                return {
                    "scheme_id": scheme_id, "scheme_name": row["scheme_name"],
                    "source": "expert_saved", "source_detail": row["source_type"],
                    "parameters": _object(row["params_json"]),
                    "historical_price_wan": None,
                    "stored_capability_score": stored.get("capability_score"),
                    "created_at": row["created_at"],
                }
            return None
        except sqlite3.Error as exc:
            raise DatabaseUnavailable("暂无法读取方案数据库：%s" % exc)
        finally:
            conn.close()

    def get_scheme_parameters(self, scheme_id, model_values=False):
        scheme = self.get_scheme(scheme_id)
        if scheme is None:
            return None
        parameters = dict(scheme.get("parameters") or {})
        return self._encode_model_parameters(parameters) if model_values else parameters

    def get_sources(self):
        counts = {"historical": 0, "expert_saved": 0, "persisted": 0}
        for item in self.list_schemes():
            counts[item["source"]] = counts.get(item["source"], 0) + 1
        return counts

    def _encode_model_parameters(self, business_parameters):
        """Apply the same DataMaster mapping used by Store.runtime_parameters."""
        result = dict(business_parameters or {})
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT parameter_id,model_value_mapping_json FROM parameter_definitions WHERE enabled=1"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            key = row["parameter_id"]
            if key not in result or result[key] in (None, ""):
                continue
            mapping = _object(row["model_value_mapping_json"])
            normalized = dict((str(k).strip().lower(), v) for k, v in mapping.items())
            lookup = str(result[key]).strip().lower()
            if lookup in normalized:
                result[key] = normalized[lookup]
        return result
