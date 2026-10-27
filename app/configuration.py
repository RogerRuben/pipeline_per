# -*- coding: utf-8 -*-
"""Application configuration with one documented precedence order.

Defaults < config/model_services.json < environment variables.
"""
from __future__ import print_function

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .price_output import validate_price_output_config


DEFAULT_MODEL_SERVICE_CONFIG = {
    "execution_mode": "local",
    "price_service_url": "http://127.0.0.1:18101",
    "effectiveness_service_url": "http://127.0.0.1:18102",
    "timeout_seconds": 15.0,
    "local_fallback": False,
    "batch_size": 100,
    "price_output": {"unit": "wan_yuan", "scale": 1.0},
}

DEFAULT_SERVICE_PORTAL_CONFIG = {
    "title": "工业技术协议智能系统",
    "services": {
        "recommendation": {"label": "方案智能推荐", "description": "根据设计要求筛选、推荐并生成候选方案", "url": "/", "visible": True, "enabled": True},
        "quick_price": {"label": "简易价格预测", "description": "快速估算成品参考价格", "url": "/price", "visible": True, "enabled": True},
        "cost_effectiveness_analysis": {"label":"效费分析台","description":"比较方案的价格、效能与帕累托前沿","url":"http://127.0.0.1:17000","visible":True,"enabled":True},
        "advanced_price": {"label": "价格深度分析", "description": "专业价格预测与综合分析", "url": "", "visible": True, "enabled": False},
        "effectiveness": {"label": "简易效能评价", "description": "评估综合效能与主要风险", "url": "/effectiveness", "visible": True, "enabled": True},
        "admin": {"label": "数据管理中心", "description": "维护成品数据、指标、规则与系统设置", "url": "/admin", "visible": True, "enabled": True},
    },
}


def _boolean(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_model_service_config(root):
    root = Path(root)
    result = dict(DEFAULT_MODEL_SERVICE_CONFIG)
    path = root / "config" / "model_services.json"
    raw = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config/model_services.json必须是JSON对象")
        for key in result:
            if key in raw:
                result[key] = raw[key]

    env_map = {
        "execution_mode": "IPDEMO_MODEL_EXECUTION_MODE",
        "price_service_url": "IPDEMO_PRICE_SERVICE_URL",
        "effectiveness_service_url": "IPDEMO_EFFECT_SERVICE_URL",
        "timeout_seconds": "IPDEMO_MODEL_SERVICE_TIMEOUT",
        "local_fallback": "IPDEMO_MODEL_SERVICE_FALLBACK",
        "batch_size": "IPDEMO_MODEL_SERVICE_BATCH_SIZE",
    }
    for key, env_name in env_map.items():
        if env_name in os.environ:
            result[key] = os.environ[env_name]

    result["execution_mode"] = str(result.get("execution_mode") or "local").strip().lower()
    result["price_service_url"] = str(result["price_service_url"]).rstrip("/")
    result["effectiveness_service_url"] = str(result["effectiveness_service_url"]).rstrip("/")
    result["timeout_seconds"] = float(result["timeout_seconds"])
    result["local_fallback"] = _boolean(result.get("local_fallback"), False)
    result["batch_size"] = max(1, int(result["batch_size"]))
    price_output = dict(result.get("price_output") or {})
    configured_price_output = validate_price_output_config(price_output)
    price_output_source = "config_file" if path.is_file() and isinstance(raw.get("price_output"), dict) else "default"
    if "IPDEMO_PRICE_OUTPUT_UNIT" in os.environ:
        price_output["unit"] = os.environ["IPDEMO_PRICE_OUTPUT_UNIT"]
        price_output_source = "environment"
    if "IPDEMO_PRICE_OUTPUT_SCALE" in os.environ:
        price_output["scale"] = os.environ["IPDEMO_PRICE_OUTPUT_SCALE"]
        price_output_source = "environment"
    result["price_output"] = validate_price_output_config(price_output)
    result["price_output_configured"] = configured_price_output
    result["price_output_source"] = price_output_source
    result["price_output_environment_override"] = price_output_source == "environment"
    result["config_path"] = str(path)
    result["config_loaded"] = path.is_file()
    return result


def save_price_output_config(root, price_output):
    """Atomically merge only ``price_output`` into model_services.json."""
    root = Path(root)
    path = root / "config" / "model_services.json"
    validated = validate_price_output_config(price_output)
    raw = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config/model_services.json必须是JSON对象")
    raw["price_output"] = validated
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if path.is_file():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = path.with_name("model_services.%s.bak.json" % stamp)
        shutil.copy2(str(path), str(backup_path))
    fd, temp_name = tempfile.mkstemp(prefix="model_services.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(path))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"price_output": validated, "config_path": str(path),
            "backup_path": str(backup_path) if backup_path else None}


def _load_json_object(path, default, label):
    result = json.loads(json.dumps(default, ensure_ascii=False))
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("%s必须是JSON对象" % label)
        result.update(raw)
    result["config_path"] = str(path)
    result["config_loaded"] = path.is_file()
    return result


def load_service_portal_config(root):
    path = Path(root) / "config" / "service_portal.json"
    raw = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config/service_portal.json必须是JSON对象")
    raw_services = raw.get("services", {})
    if not isinstance(raw_services, dict):
        raise ValueError("config/service_portal.json的services必须是JSON对象")
    result = json.loads(json.dumps(DEFAULT_SERVICE_PORTAL_CONFIG, ensure_ascii=False))
    for key, value in raw.items():
        if key != "services":
            result[key] = value
    services = dict(result["services"])
    for key, value in raw_services.items():
        if isinstance(value, dict):
            services[key] = dict(services.get(key) or {}, **value)
    validated = {}
    for key, value in services.items():
        if not isinstance(value, dict):
            continue
        key, item = str(key), dict(value)
        enabled = _boolean(item.get("enabled"), True)
        visible = _boolean(item.get("visible"), True)
        url = str(item.get("url") or "").strip()
        if url:
            parsed = urlparse(url)
            local = url.startswith("/") and not url.startswith("//") and not parsed.scheme and not parsed.netloc
            external = parsed.scheme in ("http", "https") and bool(parsed.netloc)
            if ("\\" in url or any(ord(char) < 32 for char in url) or not (local or external)):
                raise ValueError("config/service_portal.json服务%s的url不安全或无效" % key)
        elif enabled:
            raise ValueError("config/service_portal.json服务%s启用时必须配置url" % key)
        item["description"] = str(item.get("description") or "").strip()
        item["url"], item["enabled"], item["visible"] = url, enabled, visible
        validated[key] = item
    result["services"] = validated
    result["config_path"] = str(path)
    result["config_loaded"] = path.is_file()
    return result


def load_workbench_defaults(root):
    path = Path(root) / "config" / "workbench_defaults.json"
    return _load_json_object(path, {"historical_example_agreement_id": ""}, "config/workbench_defaults.json")
