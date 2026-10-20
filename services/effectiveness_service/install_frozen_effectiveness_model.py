# -*- coding: utf-8 -*-
"""Verify and install a frozen model ZIP exported by the V11 expert program."""
from __future__ import print_function

import argparse
import hashlib
import json
import shutil
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FORMAT_VERSION = "effectiveness-frozen-runtime-package-1.0"
MANIFEST_NAME = "effectiveness_runtime_manifest.json"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 768 * 1024 * 1024


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(name):
    value = str(name or "").replace("\\", "/")
    path = Path(value)
    if not value or value.startswith("/") or path.is_absolute() or ".." in path.parts:
        raise ValueError("冻结模型ZIP包含不安全路径: %s" % name)
    return path


def _extract_verified(package_path, stage):
    total = 0
    with zipfile.ZipFile(str(package_path), "r") as archive:
        names = set()
        for info in archive.infolist():
            relative = _safe_relative(info.filename)
            if info.is_dir():
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError("冻结模型ZIP单文件过大: %s" % info.filename)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("冻结模型ZIP解压后总大小超过限制")
            normalized = relative.as_posix()
            if normalized in names:
                raise ValueError("冻结模型ZIP包含重复路径: %s" % normalized)
            names.add(normalized)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    manifest_path = stage / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError("冻结模型ZIP缺少%s" % MANIFEST_NAME)
    return manifest_path


def install_frozen_package(package_path, output_dir, expected_product_code=None):
    package_path = Path(package_path).resolve()
    output = Path(output_dir).resolve()
    if not package_path.is_file() or package_path.suffix.lower() != ".zip":
        raise ValueError("请选择专家软件导出的effectiveness_model_*.zip")
    if output == package_path.parent or output in package_path.parents:
        raise ValueError("输出目录不能覆盖冻结模型ZIP所在目录")
    stage = output.parent / (".%s.installing-%s" % (output.name, uuid.uuid4().hex[:8]))
    backup = None
    stage.mkdir(parents=True, exist_ok=False)
    try:
        manifest_path = _extract_verified(package_path, stage)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format_version") != FORMAT_VERSION:
            raise ValueError("不是V11冻结效能模型包: %s" % manifest.get("format_version"))
        declared_paths = set()
        for item in manifest.get("files") or []:
            relative = _safe_relative(item.get("path"))
            declared_paths.add(relative.as_posix())
            target = stage / relative
            if not target.is_file():
                raise ValueError("冻结模型包缺少文件: %s" % relative.as_posix())
            if str(item.get("sha256") or "").lower() != _sha256(target).lower():
                raise ValueError("冻结模型包文件摘要校验失败: %s" % relative.as_posix())
        required_declared = {
            str(manifest.get("model") or "").replace("\\", "/"),
            "source/coupling_model.py", "source/feasibility_model.py",
            "source/frozen_effectiveness_model.py", "source/interactive_project_app.py",
            "source/preference_models.py", "source/project_excel.py",
            "source/requirement_model.py",
        }
        missing_declarations = sorted(required_declared - declared_paths)
        if missing_declarations:
            raise ValueError(
                "冻结模型包运行文件未纳入SHA-256清单: %s" %
                "、".join(missing_declarations)
            )

        # Import only after every declared executable/data file has passed SHA-256.
        from services.effectiveness_service.app import backend_from_package
        backend = backend_from_package(manifest_path)
        expected = str(expected_product_code or "").strip()
        if expected and backend.product_code != expected:
            raise ValueError(
                "冻结模型product_code为%r，与期望值%r不一致" %
                (backend.product_code, expected)
            )
        schema = backend.schema()
        sample = {}
        for field in schema.get("fields") or []:
            allowed = field.get("allowed_values") or []
            lo, hi = field.get("generation_min"), field.get("generation_max")
            if allowed:
                value = allowed[0]
            elif lo is not None and hi is not None:
                value = (float(lo) + float(hi)) / 2.0
            elif lo is not None:
                value = lo
            else:
                value = 0
            sample[field.get("field_name")] = value
        smoke = backend.evaluate(sample)
        if smoke.get("effectiveness_score") is None:
            raise ValueError("冻结模型冒烟评价未返回效能分")

        if output.exists():
            suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = output.parent / ("%s.backup_%s" % (output.name, suffix))
            if backup.exists():
                backup = output.parent / ("%s.backup_%s_%s" % (output.name, suffix, uuid.uuid4().hex[:4]))
            output.replace(backup)
        try:
            try:
                stage.replace(output)
            except OSError:
                # Windows 7/Windows security software may deny an atomic
                # directory rename after the smoke test imported Python files
                # from the staged runtime.  A verified copy is a safe fallback;
                # the previous installation has already been backed up.
                if output.exists():
                    shutil.rmtree(str(output))
                shutil.copytree(str(stage), str(output))
                shutil.rmtree(str(stage))
        except Exception:
            if output.exists():
                shutil.rmtree(str(output), ignore_errors=True)
            if backup is not None and backup.exists():
                try:
                    backup.replace(output)
                except OSError:
                    shutil.copytree(str(backup), str(output))
            raise
        return {
            "status": "PASS",
            "input_package": str(package_path),
            "input_package_sha256": _sha256(package_path),
            "output_manifest": str(output / MANIFEST_NAME),
            "backup_directory": str(backup) if backup is not None else None,
            "product_code": backend.product_code,
            "product_name": backend.product_name,
            "model_version": backend.model_version,
            "algorithm_version": backend.algorithm_version,
            "profile_version": backend.profile_version,
            "backend": backend.name,
            "state_mode": "frozen_learned_model_without_training_records",
            "smoke_effectiveness_score": smoke.get("effectiveness_score"),
        }
    except Exception:
        if stage.exists():
            shutil.rmtree(str(stage), ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="安装V11专家软件导出的冻结效能模型ZIP（旧Workbook+State打包方式仍兼容）"
    )
    parser.add_argument("--model-package", required=True, help="effectiveness_model_*.zip")
    parser.add_argument(
        "--output",
        default=str(ROOT / "services" / "effectiveness_service" / "model" / "current"),
    )
    parser.add_argument("--expected-product-code", default="")
    args = parser.parse_args()
    result = install_frozen_package(
        args.model_package, args.output, args.expected_product_code or None
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
