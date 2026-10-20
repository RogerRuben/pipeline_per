# -*- coding: utf-8 -*-
"""Export the price models that were actually saved or fitted.

The previous V19.6 exporter assumed a fixed seven-model ensemble. This hotfix
uses dynamic discovery instead:

* ``saved_files``: scan the model directory and bundle exactly the discovered
  pickle files;
* ``namespace``: bundle exactly the fitted model variables currently present;
* ``auto``: prefer saved pickle files, then fall back to present variables.

The resulting bundle stores only the selected models, and the service predicts
with exactly the members declared by the bundle. No model name is mandatory.
"""
from __future__ import print_function

import json
import math
import pickle
from pathlib import Path

try:
    from .native_bundle import save_bundle, environment_versions, file_sha256
except ImportError:
    from native_bundle import save_bundle, environment_versions, file_sha256


CANONICAL_MODEL_ORDER = [
    "lasso", "ridge", "xgboost", "random_forest", "extra_trees", "svr", "gbdt"
]

DEFAULT_MODEL_VARIABLES = {
    "lasso": "lasso_model",
    "ridge": "ridge_model",
    "xgboost": "xgb_model",
    "random_forest": "rf_model",
    "extra_trees": "et_model",
    "svr": "svr_model",
    "gbdt": "gbdt_model",
}

DEFAULT_MODEL_FILE_CANDIDATES = {
    "lasso": ["lasso.pkl", "lasso_model.pkl"],
    "ridge": ["ridge.pkl", "ridge_model.pkl"],
    "xgboost": ["xgb.pkl", "xgboost.pkl", "xgb_model.pkl"],
    "random_forest": ["rf.pkl", "random_forest.pkl", "rf_model.pkl"],
    "extra_trees": ["et.pkl", "extra_trees.pkl", "et_model.pkl"],
    "svr": ["svr.pkl", "svr_model.pkl"],
    "gbdt": ["gbdt.pkl", "gbdt_model.pkl"],
}


def _feature_schema(feature_order, X_train, field_metadata=None):
    metadata = dict(field_metadata or {})
    result = []
    for key in feature_order:
        values = X_train[key]
        info = dict(metadata.get(str(key), {}))
        result.append({
            "field_name": str(info.get("field_name") or key),
            "source_column": str(key),
            "field_label": str(info.get("field_label") or key),
            "dtype": str(info.get("dtype") or "number"),
            "unit": str(info.get("unit") or ""),
            "required": bool(info.get("required", True)),
            "missing_policy": str(info.get("missing_policy") or "reject"),
            "training_min": float(values.min()),
            "training_max": float(values.max()),
            "training_mean": float(values.mean()),
            "default_value": info.get("default_value"),
            "allowed_values": info.get("allowed_values"),
            "category_mapping": info.get("category_mapping"),
            "source": str(info.get("source") or "product_parameter"),
            "parser": info.get("parser"),
        })
    return result


def _training_frame(namespace):
    """Return the unscaled training frame when the notebook exposes one.

    Older notebooks overwrite ``X_train`` with its scaled values.  They normally
    retain either ``train_df`` or ``df_X``; using that frame keeps the service
    schema ranges and defaults meaningful without asking an operator to retype
    field metadata.
    """
    scaled = namespace.get("X_train")
    if scaled is None or not hasattr(scaled, "columns"):
        raise ValueError("Notebook中必须保留带英文表头的X_train")
    columns = [str(x) for x in scaled.columns]
    for name in ("X_train_original", "train_df", "df_X"):
        candidate = namespace.get(name)
        if candidate is None or not hasattr(candidate, "columns"):
            continue
        if all(column in candidate.columns for column in columns):
            return candidate.loc[:, columns]
    return scaled


def _ordered_names(names):
    names = list(names)
    canonical = [name for name in CANONICAL_MODEL_ORDER if name in names]
    custom = sorted(name for name in names if name not in CANONICAL_MODEL_ORDER)
    return canonical + custom


