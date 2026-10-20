# -*- coding: utf-8 -*-
"""Run a validated saved prototype without consulting training inputs."""
from __future__ import print_function

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixed_version_launcher_bytes():
    # Raw paths prevent runtime and tools paths becoming control characters.
    lines = ["@echo off", "setlocal", "chcp 65001 >nul",
             r'"%~dp0..\..\runtime\python38\python.exe" -X utf8 "%~dp0..\..\tools\product_portable.py" start --output "%~dp0." %*',
             "if errorlevel 1 (", "  if not defined PRODUCT_NO_PAUSE pause",
             "  exit /b 1", ")", "endlocal", ""]
    return "\r\n".join(lines).encode("ascii")


def saved_start_launcher_bytes():
    lines = ["@echo off", "setlocal", "chcp 65001 >nul", r'cd /d "%~dp0"',
             r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\product_saved_start.py" %*',
             "if errorlevel 1 (", "  if not defined PRODUCT_NO_PAUSE pause",
             "  exit /b 1", ")", "endlocal", ""]
    return "\r\n".join(lines).encode("ascii")


def main(argv=None):
    from tools import product_portable as portable
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        output = portable.latest_output()
        if output is None:
            raise ValueError("当前模式没有完整且校验通过的已保存版本；请先CHECK_INPUT.bat，再RETRAIN.bat。START和RUN_SAVED均不会自动训练。")
        launcher = output / "START_PROTOTYPE_WIN7.bat"
        content = fixed_version_launcher_bytes()
        if not launcher.is_file() or launcher.read_bytes() != content:
            launcher.write_bytes(content)
        print("直接运行已保存版本：%s；跳过训练表、选参和重新训练。" % output.relative_to(portable.ROOT), flush=True)
    except Exception as exc:
        print("保存版本启动失败：%s；已有模型保留。" % exc, file=sys.stderr, flush=True)
        return 1
    return portable.main(["start", "--output", str(output)] + arguments)


if __name__ == "__main__":
    sys.exit(main())
