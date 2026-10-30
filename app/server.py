# -*- coding: utf-8 -*-
from __future__ import print_function

import base64
import hashlib
import hmac
import json
import math
import mimetypes
import os
import shutil
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .model_runtime import IntegratedModelRuntime, ModelInputError
from .model_service_client import DegradedServiceRuntime, ModelServiceGateway, ServiceBackedRuntime, build_model_request
from .recommender import agreement_matches, rank_agreements, rank_historical_products
from .store import Store
from .wide_import import WideTableParser
from .data_master import DataMasterService
from .product_releases import ProductReleaseService
from .local_generator import HistorySeededGenerator
from .generation_tasks import GenerationTaskManager
from .configuration import (_boolean, load_model_service_config, load_service_portal_config,
                            load_workbench_defaults, save_price_output_config)
from .range_diagnostics import build_range_diagnostics
from .scenario_policy import ScenarioPolicyService
from .recommendation_explanation import annotate_candidate_recommendations
from .expert_scheme import ExpertSchemeService
from .price_output import PriceOutputNormalizer
from .semantic_snapshot import SemanticSnapshotService
from .requirement_versions import RequirementVersionService, demand_fingerprint
from .generation_profiles import public_profiles
from .ranking_explanation import annotate_ranking_explanations
from .relaxation_advisor import build_relaxation_suggestions
from .deep_price.http import handle_http as handle_deep_price_http


class GeneratedSessions(object):
    """Session-scoped, multi-batch temporary store for generated schemes."""
    def __init__(self, ttl_seconds=8 * 3600):
        self.ttl = ttl_seconds
        self.lock = threading.RLock()
        self.data = {}

    def add_batch(self, session_id, items, batch_id=None, fingerprint=None):
        session_id = str(session_id)
        batch_id = str(batch_id or ("GENBATCH-" + hashlib.sha256((session_id + str(time.time())).encode("utf-8")).hexdigest()[:12].upper()))
        prepared = []
        for index, source in enumerate(items or [], 1):
            item = dict(source)
            candidate_id = str(item.get("candidate_id") or ("CAND-%03d" % index))
            item["batch_id"] = batch_id
            item["candidate_id"] = candidate_id
            item["agreement_id"] = "%s-%s" % (batch_id, candidate_id)
            prepared.append(item)
        with self.lock:
            session = self.data.setdefault(session_id, {"updated": time.time(), "latest_batch_id": None, "batches": {}})
            session["batches"][batch_id] = {"updated": time.time(), "fingerprint": fingerprint, "items": prepared}
            session["latest_batch_id"] = batch_id
            session["updated"] = time.time()
            self._cleanup()
        return batch_id, list(prepared)

    def get(self, session_id, batch_id=None, fingerprint=None):
        with self.lock:
            self._cleanup()
            session = self.data.get(str(session_id), {})
            batch_id = str(batch_id or session.get("latest_batch_id") or "")
            batch = (session.get("batches") or {}).get(batch_id, {})
            if fingerprint is not None and batch and batch.get("fingerprint") != fingerprint:
                return []
            return list(batch.get("items", []))

    def batch_metadata(self, session_id, batch_id):
        with self.lock:
            self._cleanup()
            session = self.data.get(str(session_id), {})
            batch = (session.get("batches") or {}).get(str(batch_id or ""))
            if not batch:
                return None
            return {
                "batch_id": str(batch_id),
                "fingerprint": batch.get("fingerprint"),
                "updated": batch.get("updated"),
                "count": len(batch.get("items") or []),
            }

    def batches(self, session_id):
        with self.lock:
            self._cleanup()
            session = self.data.get(str(session_id), {})
            return [{"batch_id": key, "updated": value.get("updated"), "count": len(value.get("items") or [])} for key, value in (session.get("batches") or {}).items()]

    def clear(self, session_id):
        with self.lock:
            self.data.pop(str(session_id), None)

    def clear_all(self):
        with self.lock:
            self.data.clear()

    def find(self, session_id, agreement_id, batch_id=None):
        with self.lock:
            self._cleanup()
            session = self.data.get(str(session_id), {})
            batches = session.get("batches") or {}
            search = [str(batch_id)] if batch_id else list(batches.keys())
            for key in search:
                for item in batches.get(key, {}).get("items", []):
                    if item.get("agreement_id") == agreement_id or item.get("candidate_id") == agreement_id:
                        return dict(item)
        return None

    def _cleanup(self):
        now = time.time()
        for session_id in list(self.data):
            session = self.data[session_id]
            for batch_id in list((session.get("batches") or {})):
                if now - session["batches"][batch_id].get("updated", 0) > self.ttl:
                    session["batches"].pop(batch_id, None)
            if not session.get("batches") or now - session.get("updated", 0) > self.ttl:
                self.data.pop(session_id, None)
            elif session.get("latest_batch_id") not in session.get("batches", {}):
                session["latest_batch_id"] = sorted(session["batches"], key=lambda x: session["batches"][x].get("updated", 0))[-1]