def discover_saved_model_files(directory=None, saved_model_files=None):
    """Return discovered model files in deterministic model order.

    ``saved_model_files`` may map any model name to a filename/path. When it is
    omitted, known filenames are scanned. Unknown ``*.pkl`` files are not loaded
    automatically because arbitrary pickle loading is unsafe and their model
    names cannot be inferred reliably.
    """
    root = Path(directory or Path.cwd()).resolve()
    explicit = dict(saved_model_files or {})
    found = {}

    if explicit:
        for name, raw_path in explicit.items():
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            if path.is_file():
                found[str(name)] = path.resolve()
        return [(name, found[name]) for name in _ordered_names(found)]

    for name in CANONICAL_MODEL_ORDER:
        for filename in DEFAULT_MODEL_FILE_CANDIDATES[name]:
            path = root / filename
            if path.is_file():
                found[name] = path.resolve()
                break
    return [(name, found[name]) for name in _ordered_names(found)]


def _load_saved_models(discovered):
    models = {}
    sources = []
    used_paths = set()
    for name, path in discovered:
        key = str(path).lower()
        if key in used_paths:
            raise ValueError("同一个模型文件不能绑定到多个模型名: %s" % path)
        used_paths.add(key)
        with Path(path).open("rb") as handle:
            model = pickle.load(handle)
        if not hasattr(model, "predict"):
            raise ValueError("模型文件%s没有predict方法" % path)
        models[name] = model
        sources.append({
            "name": name,
            "source": "saved_pickle",
            "file": Path(path).name,
            "absolute_file_at_export": str(Path(path)),
            "sha256": file_sha256(path),
            "class": "%s.%s" % (type(model).__module__, type(model).__name__),
        })
    return models, sources


def _discover_namespace_models(namespace, model_variables=None):
    variables = dict(DEFAULT_MODEL_VARIABLES)
    variables.update(model_variables or {})
    models = {}
    sources = []
    for name in _ordered_names(variables):
        var_name = variables[name]
        model = namespace.get(var_name)
        if model is None:
            continue
        if not hasattr(model, "predict"):
            continue
        models[name] = model
        sources.append({
            "name": name,
            "source": "notebook_namespace",
            "variable": var_name,
            "class": "%s.%s" % (type(model).__module__, type(model).__name__),
        })
    return models, sources


def _select_models(models, sources, ensemble_model_names=None, strict=False):
    available = _ordered_names(models)
    if ensemble_model_names is None:
        selected = list(available)
    else:
        requested = [str(name) for name in ensemble_model_names]
        missing = [name for name in requested if name not in models]
        if strict and missing:
            raise ValueError("显式指定的模型不存在: %s" % ",".join(missing))
        selected = [name for name in requested if name in models]
    if not selected:
        raise ValueError("没有发现可导出的价格预测模型")
    selected_models = dict((name, models[name]) for name in selected)
    selected_sources = [item for item in sources if item.get("name") in selected_models]
    return selected, selected_models, selected_sources, available