class Application(object):
    def __init__(self, root):
        self.root = Path(root)
        self.static_dir = self.root / "app" / "static"
        self.demo_read_only = str(os.environ.get("IPDEMO_DEMO_READ_ONLY", "0")).strip().lower() in ("1", "true", "yes", "on")
        self.disable_admin = str(os.environ.get("IPDEMO_DISABLE_ADMIN", "0")).strip().lower() in ("1", "true", "yes", "on")
        self.auth_enabled = str(os.environ.get("IPDEMO_AUTH_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
        self.auth_username = str(os.environ.get("IPDEMO_AUTH_USERNAME", "ab123"))
        self.auth_password = str(os.environ.get("IPDEMO_AUTH_PASSWORD", "ab123"))
        self.auth_ttl_seconds = max(300, int(os.environ.get("IPDEMO_AUTH_TTL_SECONDS", str(8 * 3600))))
        secret = os.environ.get("IPDEMO_AUTH_SECRET")
        self.auth_secret = (secret.encode("utf-8") if secret else os.urandom(32))
        self.login_lock = threading.RLock()
        self.login_attempts = {}
        self.model_config = load_model_service_config(self.root)
        self.portal_config = load_service_portal_config(self.root)
        self.workbench_defaults = load_workbench_defaults(self.root)
        self.scenario_policy = ScenarioPolicyService(self.root / "config" / "scenario_config.json")
        self.model_execution_mode = self.model_config["execution_mode"]
        self.local_runtime = None
        self.model_gateway = None
        self.model_startup_error = None
        if self.model_execution_mode in ("service", "services", "http", "remote"):
            if self.model_config["local_fallback"]:
                self.local_runtime = IntegratedModelRuntime(self.root / "models")
            self.model_gateway = ModelServiceGateway(
                self.local_runtime,
                price_url=self.model_config["price_service_url"],
                effectiveness_url=self.model_config["effectiveness_service_url"],
                timeout=self.model_config["timeout_seconds"],
                fallback=self.model_config["local_fallback"],
                price_output_config=self.model_config.get("price_output"),
            )
            try:
                schemas = self.model_gateway.schemas()
                self.runtime = ServiceBackedRuntime(self.model_gateway, schemas=schemas, local_runtime=self.local_runtime)
            except Exception as exc:
                # Business data and historical lookup must remain available even
                # when both independent model services are stopped.
                data_code, data_name = "", ""
                database_path = self.root / "data" / "protocol_demo.db"
                if database_path.is_file():
                    try:
                        conn = sqlite3.connect(str(database_path))
                        row = conn.execute(
                            "SELECT product_code,product_name FROM products ORDER BY enabled DESC,product_code LIMIT 1"
                        ).fetchone()
                        conn.close()
                        if row:
                            data_code, data_name = str(row[0] or ""), str(row[1] or "")
                    except Exception:
                        pass
                self.model_startup_error = str(exc)
                self.runtime = DegradedServiceRuntime(self.model_gateway, data_code, data_name)
        else:
            self.local_runtime = IntegratedModelRuntime(self.root / "models")
            self.runtime = self.local_runtime
        self.store = Store(
            self.root / "data" / "protocol_demo.db",
            self.root / "data" / "virtual_protocol_dataset.csv",
            self.runtime,
            self.root / "backups",
            read_only=self.demo_read_only,
        )
        self.data_master = DataMasterService(self.store, self.runtime)
        self.requirement_versions = RequirementVersionService(self.store)
        self.semantic_snapshot = SemanticSnapshotService(
            self.store, self.data_master, self._semantic_runtime_contract
        )
        self.product_releases = ProductReleaseService(
            self.store, self.runtime, self.data_master, self._semantic_runtime_contract,
        )
        self._sync_wire_product_code()
        # A newly created installation is initialized from an operator-visible
        # DataMaster workbook, never from product-specific Python constants.
        if not self.demo_read_only and self.store.is_empty():
            default_master = self.root / "data_master" / "DataMaster_Current.xlsx"
            if not default_master.is_file():
                raise RuntimeError("数据库为空且缺少data_master/DataMaster_Current.xlsx")
            report = self.data_master.parse(default_master.name, default_master.read_bytes())
            if not report.get("valid"):
                raise RuntimeError("默认DataMaster校验失败：%s" % "；".join(report.get("errors") or []))
            self.data_master.commit(report)
        # A public demonstration must never mutate the prepared database merely
        # because the server starts. Local deployments still validate schema.
        self.model_data_sync_error = None
        self.model_data_sync_warnings = []
        self.model_parameter_coverage = {}
        if not self.demo_read_only:
            self._refresh_model_data_readiness(raise_local=True)
        self.expert_schemes = ExpertSchemeService(
            self.store.parameter_map(), self.store.current_product_code(), self.runtime,
            encode_parameters=self.store.runtime_parameters,
        )
        self.sessions = GeneratedSessions()
        self.model_lock = threading.RLock()
        self.evaluation_lock = threading.RLock()
        self.evaluation_cache = {}
        self.expert_evaluation_cache = {}
        self.generator = HistorySeededGenerator(
            self.store, self.runtime, self._evaluate_with_rules,
            self._evaluate_batch_with_rules if hasattr(self.runtime, "evaluate_batch") else None,
        )
        self.generation_tasks = GenerationTaskManager(self)

    def _semantic_runtime_contract(self):
        manifest = self.runtime.manifest()
        return {
            "product_code": self.store.current_product_code(),
            "price_output": self._current_price_output_contract(),
            "model_versions": manifest.get("model_versions") or {
                "price": (manifest.get("price") or {}).get("model_version"),
                "effectiveness": (manifest.get("effectiveness") or {}).get("model_version"),
            },
            "schemas": {
                "price": (manifest.get("price") or {}).get("schema_version"),
                "effectiveness": (manifest.get("effectiveness") or {}).get("schema_version"),
            },
        }

    def capture_requirement_version(self, request, source):
        if self.demo_read_only:
            return {"id": None, "version_no": None,
                    "demand_fingerprint": demand_fingerprint(request), "read_only": True}
        return self.requirement_versions.capture(request, created_by="operator", change_source=source)

    def _invalidate_runtime_caches(self):
        # Cancel running workers before removing their session batches.  The
        # generation manager and final add_batch use one epoch-protected commit.
        self.generation_tasks.invalidate_all()
        self.sessions.clear_all()
        with self.evaluation_lock:
            self.evaluation_cache.clear()
        self.expert_evaluation_cache.clear()
        self.generator = HistorySeededGenerator(
            self.store, self.runtime, self._evaluate_with_rules,
            self._evaluate_batch_with_rules if hasattr(self.runtime, "evaluate_batch") else None,
        )

    def _sync_wire_product_code(self):
        """Point the wire product code at the current business product.

        The model services declare their own ``product_code`` in their Schema;
        that is a diagnostic only.  The recommendation system operates on the
        business product in the database, so every request envelope carries that
        code instead of the model's declared code.
        """
        if self.model_gateway is None:
            return
        business = self.store.current_product_code() if hasattr(self, "store") else ""
        if business:
            self.model_gateway.product_code = business

    def on_business_data_changed(self):
        """Single hook for every business-data write path.

        Recomputes model readiness (Schema diagnostics + parameter coverage +
        dual-service probe) and then invalidates the in-memory caches so the
        management page never shows a stale product_ready / parameter_coverage.
        """
        self._sync_wire_product_code()
        readiness = self._refresh_model_data_readiness()
        self.expert_schemes = ExpertSchemeService(
            self.store.parameter_map(), self.store.current_product_code(), self.runtime,
            encode_parameters=self.store.runtime_parameters,
        )
        self._invalidate_runtime_caches()
        return readiness

    def _bind_runtime(self, runtime):
        """Switch all runtime consumers together after HTTP services recover."""
        self.runtime = runtime
        if hasattr(self, "store"):
            self.store.runtime = runtime
        if hasattr(self, "expert_schemes"):
            self.expert_schemes.runtime = runtime
        if hasattr(self, "data_master"):
            self.data_master.runtime = runtime
        if hasattr(self, "product_releases"):
            self.product_releases.runtime = runtime
        self._sync_wire_product_code()
        if hasattr(self, "sessions"):
            self._invalidate_runtime_caches()

    def _try_restore_model_services(self):
        """Recover automatically when services started after the main system."""
        if self.model_execution_mode not in ("service", "services", "http", "remote"):
            return not bool(self.model_data_sync_error)
        if not self.model_data_sync_error and not isinstance(self.runtime, DegradedServiceRuntime):
            return True
        try:
            schemas = self.model_gateway.schemas()
            recovered = ServiceBackedRuntime(
                self.model_gateway, schemas=schemas, local_runtime=self.local_runtime
            )
            self._bind_runtime(recovered)
            self.model_startup_error = None
            return bool(self._refresh_model_data_readiness().get("ready"))
        except Exception as exc:
            self.model_data_sync_error = str(exc)
            return False

    def _refresh_model_data_readiness(self, raise_local=False):
        """Use product identity as the only cross-system calculation gate.

        Model Schema differences remain useful operator diagnostics, but the
        independent services own parsing and preprocessing.  They must not make
        an otherwise callable price/effectiveness API unavailable.
        """
        self.model_data_sync_error = self.model_startup_error
        self.model_data_sync_warnings = []
        self.model_parameter_coverage = {}
        if isinstance(self.runtime, DegradedServiceRuntime):
            return {
                "ready": False,
                "message": self.model_data_sync_error,
                "scope": "calculation_only",
                "historical_recommendation_available": True,
            }
        try:
            data_code = self.store.current_product_code()
            service_code = str(self.runtime.schema.get("product_code") or "")
            if data_code and service_code and data_code != service_code:
                self.model_data_sync_warnings.append(
                    "业务成品代号%s与服务声明%s不同；只要实算接口接受请求并返回标准JSON，推荐仍可用。"
                    % (data_code, service_code)
                )
            try:
                self.model_data_sync_warnings.extend(self.store.sync_model_schema() or [])
            except Exception as exc:
                self.model_data_sync_warnings.append(
                    "模型Schema自动登记未完成，但不阻断HTTP实算：%s" % exc
                )
            if self.model_execution_mode in ("service", "services", "http", "remote"):
                probe_parameters = self._model_probe_parameters()
                self.model_parameter_coverage = self._parameter_coverage_diagnostics(probe_parameters)
                self.model_data_sync_warnings.append(
                    "参数覆盖：DataMaster启用%d项，价格Schema %d项，效能Schema %d项，实算JSON %d项。" % (
                        self.model_parameter_coverage.get("enabled_business_field_count", 0),
                        self.model_parameter_coverage.get("price_schema_field_count", 0),
                        self.model_parameter_coverage.get("effectiveness_schema_field_count", 0),
                        self.model_parameter_coverage.get("probe_field_count", 0),
                    )
                )
                probe = self.runtime.evaluate(probe_parameters)
                price = float(probe.get("predicted_price_wan"))
                score = float(probe.get("capability_score"))
                self.model_data_sync_warnings.append(
                    "双服务实算探测通过：价格=%s，效能=%s。" % (round(price, 6), round(score, 6))
                )
        except Exception as exc:
            if raise_local and self.model_execution_mode not in ("service", "services", "http", "remote"):
                raise
            self.model_data_sync_error = str(exc)
        return {
            "ready": not bool(self.model_data_sync_error),
            "message": self.model_data_sync_error,
            "scope": "calculation_only",
            "warnings": list(self.model_data_sync_warnings),
            "parameter_coverage": dict(self.model_parameter_coverage),
        }

    def _model_probe_parameters(self):
        """Build one complete, non-persistent request for service readiness."""
        business, _sources = self._complete_business_parameters(self._service_feature_specs())
        return self.store.runtime_parameters(business)

    def _require_product_ready(self):
        if self.model_data_sync_error:
            raise ValueError(
                "当前HTTP模型服务与运行业务数据尚未同步，推荐和计算已暂停；"
                "数据维护和业务成品切换不受影响。请启动该成品对应的价格/效能服务后重启主系统。详情：%s" % self.model_data_sync_error
            )

    @staticmethod
    def _parameter_hash(params, target_protocol=None):
        payload = {"parameters": dict(params or {})}
        if target_protocol not in (None, ""):
            payload["target_protocol"] = target_protocol
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _register_evaluation(self, requested_params, evaluation, target_protocol=None):
        token = "EVAL-" + hashlib.sha256(os.urandom(24)).hexdigest()[:24].upper()
        parameter_hash = self._parameter_hash(requested_params, target_protocol)
        result = dict(evaluation)
        result["evaluation_token"] = token
        result["parameter_hash"] = parameter_hash
        result["evaluated_at"] = datetime.now().isoformat(timespec="seconds")
        with self.evaluation_lock:
            cutoff = time.time() - 8 * 3600
            stale = [key for key, value in self.evaluation_cache.items() if value.get("created_at", 0) < cutoff]
            for key in stale:
                self.evaluation_cache.pop(key, None)
            self.evaluation_cache[token] = {
                "created_at": time.time(),
                "parameter_hash": parameter_hash,
                "evaluation": dict(result),
            }
        return result

    def _saved_evaluation(self, token, params, target_protocol=None):
        with self.evaluation_lock:
            cached = self.evaluation_cache.get(str(token or ""))
        if not cached:
            raise ValueError("当前参数尚未完成显式计算，或计算结果已过期；请先点击“重新计算价格与效能”。")
        if cached.get("parameter_hash") != self._parameter_hash(params, target_protocol):
            raise ValueError("成品属性或目标协议已在上次计算后发生变化；请重新计算后再保存。")
        return dict(cached["evaluation"])

    def make_auth_token(self, username):
        expires = int(time.time()) + self.auth_ttl_seconds
        payload = (str(username) + "|" + str(expires)).encode("utf-8")
        signature = hmac.new(self.auth_secret, payload, hashlib.sha256).hexdigest().encode("ascii")
        return base64.urlsafe_b64encode(payload + b"|" + signature).decode("ascii").rstrip("=")

    def verify_auth_token(self, token):
        try:
            token = str(token or "")
            raw = base64.urlsafe_b64decode((token + "=" * (-len(token) % 4)).encode("ascii"))
            username, expires_text, signature = raw.rsplit(b"|", 2)
            payload = username + b"|" + expires_text
            expected = hmac.new(self.auth_secret, payload, hashlib.sha256).hexdigest().encode("ascii")
            if not hmac.compare_digest(signature, expected):
                return None
            if int(expires_text.decode("ascii")) < int(time.time()):
                return None
            decoded = username.decode("utf-8")
            return decoded if hmac.compare_digest(decoded, self.auth_username) else None
        except Exception:
            return None

    def login_allowed(self, client_id):
        now = time.time()
        with self.login_lock:
            attempts = [stamp for stamp in self.login_attempts.get(client_id, []) if now - stamp < 60]
            self.login_attempts[client_id] = attempts
            return len(attempts) < 5, max(0, 60 - int(now - attempts[0])) if attempts else 0

    def record_login_failure(self, client_id):
        with self.login_lock:
            self.login_attempts.setdefault(client_id, []).append(time.time())

    def clear_login_failures(self, client_id):
        with self.login_lock:
            self.login_attempts.pop(client_id, None)

    def bootstrap(self):
        self._try_restore_model_services()
        payload = self.store.bootstrap()
        payload["model_manifest"] = self.runtime.manifest()
        payload["integration"] = {
            "mode": self.model_execution_mode, "single_port": self.model_execution_mode == "local",
            "generated_protocol_policy": "用户条件与标签规则锚定＋混合属性合法邻域＋模型输出目标优化＋双模型评价",
            "data_master_enabled": True,
            "master_data_version": self.store.master_data_version(),
            "database_admin_path": None if self.disable_admin else "/admin",
            "demo_read_only": self.demo_read_only,
            "admin_enabled": not self.disable_admin,
            "auth_enabled": self.auth_enabled,
            "authenticated_demo": self.auth_enabled and self.demo_read_only,
            "persistent_writes_enabled": not self.demo_read_only,
            "cloudflare_demo_supported": True,
            "model_config_loaded": bool(self.model_config.get("config_loaded")),
            "model_config_path": self.model_config.get("config_path"),
            "local_fallback_enabled": bool(self.model_config.get("local_fallback")),
            "price_service_url": self.model_config.get("price_service_url"),
            "effectiveness_service_url": self.model_config.get("effectiveness_service_url"),
            "price_output": dict(self.model_config.get("price_output") or {}),
            "product_ready": not bool(self.model_data_sync_error),
            "calculation_available": not bool(self.model_data_sync_error),
            "historical_recommendation_available": True,
            "model_data_sync_error": self.model_data_sync_error,
            "model_data_sync_warnings": list(self.model_data_sync_warnings),
            "parameter_coverage": dict(self.model_parameter_coverage),
            "dynamic_target_protocol_enabled": bool(
                ((self.runtime.manifest().get("effectiveness") or {}).get("target_protocol_contract") or {}).get("supported")
            ),
            "generation_limits": {
                "max_budget": self.generation_budget_limit(),
                "max_rounds": self.generation_rounds_limit(),
            },
        }
        effect_manifest = self.runtime.manifest().get("effectiveness") or {}
        payload["protocols"] = effect_manifest.get("protocol_profiles") or []
        payload["active_protocol"] = effect_manifest.get("active_protocol")
        payload["optimization_scenarios"] = self.scenario_policy.catalog()
        payload["scenario_policy"] = self.scenario_policy.resolve({"scenario": self.scenario_policy.config["default_scenario"]})
        payload["generation_profiles"] = public_profiles()
        payload["semantic_signature"] = self.semantic_snapshot.build().get("semantic_signature")
        coverage = {}
        for rule in self.store.tag_rule_rows():
            key = str(rule.get("parameter_id") or "")
            if key and not key.startswith("__"):
                coverage.setdefault(str(rule.get("tag_id")), []).append(key)
        payload["tag_parameter_coverage"] = dict(
            (key, sorted(set(values))) for key, values in coverage.items()
        )
        return payload

    def admin_snapshot(self):
        payload = self.store.admin_snapshot()
        payload["runtime_manifest"] = self.runtime.manifest()
        payload["integration"] = {
            "mode": self.model_execution_mode,
            "local_fallback_enabled": bool(self.model_config.get("local_fallback")),
            "price_service_url": self.model_config.get("price_service_url"),
            "effectiveness_service_url": self.model_config.get("effectiveness_service_url"),
            "price_output": self.price_output_settings(),
            "product_ready": not bool(self.model_data_sync_error),
            "model_data_sync_error": self.model_data_sync_error,
            "model_data_sync_warnings": list(self.model_data_sync_warnings),
            "parameter_coverage": dict(self.model_parameter_coverage),
        }
        if self.model_gateway is not None:
            payload["model_services"] = self._model_service_snapshot()
        payload["product_releases"] = self.product_releases.list()
        return payload

    def _example_parameters(self):
        """Build the full enabled business space; history only contributes values."""
        result, _sources = self._complete_business_parameters(self._service_feature_specs())
        return result

    @staticmethod
    def _field_key(field):
        return str(field.get("field_name") or field.get("key") or "").strip()

    @staticmethod
    def _field_fallback(field):
        if field.get("default_value") is not None:
            return field.get("default_value")
        if field.get("training_mean") is not None:
            return field.get("training_mean")
        allowed = list(field.get("allowed_values") or field.get("categories") or [])
        if allowed:
            return allowed[0]
        lower = field.get("generation_min", field.get("min", field.get("training_min")))
        upper = field.get("generation_max", field.get("max", field.get("training_max")))
        if lower is not None and upper is not None:
            return (float(lower) + float(upper)) / 2.0
        if lower is not None:
            return lower
        dtype = str(field.get("value_type") or field.get("dtype") or field.get("type") or "").lower()
        if dtype in ("boolean", "bool"):
            return 0
        return None

    def _service_feature_specs(self, extra_fields=None):
        """Return the union of every price/effect field, including model-only inputs."""
        groups = []
        effectiveness = getattr(self.runtime, "effectiveness", None)
        price = getattr(self.runtime, "price", None)
        groups.append(getattr(effectiveness, "features", []) or [])
        groups.append(getattr(price, "raw_contract", []) or [])
        if extra_fields:
            groups.append(extra_fields)
        result = []
        positions = {}
        for group in groups:
            for raw in group:
                item = dict(raw or {})
                key = self._field_key(item)
                if not key:
                    continue
                item["key"] = key
                if key not in positions:
                    positions[key] = len(result)
                    result.append(item)
                else:
                    current = result[positions[key]]
                    for name, value in item.items():
                        if current.get(name) in (None, "", []):
                            current[name] = value
        return result

    def _complete_business_parameters(self, schema_fields=None):
        """Merge history, configured values, Schema defaults and DataMaster fields.

        Enabled DataMaster rows define the complete business field set. Price and
        effectiveness Schema fields extend that set for model-only inputs. A
        historical agreement is a value source and can never truncate the set.
        """
        conn = self.store.connect()
        try:
            code = self.store.current_product_code()
            row = conn.execute(
                "SELECT params_json FROM agreements WHERE product_code=? AND enabled=1 ORDER BY agreement_id LIMIT 1",
                (code,),
            ).fetchone()
            definitions = [dict(item) for item in conn.execute(
                "SELECT * FROM parameter_definitions WHERE enabled=1 ORDER BY display_order,parameter_id"
            )]
        finally:
            conn.close()

        specs = self._service_feature_specs(schema_fields)
        spec_map = dict((self._field_key(item), item) for item in specs if self._field_key(item))
        definition_map = dict((item["parameter_id"], item) for item in definitions)
        ordered_keys = [item["parameter_id"] for item in definitions]
        ordered_keys.extend(key for key in spec_map if key not in definition_map)
        allowed_keys = set(ordered_keys)
        result = {}
        sources = {}
        if row:
            try:
                historical = json.loads(row["params_json"] or "{}")
                if not isinstance(historical, dict):
                    historical = {}
            except (TypeError, ValueError):
                historical = {}
            for key, value in historical.items():
                if key in allowed_keys and value not in (None, ""):
                    result[key] = value
                    sources[key] = "historical_agreement"

        for key in ordered_keys:
            if result.get(key) not in (None, ""):
                continue
            value = self._field_fallback(spec_map.get(key, {}))
            source = "model_schema"
            if value is None and key in definition_map:
                item = definition_map[key]
                try:
                    allowed = json.loads(item.get("allowed_values_json") or "[]")
                    if not isinstance(allowed, list):
                        allowed = []
                except (TypeError, ValueError):
                    allowed = []
                value = self._field_fallback({
                    "allowed_values": allowed,
                    "value_type": item.get("value_type"),
                    "min": item.get("min_value"),
                    "max": item.get("max_value"),
                })
                source = "data_master_default"
            if value is not None:
                result[key] = value
                sources[key] = source
            elif key not in result:
                # Preserve the authoritative field shape even when an operator
                # has not configured a usable value yet.  Coverage diagnostics
                # distinguish this from a missing key and the target service
                # remains responsible for its own missing-value policy.
                result[key] = None
                sources[key] = "unresolved_enabled_or_schema_field"
        return result, sources

    def _parameter_coverage_diagnostics(self, parameters, price_fields=None, effect_fields=None):
        definitions = self.store.parameter_map()
        enabled = sorted(key for key, item in definitions.items() if int(item.get("enabled") or 0))
        if price_fields is None:
            price_fields = getattr(getattr(self.runtime, "price", None), "raw_contract", []) or []
        if effect_fields is None:
            effect_fields = getattr(getattr(self.runtime, "effectiveness", None), "features", []) or []
        price_keys = sorted(set(self._field_key(item) for item in price_fields if self._field_key(item)))
        effect_keys = sorted(set(self._field_key(item) for item in effect_fields if self._field_key(item)))
        payload = dict(parameters or {})
        present = set(payload)
        empty = sorted(key for key, value in payload.items() if value in (None, ""))
        return {
            "enabled_business_field_count": len(enabled),
            "price_schema_field_count": len(price_keys),
            "effectiveness_schema_field_count": len(effect_keys),
            "union_field_count": len(set(enabled) | set(price_keys) | set(effect_keys)),
            "probe_field_count": len(present),
            "missing_enabled_in_probe": sorted(set(enabled) - present),
            "missing_price_schema_in_probe": sorted(set(price_keys) - present),
            "missing_effectiveness_schema_in_probe": sorted(set(effect_keys) - present),
            "empty_value_fields": empty,
        }

    def _model_service_snapshot(self):
        inspected = self.model_gateway.inspect_services()
        price_fields = (((inspected.get("price") or {}).get("schema") or {}).get("fields") or [])
        effect_fields = (((inspected.get("effectiveness") or {}).get("schema") or {}).get("fields") or [])
        business, sources = self._complete_business_parameters(price_fields + effect_fields)
        encoded = self.store.runtime_parameters(business)
        business_code = self.store.current_product_code()
        price_declared = str((((inspected.get("price") or {}).get("schema") or {}).get("product_code")) or "")
        effect_declared = str((((inspected.get("effectiveness") or {}).get("schema") or {}).get("product_code")) or "")
        wire_code = self.model_gateway.product_code
        examples = {}
        for kind in ("price", "effectiveness"):
            service = inspected.get(kind) or {}
            endpoint = "/api/v1/predict" if kind == "price" else "/api/v1/evaluate"
            # The example body is built by the same envelope builder used for the
            # real recommendation request, so it is the exact wire JSON.
            envelope = build_model_request(kind, encoded, request_id="DATA-CENTER-EXAMPLE", product_code=wire_code)
            examples[kind] = {
                "method": "POST", "url": service.get("url", "") + endpoint,
                "body": envelope,
            }
        return {
            "source": "independent_http_services", "local_model_files_read": False,
            "current_business_product_code": business_code,
            "identity": {
                "business_product_code": business_code,
                "price_declared_product_code": price_declared,
                "effectiveness_declared_product_code": effect_declared,
                "wire_product_code": wire_code,
            },
            "business_parameters": business, "value_sources": sources,
            "parameter_coverage": self._parameter_coverage_diagnostics(
                encoded, price_fields=price_fields, effect_fields=effect_fields
            ),
            "services": inspected, "request_examples": examples,
            "price_output": self.price_output_settings(),
        }

    def price_output_settings(self):
        config = dict(self.model_config.get("price_output") or {})
        description = (self.model_gateway.price_normalizer.describe()
                       if self.model_gateway is not None else dict(config))
        description.update({
            "configured": dict(self.model_config.get("price_output_configured") or config),
            "source": self.model_config.get("price_output_source", "default"),
            "environment_override": bool(self.model_config.get("price_output_environment_override")),
        })
        return description

    def save_model_service_settings(self, request):
        if self.demo_read_only:
            raise ValueError("当前为只读演示，不能修改模型服务设置。")
        requested = request.get("price_output") or {}
        saved = save_price_output_config(self.root, requested)
        self.model_config = load_model_service_config(self.root)
        if self.model_gateway is not None:
            self.model_gateway.set_price_output_config(self.model_config.get("price_output"))
        self._invalidate_runtime_caches()
        self.store.audit_event("update", "model_service_settings", "price_output", {
            "saved": saved.get("price_output"),
            "effective": self.model_config.get("price_output"),
            "source": self.model_config.get("price_output_source"),
        })
        return {"saved": True, "settings": self.price_output_settings(),
                "backup_path": saved.get("backup_path"), "caches_invalidated": True}

    def effectiveness_workbench_schema(self):
        if self.model_gateway is None:
            manifest = self.runtime.manifest().get("effectiveness") or {}
            fields = manifest.get("fields") or self.runtime.schema.get("features") or []
            schema = {
                "product_code": self.runtime.schema.get("product_code"),
                "product_name": self.runtime.schema.get("product_name"),
                "backend": manifest.get("backend", "local_compatibility"),
                "fields": fields,
                "protocol_profiles": manifest.get("protocol_profiles") or [],
                "active_protocol": manifest.get("active_protocol"),
                "capabilities": manifest.get("capabilities") or {},
            }
        else:
            schema = self.model_gateway.effectiveness_schema()
        schema["integration_source"] = os.environ.get("IPDEMO_EFFECT_INTEGRATION_KIND", "")
        return self._enrich_workbench_schema(schema, "effectiveness")

    def price_workbench_schema(self):
        if self.model_gateway is None:
            manifest = self.runtime.manifest().get("price") or {}
            schema = {
                "product_code": self.runtime.schema.get("product_code"),
                "product_name": self.runtime.schema.get("product_name"),
                "backend": manifest.get("backend", "local_compatibility"),
                "model_version": manifest.get("model_version"),
                "fields": manifest.get("fields") or self.runtime.price.raw_contract,
            }
        else:
            schema = self.model_gateway.price_schema()
        return self._enrich_workbench_schema(schema, "price")

    @staticmethod
    def _json_array(raw):
        if isinstance(raw, list):
            return list(raw)
        try:
            parsed = json.loads(raw or "[]")
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []

    @staticmethod
    def _workbench_fallback(field):
        allowed = field.get("allowed_values") or field.get("categories") or []
        if field.get("default_value") is not None:
            return field.get("default_value"), "model_default"
        if field.get("training_mean") is not None:
            return field.get("training_mean"), "training_mean"
        if allowed:
            return allowed[0], "allowed_value"
        lower = field.get("generation_min", field.get("training_min"))
        upper = field.get("generation_max", field.get("training_max"))
        if lower is not None and upper is not None:
            return (float(lower) + float(upper)) / 2.0, "reference_midpoint"
        if lower is not None:
            return lower, "reference_lower"
        dtype = str(field.get("business_value_type") or field.get("dtype") or field.get("type") or "").lower()
        return (0, "boolean_fallback") if dtype == "boolean" else ("", "empty")

    def _enrich_workbench_schema(self, raw_schema, model_kind):
        schema = dict(raw_schema or {})
        definitions = self.store.parameter_map()
        preferred = self.workbench_defaults.get("historical_example_agreement_id")
        example = self.store.workbench_example(preferred)
        historical = dict((example or {}).get("parameters") or {})
        fields, values, sources = [], {}, {}
        for raw in schema.get("fields") or []:
            field = dict(raw)
            key = str(field.get("field_name") or field.get("key") or "")
            if not key:
                continue
            definition = definitions.get(key) or {}
            field["field_name"] = key
            field["field_label"] = definition.get("label") or field.get("field_label") or field.get("label") or key
            field["unit"] = definition.get("unit") or field.get("unit") or ""
            field["parameter_id"] = key
            field["display_value_mapping_json"] = definition.get("display_value_mapping_json")
            field["special_value_keys_json"] = definition.get("special_value_keys_json")
            field["business_value_type"] = definition.get("value_type")
            business_allowed = self._json_array(definition.get("allowed_values_json"))
            if business_allowed:
                field["allowed_values"] = business_allowed
            def compatible_allowed(candidate):
                for allowed_value in business_allowed:
                    if str(allowed_value) == str(candidate):
                        return allowed_value
                    try:
                        if float(allowed_value) == float(candidate):
                            return allowed_value
                    except (TypeError, ValueError):
                        pass
                return None
            if key in historical and historical[key] not in (None, ""):
                historical_value = historical[key]
                compatible = compatible_allowed(historical_value) if business_allowed else historical_value
                if business_allowed and compatible is None:
                    fallback, _fallback_source = self._workbench_fallback(field)
                    fallback = self.store.business_parameters({key: fallback}).get(key, fallback)
                    value = compatible_allowed(fallback)
                    if value is None:
                        value = compatible_allowed(definition.get("business_default"))
                    if value is None:
                        value = business_allowed[0]
                    source = "historical_incompatible_fallback"
                    field["example_warning"] = "该字段历史值已不在当前业务枚举中，已使用当前业务默认值。"
                else:
                    value, source = compatible, "historical"
            else:
                value, source = self._workbench_fallback(field)
                # Model schemas may expose encoded defaults.  Workbench controls
                # always stay in DataMaster business-value space; encoding happens
                # only at the model-service boundary.
                value = self.store.business_parameters({key: value}).get(key, value)
            compatible = compatible_allowed(value) if business_allowed else None
            if compatible is not None:
                value = compatible
            field["example_value"] = value
            field["example_source"] = source
            values[key], sources[key] = value, source
            fields.append(field)
        schema["fields"] = fields
        schema["example"] = {
            "agreement_id": (example or {}).get("agreement_id"),
            "agreement_name": (example or {}).get("agreement_name"),
            "source_year": (example or {}).get("source_year"),
            "parameters": values, "value_sources": sources, "model_kind": model_kind,
        }
        schema["example_parameters"] = values
        return schema

    def portal(self):
        services = []
        for key, raw in (self.portal_config.get("services") or {}).items():
            item = dict(raw)
            if not item.get("visible", True) or (key == "admin" and self.disable_admin):
                continue
            item["key"] = key
            item["external"] = bool(item.get("enabled") and urlparse(str(item.get("url") or "")).scheme in ("http", "https"))
            services.append(item)
        return {"title": self.portal_config.get("title") or "工业技术协议智能系统",
                "services": services, "config_path": self.portal_config.get("config_path")}

    def save_portal_config(self, request):
        """Validate and atomically replace navigation config without touching model config."""
        if not isinstance(request, dict) or not isinstance(request.get("services"), dict):
            raise ValueError("服务导航配置必须包含services JSON对象")
        current = self.portal_config.get("services") or {}
        services = {}
        for key in list(current) + [key for key in request["services"] if key not in current]:
            raw = request["services"].get(key, {})
            if not isinstance(raw, dict):
                raise ValueError("服务%s的配置必须是JSON对象" % key)
            item = dict(current.get(key) or {})
            item.update(raw)
            item["label"] = str(item.get("label") or key).strip()
            item["description"] = str(item.get("description") or "").strip()
            item["url"] = str(item.get("url") or "").strip()
            item["visible"] = _boolean(item.get("visible"), True)
            item["enabled"] = _boolean(item.get("enabled"), True)
            if item["url"]:
                parsed = urlparse(item["url"])
                local = item["url"].startswith("/") and not item["url"].startswith("//") and not parsed.scheme and not parsed.netloc
                external = parsed.scheme in ("http", "https") and bool(parsed.netloc)
                if "\\" in item["url"] or any(ord(char) < 32 for char in item["url"]) or not (local or external):
                    raise ValueError("服务%s的url不安全或无效" % key)
            elif item["enabled"]:
                raise ValueError("服务%s启用时必须配置url" % key)
            services[str(key)] = item
        path = self.root / "config" / "service_portal.json"
        payload = {"title": str(request.get("title") or self.portal_config.get("title") or "工业技术协议智能系统"), "services": services}
        backup = None
        if path.is_file():
            backup = path.with_name("service_portal.%s.bak.json" % datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
            shutil.copy2(str(path), str(backup))
        temporary = path.with_name("service_portal.json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(str(temporary), str(path))
        self.portal_config = load_service_portal_config(self.root)
        return {"saved": True, "config": self.portal_config, "backup": str(backup) if backup else None}

    def price_workbench_predict(self, request):
        schema = self.price_workbench_schema()
        fields = schema.get("fields") or []
        field_names = set(
            str(item.get("field_name") or item.get("key")) for item in fields
            if item.get("field_name") or item.get("key")
        )
        business = self.store.canonical_business_parameters(request.get("parameters") or request.get("params") or {})
        encoded = self.store.runtime_parameters(business)
        model_params = dict((key, encoded[key]) for key in field_names if key in encoded)
        if self.model_gateway is not None:
            result = self.model_gateway.predict_price(
                model_params, schema.get("product_code") or self.store.current_product_code()
            )
        else:
            evaluated = self.runtime.price.predict(model_params)
            result = {
                "success": True,
                "prediction": {
                    "predicted_price_wan": evaluated.get("predicted_price_wan"),
                    "price_interval_wan": evaluated.get("price_interval_wan"),
                    "confidence": evaluated.get("price_confidence") or "medium",
                },
                "input_status": {"filled_fields": {}, "warnings": []},
                "domain_status": {"in_domain": True, "warnings": []},
                "model": self.runtime.manifest().get("price") or {},
            }
        result["business_parameters"] = self.store.business_parameters(model_params, business)
        result["workbench"] = {
            "effectiveness_service_called": False,
            "historical_database_modified": False,
            "price_service_only": True,
        }
        return result

    def effectiveness_workbench_evaluate(self, request):
        schema = self.effectiveness_workbench_schema()
        fields = schema.get("fields") or []
        field_names = set(
            str(item.get("field_name") or item.get("key")) for item in fields
            if item.get("field_name") or item.get("key")
        )
        business = self.store.canonical_business_parameters(request.get("parameters") or request.get("params") or {})
        encoded = self.store.runtime_parameters(business)
        model_params = dict((key, encoded[key]) for key in field_names if key in encoded)
        target = request.get("target_protocol")
        dynamic_protocol = bool(
            (schema.get("target_protocol_contract") or {}).get("supported")
            or (schema.get("capabilities") or {}).get("dynamic_target_protocol")
        )
        # V10 packages evaluate correctly against their packaged fixed protocol,
        # but reject any explicit target_protocol as a V11-only dynamic request.
        # Enforce compatibility on the server as well as in the browser so an
        # old/cached effectiveness.js cannot accidentally break evaluation.
        if not dynamic_protocol:
            target = None
        if self.model_gateway is not None:
            result = self.model_gateway.evaluate_effectiveness(model_params, target)
        else:
            evaluated = (
                self.runtime.evaluate(model_params, target_protocol=target)
                if target not in (None, "") else self.runtime.evaluate(model_params)
            )
            result = {
                "success": True,
                "parameters": evaluated.get("parameters") or model_params,
                "evaluation": evaluated,
                "physical_gate": evaluated.get("physical_gate") or {},
                "risk_contributors": evaluated.get("risk_contributors") or [],
                "hard_violations": evaluated.get("hard_violations") or [],
                "learned_boundary_violations": evaluated.get("learned_boundary_violations") or [],
                "experience_extrapolations": evaluated.get("experience_extrapolations") or [],
                "coupling_assessments": evaluated.get("coupling_assessments") or [],
                "requirement_assessment": evaluated.get("requirement_assessment"),
                "capability_contributors": evaluated.get("capability_contributors") or [],
                "protocol": evaluated.get("protocol"),
            }
        result["business_parameters"] = self.store.business_parameters(
            result.get("parameters") or model_params, business
        )
        result["workbench"] = {
            "price_service_called": False,
            "historical_database_modified": False,
            "effectiveness_service_only": True,
        }
        return result

    def _evaluate_with_rules(self, params, base_params=None, target_protocol=None):
        business_params = dict(params or {})
        definitions = self.store.parameter_map()
        for key, value in business_params.items():
            value_type = str((definitions.get(key) or {}).get("value_type") or "").lower()
            if value_type not in ("number", "ip_grade", "boolean") or value in (None, ""):
                continue
            try:
                number = float(str(value).upper().replace("IP", ""))
            except (TypeError, ValueError):
                raise ModelInputError("%s（%s）不是合法数值" % ((definitions.get(key) or {}).get("label") or key, key))
            if not math.isfinite(number):
                raise ModelInputError("%s（%s）不能是NaN或Infinity" % ((definitions.get(key) or {}).get("label") or key, key))
        prepared = self.store.runtime_parameters(business_params)
        evaluation = (
            self.runtime.evaluate(prepared, target_protocol=target_protocol)
            if target_protocol not in (None, "")
            else self.runtime.evaluate(prepared)
        )
        model_parameters = dict(evaluation.get("parameters") or {})
        evaluation["model_parameters"] = model_parameters
        evaluation["parameters"] = self.store.business_parameters(model_parameters, business_params)
        return self._decorate_evaluation(evaluation, base_params)

    def _evaluate_historical_with_rules(self, params, historical_price_wan=None, target_protocol=None):
        """Evaluate an unchanged historical sample without re-predicting price.

        Stored historical samples keep their transaction price; only the current
        effectiveness model is run so the sample can be ranked against the user's
        requirements. Price is re-predicted only when a price-related attribute
        is modified and the user explicitly recalculates.
        """
        business_params = dict(params or {})
        prepared = self.store.runtime_parameters(business_params)
        if hasattr(self.runtime, "evaluate_effectiveness_only"):
            evaluation = self.runtime.evaluate_effectiveness_only(
                prepared, target_protocol=target_protocol, historical_price_wan=historical_price_wan,
            )
        else:
            evaluation = (
                self.runtime.evaluate(prepared, target_protocol=target_protocol)
                if target_protocol not in (None, "")
                else self.runtime.evaluate(prepared)
            )
        model_parameters = dict(evaluation.get("parameters") or {})
        evaluation["model_parameters"] = model_parameters
        evaluation["parameters"] = self.store.business_parameters(model_parameters, business_params)
        return self._decorate_evaluation(evaluation, base_params=params)

    def _decorate_evaluation(self, evaluation, base_params=None):
        hard_details = list(evaluation.get("hard_violation_details") or evaluation.get("hard_violations") or [])
        advisory_range_violations = []
        explicit_hard_details = []
        for violation in hard_details:
            code = str(violation.get("code") or "") if isinstance(violation, dict) else ""
            if code.startswith("range_low_") or code.startswith("range_high_"):
                advisory_range_violations.append(violation)
            else:
                explicit_hard_details.append(violation)
        if advisory_range_violations:
            evaluation["advisory_range_violations"] = advisory_range_violations
            evaluation["hard_violation_details"] = explicit_hard_details
            evaluation["hard_violations"] = explicit_hard_details
            evaluation["hard_risk_reasons"] = [
                item.get("message", str(item)) if isinstance(item, dict) else str(item)
                for item in explicit_hard_details
            ]
            gate_update = dict(evaluation.get("physical_gate") or {})
            if gate_update.get("decision") == "reject_hard_violation" and not explicit_hard_details:
                if gate_update.get("mature_boundary_violations"):
                    gate_update["decision"] = "reject_mature_expert_boundary"
                elif gate_update.get("severe_coupling_mismatches"):
                    gate_update["decision"] = "reject_severe_coupling"
                elif float(gate_update.get("probability") or 0) < float(gate_update.get("probability_threshold") or 0.65):
                    gate_update["decision"] = "reject_low_feasibility_probability"
                else:
                    gate_update["decision"] = "pass"
                    gate_update["passed"] = True
                evaluation["physical_gate"] = gate_update
        definitions = self.store.parameter_map()
        feature_specs = []
        if hasattr(self.runtime, "model_feature_specs"):
            feature_specs = self.runtime.model_feature_specs()
        elif hasattr(self.runtime, "all_feature_specs"):
            for spec in self.runtime.all_feature_specs():
                item = dict(spec)
                role = item.get("model_role")
                item["model_kind"] = "price" if role == "price_only" else "effectiveness"
                feature_specs.append(item)
        evaluation["range_diagnostics"] = build_range_diagnostics(
            evaluation.get("parameters") or {}, definitions, feature_specs,
            evaluation.get("model_parameters") or evaluation.get("parameters") or {},
        )
        rule_messages = self.store.assess_rules(evaluation["parameters"], base_params)
        messages = list(rule_messages)
        for violation in evaluation.get("hard_violation_details") or []:
            key = violation.get("parameter_id") or violation.get("attribute_key")
            definition = definitions.get(key) or {}
            label = definition.get("label") or violation.get("label") or key or "未指明指标"
            actual = violation.get("actual")
            bounds = ""
            if violation.get("lower") is not None or violation.get("upper") is not None:
                bounds = "；明确规则范围 %s～%s%s" % (violation.get("lower"), violation.get("upper"), (" " + (definition.get("unit") or "")) if definition.get("unit") else "")
            messages.append({
                "source": violation.get("source") or "effectiveness_service",
                "severity": "error", "title": "模型服务返回的明确风险",
                "message": "%s（%s）当前值：%s%s" % (label, key or "unknown", actual, bounds),
                "detail": violation.get("message") or "模型服务返回了结构化硬约束违反。",
                "suggestion": "核对明确工程规则或当前模型运行版本。", "parameters": [key] if key else [],
            })
        if not evaluation.get("hard_violation_details"):
            for reason in evaluation.get("hard_risk_reasons", []):
                messages.append({"source":"model", "severity":"error", "title":"模型硬风险", "message":reason, "detail":"模型服务返回硬风险，但未提供具体指标结构。", "suggestion":"检查模型服务版本并补充parameter_id。", "parameters":[]})
        gate = evaluation.get("physical_gate") or {}
        if gate.get("passed") is False and not evaluation.get("hard_risk_reasons"):
            labels = {
                "reject_mature_expert_boundary": "命中已经成熟的专家不可行边界",
                "reject_severe_coupling": "存在严重耦合不匹配",
                "reject_low_feasibility_probability": "可行概率低于普通推荐准入阈值",
                "reject_hard_violation": "违反成品硬性物理范围",
            }
            messages.append({"source":"physical_gate", "severity":"error", "title":"物理可行性门控未通过",
                             "message":labels.get(gate.get("decision"), gate.get("decision") or "物理门控未通过"),
                             "detail":"效能分不能覆盖物理不可行结论；该方案只能作为明确标注的探索方案。",
                             "suggestion":"优先修正成熟边界、严重耦合或低可行概率问题，再参与普通推荐。", "parameters":[]})
        anomaly = evaluation.get("anomaly_assessment", {})
        if anomaly.get("is_anomaly"):
            messages.append({"source":"anomaly", "severity":"error" if anomaly.get("status") == "out_of_domain" else "warning", "title":"模型适用域提醒", "message":anomaly.get("message"), "detail":"异常度评分：%s" % anomaly.get("score"), "suggestion":"优先将指标调整回训练范围；若该工况必须保留，应补充真实样本并重新训练模型。", "parameters":[item.get("parameter_id") for item in anomaly.get("items", [])]})
        guidance = self._build_guidance(evaluation, base_params, messages)
        evaluation["rule_messages"] = messages
        evaluation["adjustment_guidance"] = guidance
        evaluation["has_blocking_risk"] = any(item.get("severity") == "error" for item in messages)
        return evaluation

    def _evaluate_batch_with_rules(self, items):
        prepared = []
        for index, item in enumerate(items or []):
            business_params = dict(item.get("parameters") or item.get("params") or {})
            params = self.store.runtime_parameters(business_params)
            prepared.append({
                "candidate_id": str(item.get("candidate_id") or index),
                "parameters": params,
                "business_parameters": business_params,
                "base_parameters": item.get("base_parameters") or item.get("base_params"),
                "target_protocol": item.get("target_protocol"),
            })
        if hasattr(self.runtime, "evaluate_batch"):
            evaluations = self.runtime.evaluate_batch(prepared)
        else:
            evaluations = [
                (
                    self.runtime.evaluate(item["parameters"], target_protocol=item.get("target_protocol"))
                    if item.get("target_protocol") not in (None, "")
                    else self.runtime.evaluate(item["parameters"])
                )
                for item in prepared
            ]
        if len(evaluations) != len(prepared):
            raise ValueError("批量模型评价返回数量与请求数量不一致。")
        result = []
        for index, evaluation in enumerate(evaluations):
            model_parameters = dict(evaluation.get("parameters") or {})
            evaluation["model_parameters"] = model_parameters
            evaluation["parameters"] = self.store.business_parameters(
                model_parameters,
                prepared[index].get("business_parameters"),
            )
            result.append(self._decorate_evaluation(evaluation, prepared[index].get("base_parameters")))
        return result

    def _build_guidance(self, evaluation, base_params, messages):
        params = evaluation["parameters"]
        definitions = self.store.parameter_map()
        changes = []
        for key, value in params.items():
            if not base_params or key not in base_params: continue
            try: delta = float(value) - float(base_params[key])
            except (TypeError, ValueError): delta = 0 if value == base_params[key] else 1
            if abs(delta) > 1e-9:
                changes.append({"parameter_id":key, "label":definitions.get(key,{}).get("label",key), "before":base_params[key], "after":value, "delta":round(delta,3) if isinstance(delta,(int,float)) else delta})
        tips = []
        for item in messages[:8]:
            tips.append({"severity":item.get("severity"), "title":item.get("title"), "text":item.get("suggestion") or item.get("message")})
        for band in evaluation.get("coupling_assessments", []):
            if band.get("state") != "inside":
                key = band["target"]; label = definitions.get(key,{}).get("label",key)
                tips.append({"severity":"warning", "title":"建议调整%s" % label, "text":"当前值%s，模型经验区间为%s～%s，经验中心为%s。该区间用于可信度判断和调整参考，不会自动覆盖用户明确筛选值。" % (band["actual"], band["lower"], band["upper"], band["predicted"])})
        if not tips:
            tips.append({"severity":"success", "title":"当前方案整体协调", "text":"未发现严重约束冲突。仍建议结合试验、制造和供应商能力进行专家复核。"})
        return {
            "changes": changes,
            "tips": tips[:10],
            "summary": {
                "prediction_confidence": evaluation.get("prediction_confidence"),
                "price_interval": evaluation.get("price_interval_wan"),
                "feasibility_probability": evaluation.get("feasibility_probability"),
                "blocking_count": sum(1 for item in messages if item.get("severity") == "error"),
                "warning_count": sum(1 for item in messages if item.get("severity") == "warning"),
            }
        }

    def generation_search_profile(self, request):
        """Classify a request before background generation starts."""
        req = dict(request or {})
        history = self.store.historical_boundary_profile()
        components = []

        def lower_target(key, envelope, label):
            target = req.get(key)
            if target in (None, "") or not envelope:
                return
            value = float(target)
            lower = float(envelope[0])
            if value < lower:
                components.append({
                    "field": key, "label": label, "direction": "below",
                    "target": value, "boundary": lower,
                    "ratio": (lower - value) / max(abs(lower), 1e-9),
                })

        def upper_target(key, envelope, label):
            target = req.get(key)
            if target in (None, "") or not envelope:
                return
            value = float(target)
            upper = float(envelope[1])
            if value > upper:
                components.append({
                    "field": key, "label": label, "direction": "above",
                    "target": value, "boundary": upper,
                    "ratio": (value - upper) / max(abs(upper), 1e-9),
                })

        lower_target("max_price", history.get("price_wan"), "价格上限")
        upper_target("min_capability", history.get("capability_score"), "最低效能")
        upper_target("min_feasibility", history.get("feasibility_probability"), "最低可行概率")
        definitions = self.store.parameter_map()
        for rule in req.get("indicator_filters") or []:
            key = str(rule.get("parameter_id") or "")
            envelope = (history.get("attributes") or {}).get(key)
            if not envelope:
                continue
            op = rule.get("operator")
            v1 = rule.get("value1")
            v2 = rule.get("value2")
            label = definitions.get(key, {}).get("label", key)
            try:
                if op in ("gte", "gt", "eq") and float(v1) > float(envelope[1]):
                    value, boundary = float(v1), float(envelope[1])
                    components.append({"field": key, "label": label, "direction": "above", "target": value,
                                       "boundary": boundary, "ratio": (value - boundary) / max(abs(boundary), 1e-9)})
                elif op in ("lte", "lt", "eq") and float(v1) < float(envelope[0]):
                    value, boundary = float(v1), float(envelope[0])
                    components.append({"field": key, "label": label, "direction": "below", "target": value,
                                       "boundary": boundary, "ratio": (boundary - value) / max(abs(boundary), 1e-9)})
                elif op == "range_inside" and v2 not in (None, ""):
                    requested_lo, requested_hi = sorted((float(v1), float(v2)))
                    if requested_lo > float(envelope[1]):
                        boundary = float(envelope[1])
                        components.append({"field": key, "label": label, "direction": "above", "target": requested_lo,
                                           "boundary": boundary, "ratio": (requested_lo - boundary) / max(abs(boundary), 1e-9)})
                    elif requested_hi < float(envelope[0]):
                        boundary = float(envelope[0])
                        components.append({"field": key, "label": label, "direction": "below", "target": requested_hi,
                                           "boundary": boundary, "ratio": (boundary - requested_hi) / max(abs(boundary), 1e-9)})
            except (TypeError, ValueError):
                continue

        max_ratio = max([float(item["ratio"]) for item in components] or [0.0])
        # More than 3% outside one historical output, or simultaneous outside
        # pressure on multiple dimensions, warrants deeper multi-round search.
        deep = max_ratio > 0.030001 or len(components) >= 2
        mode = "deep_extrapolation" if deep else "fast"
        warning = (
            "当前筛选条件超出历史经验范围较多，系统将进行多轮深度探索；等待时间会比普通生成更长。"
            "价格、效能和可行性均属于模型外推预测，仅供方案探索与专家复核参考。"
            if deep else ""
        )
        return {
            "mode": mode,
            "deep_exploration": deep,
            "max_extrapolation_ratio": round(max_ratio, 6),
            "components": components,
            "warning": warning,
            "historical_profile": history,
        }

    def generation_budget_limit(self):
        return max(60, int(self.model_config.get("generation_max_budget") or 2400))

    def generation_rounds_limit(self):
        return max(3, int(self.model_config.get("generation_max_rounds") or 15))

    def _generate_sync(self, request, progress_callback=None):
        self._require_product_ready()
        req, scenario_policy = self.scenario_policy.apply(request)
        req = self.generation_tasks.canonicalize_generation_controls(req)
        session_id = str(req.get("session_id") or "default")
        count = req["count"]
        search_profile = dict(req.get("search_profile") or self.generation_search_profile(req))
        search_profile.update({
            "exploration_profile": req.get("exploration_profile"),
            "effective_exploration_profile": req.get("effective_exploration_profile"),
            "profile_definition": req.get("exploration_profile_definition") or {},
        })
        req["search_profile"] = search_profile
        search_mode = search_profile.get("mode") or "fast"
        if req.get("exploration_profile") == "deep" and search_mode != "deep_extrapolation":
            search_mode = "deep"
        if req.get("min_feasibility") not in (None, ""):
            req["min_feasibility"] = max(float(req.get("min_feasibility")), 0.0)
        if progress_callback:
            progress_callback(
                12 if search_mode == "deep_extrapolation" else 18,
                search_profile.get("warning") or "正在分析相近方案与当前需求",
            )
        has_output_target = any(req.get(key) not in (None, "") for key in ("max_price", "min_capability", "min_cost_effectiveness", "min_feasibility"))
        max_budget = self.generation_budget_limit()
        # HTTP-backed models are the dominant cost.  A compact adaptive budget
        # plus convergence stopping gives the same staged search semantics while
        # avoiding hundreds of redundant single-candidate service calls.
        if search_mode == "deep_extrapolation":
            budget = max(360, count * 60)
        else:
            budget = max(120, count * 22) if has_output_target else max(80, count * 14)
        # Automatic budgets are also clamped by server limits.
        budget = min(budget, max_budget)
        # User-tunable candidate evaluation budget is a strict hard cap.
        if req.get("generation_budget") not in (None, ""):
            budget = int(req["generation_budget"])
        req["generation_budget"] = budget
        result = self.generator.generate(
            req, count=count, seed=req.get("seed"), budget=budget,
            search_mode=search_mode, progress_callback=progress_callback,
        )
        if progress_callback:
            progress_callback(88, "正在完成目标优化、模型评价与方案排序")
        candidates = result.get("candidates", [])
        # Batch registration is handled by GenerationTaskManager or synchronous endpoint.
        if result.get("best_effort_used"):
            message = "已生成%d份可供分析的探索方案；未完全满足项、工程冲突和模型外推风险均已单独标明。" % len(candidates)
        else:
            message = "已生成%d份满足当前筛选条件的新参数组合；超出历史经验范围的方案仍保留并显示外推风险。" % len(candidates)
        if search_mode == "deep_extrapolation":
            message = "深度越界探索已完成。%s 价格、效能和可行性属于历史经验范围外推，仅供方案探索与专家复核参考。" % message
        result.update({
            "session_id": session_id,
            "count": len(candidates),
            "message": message,
            "model_versions": self.runtime.manifest(),
            "generation_method": result.get("generation_method", "batched_directional_beam_search"),
            "search_profile": search_profile,
            "exploration_profile": req.get("exploration_profile"),
            "effective_exploration_profile": req.get("effective_exploration_profile"),
            "search_warning": search_profile.get("warning") or "",
            "scenario": scenario_policy["scenario"],
            "scenario_policy": scenario_policy,
            "applied_ranking": scenario_policy["applied_ranking"],
        })
        if progress_callback:
            progress_callback(96, "方案已准备完成")
        return result

    def generate_live(self, request):
        """Backward-compatible synchronous generation endpoint."""
        prepared_request, _policy = self.scenario_policy.apply(request)
        prepared_request = self.generation_tasks.canonicalize_generation_controls(prepared_request)
        requirement_version = self.capture_requirement_version(prepared_request, "generate")
        fingerprint = self.generation_tasks.fingerprint(prepared_request)
        result = self._generate_sync(prepared_request)
        batch_id, prepared = self.sessions.add_batch(
            str(prepared_request.get("session_id") or "default"),
            result.get("candidates", []), fingerprint=fingerprint,
        )
        result["batch_id"] = batch_id
        result["candidates"] = prepared
        result["requirement_version"] = requirement_version
        return result

    def request_generation(self, request):
        self._require_product_ready()
        req, _policy = self.scenario_policy.apply(request)
        req = self.generation_tasks.canonicalize_generation_controls(req)
        requirement_version = self.capture_requirement_version(req, "generate")
        req["requirement_version_id"] = requirement_version.get("id")
        req["demand_fingerprint"] = requirement_version.get("demand_fingerprint")
        req["search_profile"] = self.generation_search_profile(req)
        result = self.generation_tasks.start(req, force=bool(req.get("force_regenerate")))
        result["requirement_version"] = requirement_version
        return result

    def _refresh_candidates_for_protocol(self, candidates, request):
        target_protocol = request.get("target_protocol")
        if not candidates or target_protocol in (None, ""):
            return candidates
        evaluatable = [item for item in candidates if item.get("model_evaluation_available") is not False]
        if not evaluatable:
            return candidates
        prepared = [
            {
                "candidate_id": item.get("agreement_id") or index,
                "parameters": item.get("params") or {},
                "base_parameters": item.get("params") or {},
                "target_protocol": target_protocol,
            }
            for index, item in enumerate(evaluatable)
        ]
        evaluations = self._evaluate_batch_with_rules(prepared)
        definitions = self.store.parameter_map()
        tag_map = self.store.tag_map()
        refreshed = []
        for source, evaluation in zip(evaluatable, evaluations):
            item = dict(source)
            params = dict(evaluation.get("parameters") or item.get("params") or {})
            tags = self.store.derive_tags(params, evaluation, item.get("tags") or [])
            item.update({
                "params": params,
                "tags": tags,
                "evaluation": evaluation,
                "predicted_price_wan": evaluation.get("predicted_price_wan"),
                "price_interval_wan": evaluation.get("price_interval_wan"),
                "capability_score": evaluation.get("capability_score"),
                "conservative_capability_score": evaluation.get("conservative_capability_score", evaluation.get("capability_score")),
                "protocol_score_interval": evaluation.get("protocol_score_interval"),
                "support_at_80": evaluation.get("support_at_80"),
                "support_at_100": evaluation.get("support_at_100"),
                "score_uncertainty_width": evaluation.get("score_uncertainty_width"),
                "feasibility_probability": evaluation.get("feasibility_probability"),
                "physical_gate": evaluation.get("physical_gate") or {},
                "cost_effectiveness": evaluation.get("cost_effectiveness"),
            })
            demand_unmet, demand_penalty, requirement_assessment = self.generator._demand_assessment(item, request, definitions, tag_map)
            hard_conflicts, hard_penalty = self.generator._engineering_conflicts(evaluation)
            item["demand_unmet_conditions"] = demand_unmet
            item["requirement_assessment"] = requirement_assessment
            item["engineering_conflicts"] = hard_conflicts
            item["unmet_conditions"] = demand_unmet + hard_conflicts
            item["strict_filter_satisfied"] = not demand_unmet and not hard_conflicts
            item["best_effort"] = bool(demand_unmet or hard_conflicts)
            item["fit_penalty"] = round(demand_penalty + 2.5 * hard_penalty, 6)
            refreshed.append(item)
        refreshed_by_id = dict((item.get("agreement_id"), item) for item in refreshed)
        return [refreshed_by_id.get(item.get("agreement_id"), item) for item in candidates]

    def recommend(self, request):
        request, scenario_policy = self.scenario_policy.apply(request)
        requirement_version = self.capture_requirement_version(request, "recommend")
        self._try_restore_model_services()
        calculation_available = not bool(self.model_data_sync_error)
        session_id = str(request.get("session_id") or "default")
        # Pre-generation starts only after the user explicitly submits a historical
        # search.  The returned task identifier is for client-side reuse only; the
        # interface does not expose the acceleration mechanism.
        task_info = None
        if request.get("start_generation") and calculation_available:
            generation_request = dict(request)
            generation_request["count"] = max(1, min(int(request.get("generation_count") or request.get("count") or 5), 30))
            generation_request["search_profile"] = self.generation_search_profile(generation_request)
            task_info = self.generation_tasks.start(generation_request, force=False)

        source_mode = str(request.get("source_mode") or "historical").lower()
        if source_mode not in ("historical", "generated", "both"):
            source_mode = "historical"
        target_protocol = request.get("target_protocol")
        historical_items = self.store.historical_agreements(
            target_protocol=target_protocol, recalculate=calculation_available
        )
        expert_items = self.expert_recommendation_schemes(
            target_protocol=target_protocol, recalculate=calculation_available,
        )
        requested_batch_id = request.get("generation_batch_id")
        batch_metadata = self.sessions.batch_metadata(session_id, requested_batch_id) if requested_batch_id else None
        batch_request = self.generation_tasks.canonicalize_generation_controls(request)
        expected_batch_fingerprint = self.generation_tasks.fingerprint(batch_request) if requested_batch_id else None
        generation_batch_stale = bool(
            batch_metadata and batch_metadata.get("fingerprint") != expected_batch_fingerprint
        )
        if generation_batch_stale:
            source_mode = "historical"
        generated_items = (
            self.sessions.get(session_id, requested_batch_id, fingerprint=expected_batch_fingerprint)
            if calculation_available and requested_batch_id else []
        )
        generated_items = self._refresh_candidates_for_protocol(generated_items, request) if calculation_available else []
        if not calculation_available:
            source_mode = "historical"
        if source_mode == "historical":
            candidates = historical_items + expert_items
        elif source_mode == "generated":
            candidates = generated_items
        else:
            candidates = historical_items + expert_items + generated_items
        ranking_request = dict(request)
        ranking_request["include_best_effort"] = source_mode in ("generated", "both")
        tag_weights = dict((item["tag_id"], item["weight"]) for item in self.bootstrap()["tags"])
        definitions = self.store.parameter_map()
        tag_map = self.store.tag_map()
        constraint_rules = self.store.constraint_rows()
        ranked = (
            rank_agreements(candidates, ranking_request, tag_weights, definitions=definitions, tag_map=tag_map, constraint_rules=constraint_rules)
            if calculation_available else
            rank_historical_products(candidates, ranking_request, tag_weights, definitions=definitions, tag_map=tag_map, constraint_rules=constraint_rules)
        )
        ranked = annotate_candidate_recommendations(ranked, scenario_policy, definitions=definitions)
        ranked = annotate_ranking_explanations(ranked, request, definitions=definitions)
        for item in ranked:
            item["scenario"] = scenario_policy["scenario"]
        analysis_fields = (
            "agreement_id", "agreement_name", "agreement_source", "rank", "params",
            "predicted_price_wan", "capability_score", "cost_effectiveness",
            "strict_filter_satisfied", "model_evaluation_available", "is_generated",
            "engineering_conflicts", "ranking_trace",
        )
        analysis_items = [dict((key, item.get(key)) for key in analysis_fields)
                          for item in ranked[:100]]
        page, page_size = max(1, int(request.get("page", 1))), max(1, min(int(request.get("page_size", 12)), 50))
        start = (page - 1) * page_size
        best_effort_count = sum(1 for item in ranked if item.get("best_effort"))
        relaxation_suggestions = build_relaxation_suggestions(
            request, candidates, definitions, tag_map, constraint_rules,
            requirement_version=requirement_version,
        ) if ranked and not any(item.get("strict_filter_satisfied") for item in ranked) else []
        return {
            "items": ranked[start:start + page_size],
            "analysis_items": analysis_items,
            "total": len(ranked),
            "page": page,
            "page_size": page_size,
            "pages": max(1, (len(ranked) + page_size - 1) // page_size),
            "source_mode": source_mode,
            "historical_available": len(historical_items) + len(expert_items),
            "expert_available": len(expert_items),
            "live_generated_available": len(generated_items),
            "generation_batch_stale": generation_batch_stale,
            "best_effort_count": best_effort_count,
            "generation_task": task_info,
            "calculation_available": calculation_available,
            "recommendation_mode": "model_evaluated" if calculation_available else "historical_only_degraded",
            "warning": None if calculation_available else "价格和效能服务当前不可用；结果仅按历史价格、成品属性和标签筛选，未执行模型计算。",
            "protocol": (
                (ranked[0].get("evaluation") or {}).get("protocol")
                if ranked
                else target_protocol
            ),
            "scenario": scenario_policy["scenario"],
            "scenario_policy": scenario_policy,
            "applied_ranking": scenario_policy["applied_ranking"],
            "ranking_trace": ranked[0].get("ranking_trace") if ranked else None,
            "requirement_version": requirement_version,
            "relaxation_suggestions": relaxation_suggestions,
        }

    def agreement_detail(self, agreement_id, session_id, target_protocol=None):
        calculation_available = not bool(self.model_data_sync_error)
        item = self.sessions.find(session_id or "default", agreement_id) or self.store.get_historical(
            agreement_id, recalculate=calculation_available,
            target_protocol=target_protocol,
        )
        if item is None: return None
        if item.get("model_evaluation_available") is False:
            item["current_model_evaluation"] = item.get("evaluation") or {
                "model_evaluation_available": False,
                "parameters": item.get("params") or {}, "predicted_price_wan": None,
                "capability_score": None, "conservative_capability_score": None,
                "cost_effectiveness": None, "feasibility_probability": None,
                "prediction_confidence": "unavailable", "coupling_assessments": [],
                "range_diagnostics": [], "tag_evidence": item.get("tag_evidence") or {},
                "rule_messages": [{"severity":"warning", "title":"模型评价不可用",
                                   "message":"模型服务拒绝该参数组合；当前仅保留参数探索方案。",
                                   "detail":"可调整参数后重新尝试计算。"}],
                "adjustment_guidance": {"tips": []},
            }
            return item
        if not calculation_available:
            item["model_evaluation_available"] = False
            item["current_model_evaluation"] = {
                "model_evaluation_available": False,
                "parameters": item.get("params") or {}, "predicted_price_wan": None,
                "capability_score": None, "conservative_capability_score": None,
                "cost_effectiveness": None, "feasibility_probability": None,
                "prediction_confidence": "unavailable", "coupling_assessments": [],
                "rule_messages": [{"severity":"warning", "title":"当前为纯历史推荐模式",
                                   "message":"价格和效能服务不可用，未执行模型计算。",
                                   "detail":"仍可查看历史价格、标签和原始成品属性。"}],
                "adjustment_guidance": {"tips": []},
            }
            return item
        historical_price = item.get("historical_price_wan")
        if item.get("is_generated") or historical_price in (None, ""):
            # Generated candidates have no stored transaction price; run the
            # full price+effectiveness evaluation.
            current = self._evaluate_with_rules(
                item["params"], item["params"], target_protocol=target_protocol,
            )
        else:
            current = self._evaluate_historical_with_rules(
                item["params"], historical_price, target_protocol=target_protocol,
            )
        item["current_model_evaluation"] = current
        item["predicted_price_wan"] = current["predicted_price_wan"]
        item["price_source"] = current.get("price_source", "historical")
        return item

    @staticmethod
    def _parameters_changed(params, base):
        """True when a business attribute actually differs from its base value.

        The editor may round-trip values through Number() while the stored
        historical JSON keeps strings, so compare tolerantly (string-equal or
        numeric-equal).  A field that the historical sample left unset is not a
        user modification, so it never counts as "changed"; clearing a field the
        sample did set does count.
        """
        params = dict(params or {})
        base = dict(base or {})
        for key in set(params) | set(base):
            a = params.get(key)
            b = base.get(key)
            if b in (None, ""):
                continue
            if a == b:
                continue
            if a in (None, ""):
                return True
            try:
                if float(a) == float(b):
                    continue
            except (TypeError, ValueError):
                pass
            return True
        return False

    def evaluate(self, request):
        self._require_product_ready()
        params = request.get("parameters") or request.get("params") or {}
        base = request.get("base_parameters") or request.get("base_params")
        target_protocol = request.get("target_protocol")
        base_agreement_id = request.get("base_agreement_id")
        is_generated = bool(request.get("is_generated"))
        evaluation = None
        if base_agreement_id and base and not is_generated:
            # An unchanged historical sample keeps its real transaction price;
            # only the effectiveness model is re-run. Generated schemes (which
            # reuse a historical seed for tag inheritance) always re-predict
            # their own price, so they never take this path.
            base_item = self.store.get_historical(base_agreement_id, target_protocol=target_protocol, recalculate=False)
            if base_item:
                historical_price = base_item.get("historical_price_wan")
                if historical_price not in (None, ""):
                    # Compare only the current business field set so stale keys
                    # left in older agreement JSON never look like a user edit.
                    defined_keys = set(self.store.parameter_map().keys())
                    base_defined = {k: v for k, v in (base or {}).items() if k in defined_keys}
                    if not self._parameters_changed(params, base_defined):
                        evaluation = self._evaluate_historical_with_rules(
                            params, historical_price, target_protocol=target_protocol,
                        )
        if evaluation is None:
            evaluation = self._evaluate_with_rules(params, base, target_protocol=target_protocol)
        context = dict(request.get("recommendation_context") or {})
        if not context:
            return self._register_evaluation(params, evaluation, target_protocol)
        base_tags = list(request.get("base_tags") or [])
        base_agreement_id = request.get("base_agreement_id")
        if base_agreement_id:
            base_item = self.store.get_historical(base_agreement_id, target_protocol=target_protocol)
            if base_item:
                base_tags = list(base_item.get("tags") or base_tags)
        current_tags = self.store.derive_tags(evaluation.get("parameters") or params, evaluation, base_tags)
        tag_evidence = self.store.tag_evidence(evaluation.get("parameters") or params, evaluation, base_tags)
        definitions = self.store.parameter_map()
        tag_map = self.store.tag_map()
        item = {
            "params": evaluation.get("parameters") or params,
            "tags": current_tags,
            "evaluation": evaluation,
            "predicted_price_wan": evaluation.get("predicted_price_wan"),
            "capability_score": evaluation.get("capability_score"),
            "historical_price_wan": None,
        }
        demand_unmet, demand_penalty, _requirement_assessment = self.generator._demand_assessment(item, context, definitions, tag_map)
        hard_conflicts, hard_penalty = self.generator._engineering_conflicts(evaluation)
        locked = set(context.get("locked_parameters") or [])
        contour_details = []
        for band in evaluation.get("coupling_assessments") or []:
            detail = dict(band)
            detail["target_locked"] = detail.get("target") in locked
            contour_details.append(detail)
        extrapolation, contour_penalty, anomaly_penalty = self.generator._extrapolation_assessment(evaluation, contour_details)
        best_effort = bool(demand_unmet or hard_conflicts)
        if best_effort or anomaly_penalty >= 0.18 or contour_penalty >= 0.70:
            confidence = "low"
        elif extrapolation or anomaly_penalty > 0:
            confidence = "medium"
        else:
            confidence = "high"
        evaluation["tags"] = current_tags
        evaluation["tag_evidence"] = tag_evidence
        evaluation["coupling_assessments"] = contour_details
        evaluation["recommendation_assessment"] = {
            "demand_unmet_conditions": demand_unmet,
            "engineering_conflicts": hard_conflicts,
            "unmet_conditions": demand_unmet + hard_conflicts,
            "extrapolation_warnings": extrapolation,
            "strict_filter_satisfied": not best_effort,
            "best_effort": best_effort,
            "recommendation_confidence": confidence,
            "demand_penalty": round(demand_penalty, 6),
            "hard_penalty": round(hard_penalty, 6),
            "contour_penalty": round(contour_penalty, 6),
            "anomaly_penalty": round(anomaly_penalty, 6),
        }
        evaluation["prediction_confidence"] = confidence
        return self._register_evaluation(params, evaluation, target_protocol)

    def save(self, request):
        self._require_product_ready()
        params = request.get("parameters") or request.get("params") or {}
        evaluation = self._saved_evaluation(
            request.get("evaluation_token"),
            params,
            request.get("target_protocol"),
        )
        feasibility = evaluation.get("feasibility_probability")
        has_risk = bool(evaluation.get("has_blocking_risk")) or (
            feasibility is not None and float(feasibility) < 0.45
        )
        risk_confirmed = bool(request.get("risk_confirmed"))
        if has_risk and not risk_confirmed: return {"saved":False,"requires_risk_confirmation":True,"evaluation":evaluation}
        final_params = self.store.canonical_business_parameters(evaluation["parameters"])
        base_params = self.store.canonical_business_parameters(request.get("base_parameters") or {})
        delta = self.expert_schemes.build_delta(base_params, final_params)
        recommendation_eligible = not has_risk
        # Persist the output contract with the immutable evaluation snapshot.
        # Adding this after save_scheme would only decorate the response object
        # and leave evaluation_json without provenance.
        evaluation["price_output_contract"] = self._current_price_output_contract()
        scheme_id = self.store.save_scheme(
            request.get("scheme_name"), request.get("base_agreement_id"),
            request.get("source_type") or "expert_modified", final_params, evaluation, risk_confirmed,
            base_params=base_params, delta=delta, changed_parameter_ids=sorted(delta),
            target_protocol=request.get("target_protocol"),
            schema_signature=self.expert_schemes.schema_signature(),
            recommendation_eligible=recommendation_eligible, training_candidate=True,
            base_scheme_name=request.get("base_agreement_name"),
        )
        saved_item = next((item for item in self.store.list_saved() if int(item.get("id")) == int(scheme_id)), {})
        return {"saved":True,"scheme_id":scheme_id,"evaluation":evaluation,
                "changed_parameter_ids":sorted(delta),
                "recommendation_eligible":recommendation_eligible,
                "scheme_name":saved_item.get("scheme_name"),
                "expert_revision_no":saved_item.get("expert_revision_no"),
                "root_base_agreement_id":saved_item.get("root_base_agreement_id"),
                "parent_saved_scheme_id":saved_item.get("parent_saved_scheme_id")}

    def saved_schemes(self):
        items = self.store.list_saved()
        for item in items:
            item["compatibility"] = self.expert_schemes.compatibility(item)
            item["changed_count"] = len(item.get("changed_parameter_ids") or [])
            self._annotate_saved_price_contract(item)
        return items

    def _current_price_output_contract(self):
        normalizer = (self.model_gateway.price_normalizer
                      if getattr(self, "model_gateway", None) is not None
                      else PriceOutputNormalizer())
        return {
            "signature": normalizer.signature(),
            "unit": normalizer.config["unit"],
            "scale": normalizer.config["scale"],
            "normalized_unit": "wan_yuan",
            "contract_version": normalizer.VERSION,
        }

    def _annotate_saved_price_contract(self, item):
        evaluation = dict(item.get("evaluation") or item.get("saved_evaluation") or {})
        saved_contract = dict(evaluation.get("price_output_contract") or {})
        normalization = dict(evaluation.get("price_output_normalization") or {})
        if not saved_contract and normalization:
            saved_contract = {
                "signature": normalization.get("signature"),
                "unit": normalization.get("raw_unit"),
                "scale": normalization.get("scale"),
                "normalized_unit": normalization.get("normalized_unit") or "wan_yuan",
                "contract_version": normalization.get("contract_version"),
            }
        # Records saved before V21.5.2 used the application's historical
        # wan-yuan/scale-1 contract.  Preserve that provenance explicitly;
        # never reinterpret the stored number using today's settings.
        saved_contract.setdefault("unit", "wan_yuan")
        saved_contract.setdefault("scale", 1.0)
        saved_contract.setdefault("normalized_unit", "wan_yuan")
        if not saved_contract.get("signature"):
            try:
                saved_contract["signature"] = PriceOutputNormalizer({
                    "unit": saved_contract.get("unit") or "wan_yuan",
                    "scale": saved_contract.get("scale", 1.0),
                }).signature()
            except ValueError:
                saved_contract["signature"] = "invalid-saved-price-contract"
        current = self._current_price_output_contract()
        stale = saved_contract.get("signature") != current.get("signature")
        item["price_evaluation_contract"] = {
            "stale": stale,
            "saved": saved_contract,
            "current": current,
            "display_source": "saved_snapshot_stale" if stale else "saved_snapshot_current_contract",
            "message": ("保存时价格采用旧输出单位或尺度；该数值不再作为当前万元价格展示。"
                        if stale else "保存时价格与当前输出契约一致。"),
        }
        return item

    def relaxation_suggestions(self, request):
        version = self.capture_requirement_version(request, "relaxation")
        calculation_available = not bool(self.model_data_sync_error)
        candidates = self.store.historical_agreements(
            target_protocol=request.get("target_protocol"), recalculate=calculation_available
        ) + self.expert_recommendation_schemes(
            target_protocol=request.get("target_protocol"), recalculate=calculation_available
        )
        batch_id = request.get("generation_batch_id")
        if batch_id:
            candidates += self.sessions.get(str(request.get("session_id") or "default"), batch_id)
        return {
            "requirement_version": version,
            "items": build_relaxation_suggestions(
                request, candidates, self.store.parameter_map(), self.store.tag_map(),
                self.store.constraint_rows(), requirement_version=version,
            ),
        }

    def save_final_decision(self, request):
        # Final decisions are audit records: always resolve the authoritative
        # server-side scheme by id and never trust a client supplied snapshot.
        scheme_id = str(request.get("scheme_id") or "").strip()
        snapshot = self.sessions.find(request.get("session_id") or "default", scheme_id)
        if snapshot is None and scheme_id.upper().startswith("SAVED-"):
            snapshot = self.saved_detail(scheme_id.split("-", 1)[1], request.get("target_protocol"))
        if snapshot is None:
            snapshot = self.agreement_detail(
                scheme_id, request.get("session_id") or "default",
                request.get("target_protocol"),
            ) or {}
        if not snapshot:
            raise ValueError("未找到要标记的方案。")
        version_id = request.get("demand_version_id")
        with self.store.lock:
            conn = self.store.connect()
            try:
                if version_id not in (None, ""):
                    version = conn.execute(
                        "SELECT id FROM requirement_versions WHERE id=? AND product_code=?",
                        (int(version_id), self.store.current_product_code()),
                    ).fetchone()
                    if not version:
                        raise ValueError("需求版本不属于当前成品。")
                cur = conn.execute(
                    "INSERT INTO final_decisions(scheme_id,scheme_snapshot_json,source,"
                    "demand_version_id,product_code,created_at) VALUES(?,?,?,?,?,?)",
                    (scheme_id or str(snapshot.get("agreement_id") or ""),
                     json.dumps(snapshot, ensure_ascii=False),
                     str(request.get("source") or snapshot.get("agreement_source") or "unknown"),
                     int(version_id) if version_id not in (None, "") else None,
                     self.store.current_product_code(), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                )
                conn.commit()
                return {"id": cur.lastrowid, "saved": True, "demand_version_id": version_id}
            finally:
                conn.close()

    def final_decisions(self, limit=100):
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM final_decisions WHERE product_code=? ORDER BY id DESC LIMIT ?",
                (self.store.current_product_code(), max(1, min(int(limit), 500)))
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["scheme_snapshot"] = json.loads(item.pop("scheme_snapshot_json") or "{}")
                items.append(item)
            return items
        finally:
            conn.close()

    def _dedupe_expert_snapshots(self, items, near_threshold=0.018):
        definitions = self.store.parameter_map()
        kept, signatures = [], {}
        for item in items:
            signature = self.expert_schemes.canonical_parameter_signature(item.get("params") or {})
            if signature in signatures:
                signatures[signature]["expert_prior_count"] += 1
                continue
            near = next((existing for existing in kept if self.generator._normalized_distance(
                item.get("params") or {}, existing.get("params") or {}, definitions,
            ) < near_threshold), None)
            if near is not None:
                near["expert_prior_count"] += 1
                continue
            item = dict(item)
            item["expert_prior_count"] = 1
            item["canonical_parameter_signature"] = signature
            kept.append(item)
            signatures[signature] = item
        return kept

    def _expert_candidate(self, saved, evaluation, model_evaluation_available=True):
        item = dict(saved)
        business_params = dict((evaluation or {}).get("parameters") or saved.get("params") or {})
        item["params"] = business_params
        item["evaluation"] = dict(evaluation or saved.get("evaluation") or {})
        item["saved_evaluation"] = dict(saved.get("evaluation") or saved.get("saved_evaluation") or {})
        item["agreement_id"] = "SAVED-%s" % item["id"]
        item["agreement_name"] = item["scheme_name"]
        item["agreement_source"] = "expert_saved"
        item["is_generated"] = False
        item["positioning"] = "专家保存方案"
        item["model_evaluation_available"] = bool(model_evaluation_available)
        for key in ("predicted_price_wan", "capability_score", "conservative_capability_score",
                    "feasibility_probability", "cost_effectiveness"):
            item[key] = item["evaluation"].get(key)
        item["tags"] = self.store.derive_tags(business_params, item["evaluation"])
        return item

    def expert_recommendation_schemes(self, target_protocol=None, recalculate=True):
        eligible = [item for item in self.saved_schemes()
                    if item["compatibility"].get("recommendation_eligible_effective")]
        eligible = self._dedupe_expert_snapshots(eligible)
        if not recalculate:
            return [self._expert_candidate(item, item.get("evaluation"), False) for item in eligible]
        result, pending, pending_keys = [], [], []
        protocol_key = str(target_protocol or "")
        cache_identity = self._expert_evaluation_identity()
        for item in eligible:
            key = (int(item["id"]), item.get("canonical_parameter_signature"),
                   self.expert_schemes.schema_signature(), protocol_key, cache_identity)
            with self.evaluation_lock:
                cached = self.expert_evaluation_cache.get(key)
            if cached is not None:
                result.append(self._expert_candidate(item, cached, True))
            else:
                pending.append({
                    "candidate_id": "SAVED-%s" % item["id"], "parameters": item.get("params") or {},
                    "base_parameters": item.get("base_params") or item.get("params") or {},
                    "target_protocol": target_protocol, "saved": item,
                })
                pending_keys.append(key)
        if pending:
            try:
                evaluations = self._evaluate_batch_with_rules(pending)
                for source, key, evaluation in zip(pending, pending_keys, evaluations):
                    with self.evaluation_lock:
                        self.expert_evaluation_cache[key] = evaluation
                    result.append(self._expert_candidate(source["saved"], evaluation, True))
            except Exception:
                # Batch failure is isolated so one stale expert snapshot cannot
                # hide the rest.  This slow path runs only for the failed batch.
                for source, key in zip(pending, pending_keys):
                    try:
                        evaluation = self._evaluate_with_rules(
                            source["parameters"], source["base_parameters"], target_protocol=target_protocol,
                        )
                        with self.evaluation_lock:
                            self.expert_evaluation_cache[key] = evaluation
                        result.append(self._expert_candidate(source["saved"], evaluation, True))
                    except Exception:
                        continue
        return result

    def _expert_evaluation_identity(self):
        runtime = getattr(self, "runtime", None)
        manifest = runtime.manifest() if runtime is not None and hasattr(runtime, "manifest") else {}
        price = manifest.get("price") or {}
        effectiveness = manifest.get("effectiveness") or {}
        price_version = price.get("model_version")
        effect_version = effectiveness.get("model_version")
        if getattr(self, "model_gateway", None) is not None:
            try:
                live_schemas = self.model_gateway.schemas()
                price_version = (live_schemas.get("price") or {}).get("model_version") or price_version
                effect_version = (live_schemas.get("effectiveness") or {}).get("model_version") or effect_version
            except Exception:
                # A service outage is handled by the existing degraded expert
                # path; identity probing must not hide persisted snapshots.
                pass
        identity = {
            "price_model_version": price_version,
            "effectiveness_model_version": effect_version,
            "price_output_contract": (
                self.model_gateway.price_normalizer.signature()
                if getattr(self, "model_gateway", None) is not None else "local-price-output"
            ),
        }
        # Services that omit model_version cannot safely own an unbounded cache.
        if not price_version or not effect_version:
            identity["versionless_ttl_bucket"] = int(time.time() // 300)
        raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def set_saved_recommendation_eligibility(self, scheme_id, enabled):
        saved = next((item for item in self.saved_schemes() if int(item.get("id")) == int(scheme_id)), None)
        if saved is None:
            raise ValueError("专家方案不存在。")
        if enabled:
            probe = dict(saved)
            probe["recommendation_eligible"] = 1
            compatibility = self.expert_schemes.compatibility(probe)
            if not compatibility.get("product_match") or not compatibility.get("schema_compatible"):
                raise ValueError("当前专家方案与当前成品或字段定义不兼容，不能参与推荐。")
        return self.store.set_saved_recommendation_eligibility(scheme_id, enabled)

    def export_saved_schemes(self, scheme_ids):
        items = self.store.saved_by_ids(scheme_ids)
        return [self.expert_schemes.training_export_record(item) for item in items]

    def set_saved_training_candidate(self, scheme_id, enabled):
        return self.store.set_saved_training_candidate(scheme_id, enabled)

    def improve(self, request):
        self._require_product_ready()
        if not hasattr(self.runtime, "improve"):
            raise ValueError("当前模型运行方式不支持V11反事实改进处方。")
        params = request.get("parameters") or request.get("params") or {}
        prepared = self.store.runtime_parameters(params)
        result = self.runtime.improve(
            prepared,
            target_protocol=request.get("target_protocol"),
        )
        plan = result.get("improvement_plan") or {}
        recommended_params = plan.get("recommended_parameters") or {}
        if recommended_params:
            business_recommended = self.store.business_parameters(recommended_params)
            plan["recommended_parameters"] = business_recommended
            fields = self.store.parameter_map()
            before_score = float(plan.get("before_score") or 0.0)
            after_score = float(plan.get("after_score") or before_score)
            plan["changes"] = [
                {
                    "parameter_id": key,
                    "attribute_label": fields.get(key, {}).get("label", key),
                    "before": params.get(key),
                    "after": value,
                    "unit": fields.get(key, {}).get("unit") or "",
                    "score_gain": round(after_score - before_score, 6),
                }
                for key, value in business_recommended.items()
                if str(value) != str(params.get(key))
            ]
        recommended = plan.get("recommended_evaluation")
        if recommended:
            model_parameters = dict(recommended.get("parameters") or {})
            recommended["model_parameters"] = model_parameters
            recommended["parameters"] = self.store.business_parameters(
                model_parameters,
                plan.get("recommended_parameters") or {},
            )
            plan["recommended_evaluation"] = self._decorate_evaluation(
                recommended,
                base_params=params,
            )
        result["improvement_plan"] = plan
        return result

    def saved_detail(self, scheme_id, target_protocol=None):
        item = self.store.get_saved(scheme_id, recalculate=False)
        if item is None:
            return None
        item["compatibility"] = self.expert_schemes.compatibility(item)
        self._annotate_saved_price_contract(item)
        if self.model_data_sync_error:
            current = dict(item.get("saved_evaluation") or item.get("evaluation") or {})
            current["parameters"] = dict(item.get("params") or {})
            current["model_evaluation_available"] = False
            current["evaluation_notice"] = "计算服务当前不可用；保存时评价已保留，但未作为当前评价结果展示。"
        else:
            try:
                current = self._evaluate_with_rules(
                    item["params"], item.get("base_params") or item["params"],
                    target_protocol=target_protocol,
                )
                current["model_evaluation_available"] = True
            except Exception as exc:
                current = dict(item.get("saved_evaluation") or item.get("evaluation") or {})
                current["parameters"] = dict(item.get("params") or {})
                current["model_evaluation_available"] = False
                current["evaluation_notice"] = "当前模型未能重新评价；保存时评价已保留，仅供历史追溯：%s" % exc
        item["current_model_evaluation"] = current
        item["model_evaluation_available"] = current.get("model_evaluation_available", True)
        stale_unavailable = (
            item["model_evaluation_available"] is False
            and item.get("price_evaluation_contract", {}).get("stale") is True
        )
        # Top-level metrics always mean a currently usable evaluation.  The
        # immutable historical values remain available under saved_evaluation
        # and current_model_evaluation with model_evaluation_available=false.
        item["predicted_price_wan"] = None if stale_unavailable else current.get("predicted_price_wan")
        item["capability_score"] = None if stale_unavailable else current.get("capability_score")
        item["feasibility_probability"] = None if stale_unavailable else current.get("feasibility_probability")
        item["cost_effectiveness"] = None if stale_unavailable else current.get("cost_effectiveness")
        return item

    def wide_table_parser(self):
        bootstrap = self.store.bootstrap()
        return WideTableParser(
            bootstrap["parameters"], bootstrap["tags"],
            self.store.current_product_code() or self.runtime.schema["product_code"],
        )

    def parse_wide_table(self, request, commit=False):
        filename = request.get("filename") or "protocols.csv"
        encoded = request.get("file_base64") or ""
        if not encoded:
            raise ValueError("未上传宽表文件")
        try:
            data = base64.b64decode(encoded)
        except Exception:
            raise ValueError("宽表文件Base64无效")
        report = self.wide_table_parser().parse(filename, data)
        if commit:
            if report["invalid_count"] and not request.get("skip_invalid"):
                raise ValueError("宽表存在%d条无效记录，请先修正或选择跳过无效行。" % report["invalid_count"])
            result = self.store.import_wide_rows(
                report["rows"], overwrite=bool(request.get("overwrite")), evaluate=False,
            )
            report["commit_result"] = result
        return report

    def parse_release_module(self, request, stage=False):
        release_id = str(request.get("release_id") or "")
        section = str(request.get("section") or "")
        encoded = request.get("file_base64") or ""
        if not release_id:
            raise ValueError("缺少待发布成品编号。")
        if not encoded:
            raise ValueError("未上传模块文件。")
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            raise ValueError("模块文件Base64无效。")
        report = self.product_releases.parse_module(
            release_id, section, request.get("filename") or ("%s.csv" % section), raw
        )
        if stage:
            report["stage_result"] = self.product_releases.stage_module(
                release_id, section, report, skip_invalid=bool(request.get("skip_invalid"))
            )
        return report

    def activate_product_release(self, release_id):
        result = self.product_releases.activate(str(release_id or ""))
        result["runtime_readiness"] = self.on_business_data_changed()
        return result

    def import_product_release_package(self, request):
        encoded = request.get("file_base64") or ""
        if not encoded:
            raise ValueError("未上传离线发布包。")
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            raise ValueError("离线发布包Base64无效。")
        return self.product_releases.import_package(raw)

    def historical_product_onboarding(self, request, create=False):
        encoded = request.get("file_base64") or ""
        if not encoded:
            raise ValueError("未上传历史成品数据文件。")
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            raise ValueError("历史成品数据文件Base64无效。")
        args = (
            request.get("filename") or "historical_products.csv", raw,
            request.get("product_code"), request.get("product_name"),
            request.get("missing_tokens"),
        )
        if create:
            return self.product_releases.create_from_history(*args)
        report = self.product_releases.analyze_history(*args)
        return dict((key, value) for key, value in report.items() if key != "data")

    def import_release_maintenance_workbook(self, request):
        encoded = request.get("file_base64") or ""
        if not encoded:
            raise ValueError("未上传维护工作簿。")
        try:
            raw = base64.b64decode(encoded)
        except Exception:
            raise ValueError("维护工作簿Base64无效。")
        return self.product_releases.import_maintenance_workbook(
            str(request.get("release_id") or ""),
            request.get("filename") or "DataMaster_Draft.xlsx", raw,
            skip_invalid=bool(request.get("skip_invalid")),
        )

    def replace_models(self, request):
        if self.model_execution_mode in ("service", "services", "http", "remote"):
            raise ValueError("当前为独立模型服务模式；请在价格服务和效能服务中安装模型，主系统不再替换本地bundle。")
        e_data, p_data = request.get("effectiveness_base64"), request.get("price_base64")
        if not e_data or not p_data: raise ValueError("必须同时选择效能模型和价格模型")
        with self.model_lock:
            model_dir = self.root / "models"; temp_dir = model_dir / "_incoming"; temp_dir.mkdir(parents=True, exist_ok=True)
            e_tmp, p_tmp = temp_dir / "effectiveness_bundle.json", temp_dir / "price_bundle.json"
            e_tmp.write_bytes(base64.b64decode(e_data)); p_tmp.write_bytes(base64.b64decode(p_data))
            report = IntegratedModelRuntime.validate_pair(e_tmp, p_tmp)
            clean_report = dict((k,v) for k,v in report.items() if k not in ("effectiveness","price"))
            if not report["valid"]:
                e_tmp.unlink(missing_ok=True); p_tmp.unlink(missing_ok=True)
                return {"replaced":False,"contract":clean_report}
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive = model_dir / "archive" / stamp; archive.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(self.runtime.effectiveness_path), str(archive / "effectiveness_bundle.json"))
            shutil.copy2(str(self.runtime.price_path), str(archive / "price_bundle.json"))
            try:
                os.replace(str(e_tmp), str(self.runtime.effectiveness_path)); os.replace(str(p_tmp), str(self.runtime.price_path)); self.runtime.reload()
                self.store.sync_model_schema()
                self._invalidate_runtime_caches()
                conn = self.store.connect()
                try: self.store._audit(conn,"replace","models",stamp,clean_report); conn.commit()
                finally: conn.close()
            except Exception:
                shutil.copy2(str(archive / "effectiveness_bundle.json"), str(self.runtime.effectiveness_path)); shutil.copy2(str(archive / "price_bundle.json"), str(self.runtime.price_path)); self.runtime.reload(); raise
            return {"replaced":True,"contract":clean_report,"manifest":self.runtime.manifest(),"archive":str(archive.relative_to(self.root))}


class Handler(BaseHTTPRequestHandler):
    app = None
    AUTH_COOKIE = "ipdemo_auth"
    PUBLIC_PATHS = {"/login", "/login.html", "/login.js", "/styles.css", "/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}
    WRITE_PATHS = {
        "/api/save-scheme", "/api/admin/upsert", "/api/admin/delete", "/api/admin/toggle", "/api/admin/purge",
        "/api/admin/conditional-constraint/upsert", "/api/admin/conditional-constraint/delete",
        "/api/admin/backup", "/api/admin/restore-backup",
        "/api/admin/upload-database", "/api/admin/models/replace",
        "/api/admin/wide-import/commit", "/api/admin/datamaster/commit",
        "/api/admin/product-releases/create", "/api/admin/product-releases/delete",
        "/api/admin/product-releases/clone-current", "/api/admin/product-releases/package/import",
        "/api/admin/product-releases/module/stage", "/api/admin/product-releases/section/save",
        "/api/admin/product-releases/history/create", "/api/admin/product-releases/maintenance/import",
        "/api/admin/product-releases/validate", "/api/admin/product-releases/activate",
        "/api/admin/portal-config", "/api/admin/model-service-settings",
        "/api/requirements/version", "/api/requirements/restore", "/api/final-decisions",
    }

    def log_message(self, fmt, *args): print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _cookie_value(self, name):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[name].value if name in cookie else None
        except Exception:
            return None

    def _authenticated_user(self):
        if not self.app.auth_enabled:
            return self.app.auth_username
        return self.app.verify_auth_token(self._cookie_value(self.AUTH_COOKIE))

    def _is_authenticated(self):
        return bool(self._authenticated_user())

    def _request_is_https(self):
        if str(self.headers.get("X-Forwarded-Proto", "")).lower() == "https":
            return True
        return '"scheme":"https"' in str(self.headers.get("CF-Visitor", "")).replace(" ", "").lower()

    def _set_auth_cookie(self, token, max_age):
        value = "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (self.AUTH_COOKIE, token, int(max_age))
        if self._request_is_https():
            value += "; Secure"
        self.send_header("Set-Cookie", value)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    @staticmethod
    def _safe_next(value):
        value = unquote(str(value or "")).strip()
        parsed = urlparse(value)
        if (not value.startswith("/") or value.startswith("//") or "\\" in value or
                any(ord(char) < 32 for char in value) or parsed.scheme or parsed.netloc):
            return None
        return value

    def _auth_required(self, path):
        if not self.app.auth_enabled or path in self.PUBLIC_PATHS:
            return False
        if self._is_authenticated():
            return False
        if path.startswith("/api/"):
            self._json({"error":"authentication_required","message":"请先登录后访问演示系统。","login_path":"/login?next=" + quote(self.path, safe="")}, 401)
        else:
            self._redirect("/login?next=" + quote(self.path, safe=""))
        return True

    def _demo_forbidden(self, message="当前为登录只读演示，页面和数据可以查看，但服务器端写入操作已禁用。"):
        self._json({"error":"demo_read_only","message":message}, 403)

    def _admin_forbidden(self):
        self._json({"error":"admin_disabled","message":"当前公开演示未开放数据管理功能。请在本地启动或受保护的正式隧道中使用。"}, 403)

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length","0") or 0); raw = self.rfile.read(length) if length else b"{}"; return json.loads(raw.decode("utf-8"))

    def _file(self, path, download_name=None):
        path = Path(path); data = path.read_bytes(); disposition = self._attachment_header(download_name) if download_name else None
        self.send_response(200); self.send_header("Content-Type","application/octet-stream"); self.send_header("Content-Length",str(len(data)))
        if disposition: self.send_header("Content-Disposition", disposition)
        self.end_headers(); self.wfile.write(data)

    @staticmethod
    def _attachment_header(download_name):
        """Return an ASCII-safe RFC 5987 attachment header for Chinese names."""
        name = Path(str(download_name or "download")).name
        suffix = Path(name).suffix
        fallback = "download%s" % suffix if suffix else "download"
        return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            fallback, quote(name.encode("utf-8"), safe=""),
        )

    def _bytes(self, data, content_type, download_name=None):
        disposition = self._attachment_header(download_name) if download_name else None
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers(); self.wfile.write(data)

    def _static(self, path):
        if path.rstrip("/") in ("/cost-effectiveness", "/cost-analysis", "/cost_effectiveness"):
            item = (self.app.portal_config.get("services") or {}).get("cost_effectiveness_analysis") or {}
            if item.get("enabled") and item.get("url"):
                self._redirect(item["url"]); return
            self._json({"error":"analysis_unavailable", "message":"效费分析台未启动，请使用统一START入口。"},503); return
        if path in ("/admin","/admin/") and self.app.disable_admin:
            self._admin_forbidden(); return
        if path in ("/login", "/login.html"): rel = "login.html"
        elif path in ("/portal", "/portal/"): rel = "portal.html"
        elif path in ("/admin","/admin/"): rel = "admin.html"
        elif path in ("/effectiveness", "/effectiveness/"): rel = "effectiveness.html"
        elif path in ("/price", "/price/"): rel = "price.html"
        elif path in ("/price/deep", "/price/deep/"): rel = "deep_price/index.html"
        else: rel = "index.html" if path in ("", "/") else unquote(path.lstrip("/"))
        target = (self.app.static_dir / rel).resolve()
        if not str(target).startswith(str(self.app.static_dir.resolve())) or not target.is_file(): self._json({"error":"not_found"},404); return
        data = target.read_bytes(); ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200); self.send_header("Content-Type",ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype in ("application/javascript","application/json") else "")); self.send_header("Content-Length",str(len(data))); self.send_header("Cache-Control","no-store, no-cache, must-revalidate"); self.send_header("Pragma","no-cache"); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if handle_deep_price_http(self, 'GET'):
            return
        parsed = urlparse(self.path); path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path in ("/login", "/login.html") and self.app.auth_enabled and self._is_authenticated():
                self._redirect(self._safe_next((query.get("next") or [""])[0]) or "/portal"); return
            if self._auth_required(path):
                return
            if path == "/api/auth/status":
                self._json({
                    "auth_enabled": self.app.auth_enabled,
                    "authenticated": self._is_authenticated(),
                    "username": self._authenticated_user() if self._is_authenticated() else None,
                    "demo_read_only": self.app.demo_read_only,
                    "persistent_writes_enabled": not self.app.demo_read_only,
                })
            elif path == "/api/health":
                payload = {"status":"ok","version":"V21","mode":self.app.model_execution_mode if hasattr(self.app,"model_execution_mode") else "local","auth_required":self.app.auth_enabled,"demo_read_only":self.app.demo_read_only}
                if self._is_authenticated() or not self.app.auth_enabled:
                    payload.update({"admin_enabled":not self.app.disable_admin,"manifest":self.app.runtime.manifest(),"database":self.app.store.integrity_check()})
                self._json(payload)
            elif path == "/api/portal": self._json(self.app.portal())
            elif path == "/api/bootstrap": self._json(self.app.bootstrap())
            elif path == "/api/scenario-policy":
                self._json(self.app.scenario_policy.resolve({
                    "scenario": (query.get("scenario") or [None])[0],
                    "optimization_intensity": (query.get("optimization_intensity") or [None])[0],
                }))
            elif path == "/api/effectiveness-workbench/schema": self._json(self.app.effectiveness_workbench_schema())
            elif path == "/api/price-workbench/schema": self._json(self.app.price_workbench_schema())
            elif path.startswith("/api/generation-tasks/"):
                task_id = unquote(path.split("/api/generation-tasks/",1)[1]); task = self.app.generation_tasks.get(task_id); self._json(task if task else {"error":"not_found"}, 200 if task else 404)
            elif path.startswith("/api/agreements/"):
                agreement_id = unquote(path.split("/api/agreements/",1)[1]); item = self.app.agreement_detail(agreement_id,(query.get("session_id") or ["default"])[0],(query.get("protocol_id") or [None])[0]); self._json(item if item else {"error":"not_found"},200 if item else 404)
            elif path == "/api/saved": self._json({"items":self.app.saved_schemes()})
            elif path == "/api/requirements/versions": self._json({"items":self.app.requirement_versions.list(int((query.get("limit") or [50])[0]))})
            elif path == "/api/final-decisions": self._json({"items":self.app.final_decisions(int((query.get("limit") or [100])[0]))})
            elif path.startswith("/api/saved/"):
                scheme_id = unquote(path.split("/api/saved/",1)[1]); item = self.app.saved_detail(scheme_id,(query.get("protocol_id") or [None])[0]); self._json(item if item else {"error":"not_found"},200 if item else 404)
            elif path.startswith("/api/admin/") and self.app.disable_admin: self._admin_forbidden()
            elif path == "/api/admin/snapshot": self._json(self.app.admin_snapshot())
            elif path == "/api/admin/portal-config": self._json(self.app.portal_config)
            elif path == "/api/admin/model-service-settings": self._json(self.app.price_output_settings())
            elif path == "/api/admin/backups": self._json({"items":self.app.store.list_backups()})
            elif path == "/api/admin/database/download": self._file(self.app.store.db_path, "protocol_demo.db")
            elif path == "/api/admin/export-json": self._json(self.app.store.export_json())
            elif path == "/api/admin/product-releases/package":
                release = self.app.product_releases.get((query.get("release_id") or [""])[0])
                filename = "%s_%s.iprelease.json" % (release["product_code"], release["release_id"])
                self._bytes(self.app.product_releases.export_package(release["release_id"]), "application/json; charset=utf-8", filename)
            elif path == "/api/admin/product-releases/module/template":
                release_id = (query.get("release_id") or [""])[0]
                section = (query.get("section") or [""])[0]
                self._bytes(
                    self.app.product_releases.module_template(release_id, section),
                    "text/csv; charset=utf-8", "%s_template.csv" % section,
                )
            elif path == "/api/admin/product-releases/maintenance/workbook":
                release = self.app.product_releases.get((query.get("release_id") or [""])[0])
                filename = "%s_%s_维护工作簿.xlsx" % (release["product_code"], release["release_id"])
                self._bytes(
                    self.app.data_master.export_snapshot(release["data"]),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename,
                )
            elif path.startswith("/api/admin/product-releases/"):
                release_id = unquote(path.split("/api/admin/product-releases/", 1)[1])
                self._json(self.app.product_releases.get(release_id))
            elif path == "/api/admin/wide-import/template": self._bytes(self.app.wide_table_parser().template_csv(), "text/csv; charset=utf-8", "protocol_wide_table_template.csv")
            elif path == "/api/admin/datamaster/template": self._bytes(self.app.data_master.template(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "DataMaster_Template.xlsx")
            elif path == "/api/admin/datamaster/current": self._bytes(self.app.data_master.export_current(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "DataMaster_Current.xlsx")
            elif path == "/api/admin/semantic-package":
                package, filename = self.app.semantic_snapshot.package()
                self._bytes(package, "application/zip", filename)
            elif path.startswith("/api/"): self._json({"error":"not_found","path":path},404)
            else: self._static(path)
        except Exception as exc: traceback.print_exc(); self._json({"error":type(exc).__name__,"message":str(exc)},500)

    def do_POST(self):
        if handle_deep_price_http(self, 'POST'):
            return
        path = urlparse(self.path).path
        try:
            request = self._body()
            if path == "/api/auth/login":
                client_id = str(self.headers.get("CF-Connecting-IP") or self.client_address[0])
                allowed, retry_after = self.app.login_allowed(client_id)
                if not allowed:
                    self._json({"error":"too_many_attempts","message":"登录失败次数过多，请%d秒后重试。" % retry_after}, 429); return
                username = str(request.get("username") or "")
                password = str(request.get("password") or "")
                valid = hmac.compare_digest(username, self.app.auth_username) and hmac.compare_digest(password, self.app.auth_password)
                if not valid:
                    self.app.record_login_failure(client_id)
                    time.sleep(0.25)
                    self._json({"error":"invalid_credentials","message":"账号或密码不正确。"}, 401); return
                self.app.clear_login_failures(client_id)
                token = self.app.make_auth_token(username)
                redirect = self._safe_next(request.get("next")) or "/portal"
                data = json.dumps({"authenticated":True,"username":username,"redirect":redirect}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length",str(len(data)))
                self.send_header("Cache-Control","no-store")
                self._set_auth_cookie(token, self.app.auth_ttl_seconds)
                self.end_headers(); self.wfile.write(data); return
            if path == "/api/auth/logout":
                data = json.dumps({"authenticated":False,"redirect":"/login"}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length",str(len(data)))
                self.send_header("Cache-Control","no-store")
                self._set_auth_cookie("", 0)
                self.end_headers(); self.wfile.write(data); return
            if self._auth_required(path):
                return
            if self.app.demo_read_only and path in self.WRITE_PATHS:
                self._demo_forbidden(); return
            if self.app.demo_read_only and path.startswith("/api/saved/"):
                self._demo_forbidden(); return
            if path == "/api/recommend": self._json(self.app.recommend(request))
            elif path == "/api/generation/request": self._json(self.app.request_generation(request))
            elif path == "/api/generate-live": self._json(self.app.generate_live(request))
            elif path == "/api/evaluate": self._json(self.app.evaluate(request))
            elif path == "/api/improve": self._json(self.app.improve(request))
            elif path == "/api/effectiveness-workbench/evaluate": self._json(self.app.effectiveness_workbench_evaluate(request))
            elif path == "/api/price-workbench/predict": self._json(self.app.price_workbench_predict(request))
            elif path == "/api/save-scheme": self._json(self.app.save(request))
            elif path == "/api/requirements/version": self._json(self.app.capture_requirement_version(request, "manual"))
            elif path == "/api/requirements/restore": self._json(self.app.requirement_versions.restore(request.get("version_id"), created_by="operator"))
            elif path == "/api/final-decisions": self._json(self.app.save_final_decision(request))
            elif path == "/api/relaxation-suggestions": self._json(self.app.relaxation_suggestions(request))
            elif path == "/api/saved/export":
                self._json({"items": self.app.export_saved_schemes(request.get("scheme_ids") or [])})
            elif path.startswith("/api/saved/") and path.endswith("/recommendation-eligibility"):
                scheme_id = unquote(path.split("/api/saved/", 1)[1].split("/", 1)[0])
                self._json(self.app.set_saved_recommendation_eligibility(scheme_id, _boolean(request.get("enabled"), False)))
            elif path.startswith("/api/saved/") and path.endswith("/training-candidate"):
                scheme_id = unquote(path.split("/api/saved/", 1)[1].split("/", 1)[0])
                self._json(self.app.set_saved_training_candidate(scheme_id, _boolean(request.get("enabled"), False)))
            elif path == "/api/clear-live-generated":
                session_id = request.get("session_id") or "default"
                self.app.generation_tasks.invalidate_session(session_id)
                self.app.sessions.clear(session_id)
                self._json({"cleared":True})
            elif path.startswith("/api/admin/") and self.app.disable_admin: self._admin_forbidden()
            elif path == "/api/admin/upsert":
                result = self.app.store.admin_upsert(request.get("section"), request.get("item") or {})
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/portal-config": self._json(self.app.save_portal_config(request))
            elif path == "/api/admin/model-service-settings": self._json(self.app.save_model_service_settings(request))
            elif path == "/api/admin/conditional-constraint/upsert":
                result = self.app.store.upsert_conditional_template(request.get("template") or {})
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/conditional-constraint/delete":
                result = self.app.store.delete_conditional_template(request.get("constraint_group"))
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/delete":
                result = self.app.store.admin_delete(request.get("section"), str(request.get("id")))
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/toggle":
                result = self.app.store.admin_toggle(request.get("section"), str(request.get("id")), bool(request.get("enabled")))
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/purge":
                result = self.app.store.admin_purge(request.get("section"), str(request.get("id")))
                self.app.on_business_data_changed()
                self._json(result)
            elif path == "/api/admin/backup": self._json(self.app.store.create_backup("manual"))
            elif path == "/api/admin/restore-backup":
                result = self.app.store.restore_backup(request.get("name")); self.app.on_business_data_changed(); self._json(result)
            elif path == "/api/admin/upload-database":
                data = base64.b64decode(request.get("database_base64") or ""); target = self.app.root / "uploads" / ("restore_%d.db" % int(time.time()*1000)); target.write_bytes(data)
                try:
                    result = self.app.store.restore_uploaded(target); self.app.on_business_data_changed(); self._json(result)
                finally:
                    try: target.unlink()
                    except OSError: pass
            elif path == "/api/admin/models/replace": self._json(self.app.replace_models(request))
            elif path == "/api/admin/product-releases/create":
                self._json(self.app.product_releases.create(
                    request.get("product_code"), request.get("product_name"),
                    request.get("product_description") or "", request.get("seed_schema", False),
                ))
            elif path == "/api/admin/product-releases/clone-current":
                self._json(self.app.product_releases.clone_current())
            elif path == "/api/admin/product-releases/package/import":
                self._json(self.app.import_product_release_package(request))
            elif path == "/api/admin/product-releases/history/preview":
                self._json(self.app.historical_product_onboarding(request, create=False))
            elif path == "/api/admin/product-releases/history/create":
                self._json(self.app.historical_product_onboarding(request, create=True))
            elif path == "/api/admin/product-releases/maintenance/import":
                self._json(self.app.import_release_maintenance_workbook(request))
            elif path == "/api/admin/product-releases/delete":
                self._json(self.app.product_releases.delete(request.get("release_id")))
            elif path == "/api/admin/product-releases/module/preview":
                self._json(self.app.parse_release_module(request, stage=False))
            elif path == "/api/admin/product-releases/module/stage":
                self._json(self.app.parse_release_module(request, stage=True))
            elif path == "/api/admin/product-releases/section/save":
                self._json(self.app.product_releases.set_section(
                    request.get("release_id"), request.get("section"), request.get("items")
                ))
            elif path == "/api/admin/product-releases/validate":
                self._json(self.app.product_releases.validate(request.get("release_id")))
            elif path == "/api/admin/product-releases/activate":
                self._json(self.app.activate_product_release(request.get("release_id")))
            elif path == "/api/admin/wide-import/preview": self._json(self.app.parse_wide_table(request, commit=False))
            elif path == "/api/admin/wide-import/commit":
                result = self.app.parse_wide_table(request, commit=True); self.app.on_business_data_changed(); self._json(result)
            elif path == "/api/admin/datamaster/preview":
                data = base64.b64decode(request.get("file_base64") or ""); self._json(self.app.data_master.parse(request.get("filename") or "DataMaster.xlsx", data))
            elif path == "/api/admin/datamaster/commit":
                data = base64.b64decode(request.get("file_base64") or "")
                report = self.app.data_master.parse(request.get("filename") or "DataMaster.xlsx", data)
                commit_result = self.app.data_master.commit(report)
                runtime_readiness = self.app.on_business_data_changed()
                self._json(dict(report, commit_result=commit_result, runtime_readiness=runtime_readiness))
            else: self._json({"error":"not_found","path":path},404)
        except ModelInputError as exc: self._json({"error":"model_input_error","message":str(exc),"model_kind":exc.model_kind,"missing_features":exc.missing_features},400)
        except ValueError as exc: self._json({"error":"validation_error","message":str(exc)},400)
        except sqlite3.IntegrityError as exc: self._json({"error":"database_validation_error","message":"数据未保存：%s" % exc},400)
        except Exception as exc: traceback.print_exc(); self._json({"error":type(exc).__name__,"message":str(exc)},500)


def create_server(root, host="127.0.0.1", port=17891):
    app = Application(root); Handler.app = app; return ThreadingHTTPServer((host,int(port)),Handler)