def _as_weight_list(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict((str(k), float(v)) for k, v in raw.items())
    try:
        return [float(x) for x in list(raw)]
    except Exception:
        raise ValueError("无法解析集成权重")


def _resolve_weights(namespace, selected_names, available_names, ensemble_weights=None):
    raw = _as_weight_list(ensemble_weights)
    source = "explicit"
    if raw is None:
        raw = _as_weight_list(namespace.get("weights"))
        source = "notebook_weights"

    if isinstance(raw, dict):
        missing = [name for name in selected_names if name not in raw]
        if missing:
            raise ValueError("权重字典缺少模型: %s" % ",".join(missing))
        values = [raw[name] for name in selected_names]
    elif isinstance(raw, list):
        if len(raw) == len(selected_names):
            values = list(raw)
        elif len(raw) == len(CANONICAL_MODEL_ORDER):
            mapped = dict(zip(CANONICAL_MODEL_ORDER, raw))
            missing = [name for name in selected_names if name not in mapped]
            if missing:
                raise ValueError("七模型权重无法映射自定义模型: %s" % ",".join(missing))
            values = [mapped[name] for name in selected_names]
            source += "_canonical_subset"
        elif len(raw) == len(available_names):
            mapped = dict(zip(available_names, raw))
            values = [mapped[name] for name in selected_names]
            source += "_available_subset"
        else:
            # A stale Notebook cell often keeps weights for models that were
            # not trained or not saved in the latest run.  In automatic mode
            # this must not make an otherwise valid independent-service bundle
            # impossible to export.  Explicit caller weights remain strict.
            if ensemble_weights is None:
                values = [1.0] * len(selected_names)
                source = "equal_weights_stale_notebook_weights_ignored"
            else:
                raise ValueError(
                    "显式权重数量%s与实际导出模型数量%s不一致；请传入与模型一一对应的列表或{name: weight}字典"
                    % (len(raw), len(selected_names))
                )
    else:
        values = [1.0] * len(selected_names)
        source = "equal_weights_no_saved_weights"

    if any((not math.isfinite(float(x))) or float(x) < 0 for x in values):
        raise ValueError("集成权重必须是非负有限数")
    total = sum(float(x) for x in values)
    if total <= 0:
        raise ValueError("集成权重总和必须大于0")
    return [float(x) / total for x in values], source


def _recalculate_residual(namespace, models, names, weights, preprocessor, output_transform):
    residual = {"space": "native", "half_width_native": 0.0, "coverage": 0.95, "source": "not_available"}
    if namespace.get("X_test") is None or namespace.get("y_test") is None:
        return residual
    try:
        import numpy as np
        prepared = namespace["X_test"]
        if preprocessor is not None:
            prepared = preprocessor.transform(prepared)
        member_predictions = []
        for name in names:
            raw = np.asarray(models[name].predict(prepared), dtype=float)
            if output_transform == "log":
                values = np.exp(raw)
            else:
                values = raw
            member_predictions.append(values)
        deployed = np.zeros_like(member_predictions[0], dtype=float)
        for values, weight in zip(member_predictions, weights):
            deployed += values * float(weight)
        actual = np.asarray(namespace["y_test"], dtype=float)
        errors = actual - deployed
        residual["half_width_native"] = float(np.quantile(np.abs(errors), 0.95))
        residual["sample_count"] = int(len(errors))
        residual["source"] = "recomputed_from_selected_models"
    except Exception as exc:
        residual["source"] = "recalculation_failed"
        residual["warning"] = str(exc)
    return residual


def _required_modules(models, preprocessor):
    roots = set()
    for obj in list(models.values()) + ([preprocessor] if preprocessor is not None else []):
        root = str(type(obj).__module__).split(".")[0]
        if root and root not in ("builtins", "__main__"):
            roots.add(root)
    return sorted(roots)


def export_from_notebook(namespace, output="price_native_bundle.pkl", product_code=None,
                         product_name="", model_version="price-native-v1",
                         target_divisor_to_wan=1.0, field_metadata=None,
                         model_variables=None, ensemble_model_names=None,
                         ensemble_weights=None, strict=False,
                         model_source="auto", saved_model_dir=None,
                         saved_model_files=None, model_output_transform="log"):
    """Export exactly the models discovered by the selected source policy.

    ``model_source`` values:

    * ``saved_files``: use only pickle files found in ``saved_model_dir``;
    * ``namespace``: use only present model variables;
    * ``auto``: use saved files when at least one is found, otherwise namespace.

    ``strict`` applies only when ``ensemble_model_names`` is explicitly supplied.
    It no longer means that seven hard-coded models are required.
    """
    ns = namespace
    training_frame = _training_frame(ns)
    product_code = product_code or ns.get("PRODUCT_CODE") or ns.get("product_code")
    if not str(product_code or "").strip():
        raise ValueError("请在Notebook设置PRODUCT_CODE；这是唯一必须手工确认的成品信息")
    preprocessor = ns.get("price_scaler", ns.get("model_scaler", ns.get("scaler")))

    mode = str(model_source or "auto").strip().lower()
    if mode not in ("auto", "saved_files", "namespace"):
        raise ValueError("model_source必须是auto、saved_files或namespace")

    discovered_files = discover_saved_model_files(saved_model_dir, saved_model_files)
    if mode in ("auto", "saved_files") and discovered_files:
        models, sources = _load_saved_models(discovered_files)
        discovery_mode = "saved_files"
    elif mode == "saved_files":
        raise ValueError("没有在%s发现受支持的已保存pkl模型" % Path(saved_model_dir or Path.cwd()).resolve())
    else:
        models, sources = _discover_namespace_models(ns, model_variables)
        discovery_mode = "namespace"

    names, models, sources, available_names = _select_models(
        models, sources, ensemble_model_names=ensemble_model_names, strict=bool(strict)
    )
    weights, weight_source = _resolve_weights(ns, names, available_names, ensemble_weights)

    feature_order = [str(x) for x in ns["X_train"].columns]
    schema = _feature_schema(feature_order, training_frame, field_metadata)
    source_to_field = dict((x["source_column"], x["field_name"]) for x in schema)
    if len(set(source_to_field.values())) != len(source_to_field):
        raise ValueError("field_metadata产生重复field_name")
    api_order = [source_to_field[x] for x in feature_order]
    for item in schema:
        item["field_name"] = source_to_field[item["source_column"]]

    residual = _recalculate_residual(
        ns, models, names, weights, preprocessor, str(model_output_transform or "log")
    )
    omitted = [name for name in available_names if name not in names]
    bundle = {
        "product_code": str(product_code),
        "product_name": str(product_name or product_code),
        "model_version": str(model_version),
        "feature_order": api_order,
        "source_feature_order": feature_order,
        "feature_schema": schema,
        "preprocessor": preprocessor,
        "models": models,
        "ensemble": {
            "aggregation": "weighted_mean_price",
            "members": [{"name": name, "weight": weight} for name, weight in zip(names, weights)],
        },
        "target_transform": str(model_output_transform or "log"),
        "model_output_transform": str(model_output_transform or "log"),
        "target_divisor_to_wan": float(target_divisor_to_wan),
        "residual_calibration": residual,
        "training_environment": environment_versions(),
        "required_modules": _required_modules(models, preprocessor),
        "model_sources": sources,
        "export_notes": {
            "discovery_mode": discovery_mode,
            "detected_model_count": len(available_names),
            "detected_models": available_names,
            "included_model_count": len(names),
            "included_models": names,
            "omitted_models": omitted,
            "weight_source": weight_source,
            "strict_explicit_selection": bool(strict),
            "uses_joblib": False,
            "prediction_equivalence": "same saved estimators + same scaler + selected weights + same output transform",
        },
    }
    path = save_bundle(output, bundle)
    manifest = {
        "format_version": "price-native-bundle-1.0",
        "product_code": bundle["product_code"],
        "product_name": bundle["product_name"],
        "model_version": bundle["model_version"],
        "feature_order": bundle["feature_order"],
        "model_count": len(names),
        "model_names": names,
        "ensemble": bundle["ensemble"],
        "model_sources": sources,
        "export_notes": bundle["export_notes"],
        "residual_calibration": residual,
        "training_environment": bundle["training_environment"],
        "required_modules": bundle.get("required_modules") or [],
        "bundle_file": path.name,
        "bundle_sha256": file_sha256(path),
    }
    Path(str(path) + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return bundle


def export_price_service_bundle(namespace):
    """One-call notebook export for the independent price service.

    Required notebook variable: ``PRODUCT_CODE``.  The exporter discovers any
    fitted supported estimator variables that actually exist, uses the English
    ``X_train`` headers as stable API field IDs, and installs the bundle in the
    current project. Optional overrides remain available as notebook variables
    rather than a long function call.
    """
    root = Path(__file__).resolve().parents[2]
    output = namespace.get("PRICE_BUNDLE_OUTPUT") or (
        root / "services" / "price_service" / "model" / "price_native_bundle.pkl"
    )
    return export_from_notebook(
        namespace,
        output=output,
        product_code=namespace.get("PRODUCT_CODE"),
        product_name=namespace.get("PRODUCT_NAME", ""),
        model_version=namespace.get("PRICE_MODEL_VERSION", "price-native-notebook"),
        target_divisor_to_wan=namespace.get("TARGET_DIVISOR_TO_WAN", 1.0),
        field_metadata=namespace.get("FIELD_METADATA"),
        ensemble_model_names=namespace.get("PRICE_ENSEMBLE_MODELS"),
        ensemble_weights=namespace.get("PRICE_ENSEMBLE_WEIGHTS"),
        model_source=namespace.get("PRICE_MODEL_SOURCE", "namespace"),
        saved_model_dir=namespace.get("PRICE_SAVED_MODEL_DIR"),
        saved_model_files=namespace.get("PRICE_SAVED_MODEL_FILES"),
        model_output_transform=namespace.get("PRICE_OUTPUT_TRANSFORM", "log"),
    )


if __name__ == "__main__":
    print("请从Notebook最后一个单元格调用export_from_notebook(globals(), ...)")
