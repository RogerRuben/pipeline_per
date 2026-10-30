# -*- coding: utf-8 -*-
"""Offline Windows launcher for the lock-specialized V17 application."""

from __future__ import print_function

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
APP_DIR = HERE if HERE.name.lower() != "app" else HERE
PACKAGE_ROOT = HERE.parent if HERE.name.lower() == "app" else HERE
USERDATA_DIR = PACKAGE_ROOT / "userdata"
STATE_DIR = USERDATA_DIR / "state_v17_lock"
PROJECT_DIR = USERDATA_DIR / "projects"
LOG_DIR = USERDATA_DIR / "logs"
RECENT_FILE = USERDATA_DIR / "recent_workbook_v17.json"

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if HERE.name.lower() == "app":
    os.environ["EFFECTIVENESS_EXPERT_PACKAGE"] = "1"

from lock_v17_app import LockV17App, serve  # noqa: E402
from project_excel import ProjectDataError, load_project_workbook  # noqa: E402


def message_box(text, title="锁成品V17效能与复用评估"):
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x00000010)
    except Exception:
        print("%s: %s" % (title, text))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def initial_directory():
    try:
        data = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
        selected = Path(str(data.get("source_workbook") or ""))
        if selected.parent.is_dir():
            return str(selected.parent)
    except Exception:
        pass
    return str(PACKAGE_ROOT)


def choose_workbook_native():
    if os.name != "nt":
        raise ProjectDataError("文件选择窗口只支持Windows，请用--workbook指定Excel。")

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buffer = ctypes.create_unicode_buffer(32768)
    dialog = OPENFILENAMEW()
    dialog.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    dialog.lpstrFilter = "Excel工作簿 (*.xlsx;*.xlsm)\0*.xlsx;*.xlsm\0所有文件 (*.*)\0*.*\0\0"
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrInitialDir = initial_directory()
    dialog.lpstrTitle = "请选择锁成品V17项目Excel"
    dialog.Flags = 0x00080000 | 0x00001000 | 0x00000800 | 0x00000008
    dialog.lpstrDefExt = "xlsx"
    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(dialog)):
        error_code = ctypes.windll.comdlg32.CommDlgExtendedError()
        if error_code:
            raise ProjectDataError("Windows文件选择窗口失败，错误码：%s" % error_code)
        return None
    return Path(buffer.value).expanduser().resolve()


def import_workbook(source):
    source = Path(source).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ProjectDataError("请选择有效的.xlsx或.xlsm工作簿。")
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    digest = file_sha256(source)
    imported = PROJECT_DIR / ("%s_%s%s" % (source.stem[:70], digest[:12], source.suffix.lower()))
    if source != imported and (not imported.exists() or file_sha256(imported) != digest):
        shutil.copy2(str(source), str(imported))
    RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(
        json.dumps(
            {
                "source_workbook": str(source),
                "imported_workbook": str(imported),
                "source_sha256": digest,
                "selected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return imported


def find_available_port(preferred=8798, span=20):
    for port in range(int(preferred), int(preferred) + int(span) + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            pass
        finally:
            sock.close()
    raise ProjectDataError("本机没有可用端口，请关闭旧程序后重试。")


def open_application(url):
    candidates = [
        PACKAGE_ROOT / "browser" / "firefox" / "firefox.exe",
        PACKAGE_ROOT / "browser" / "chrome" / "chrome.exe",
        PACKAGE_ROOT / "browser" / "chromium" / "chrome.exe",
    ]
    for browser in candidates:
        if not browser.is_file():
            continue
        profile = USERDATA_DIR / "browser_profile_v17"
        profile.mkdir(parents=True, exist_ok=True)
        if browser.name.lower() == "firefox.exe":
            command = [str(browser), "-no-remote", "-profile", str(profile), url]
        else:
            command = [
                str(browser),
                "--user-data-dir=%s" % profile,
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--app=%s" % url,
            ]
        subprocess.Popen(command, cwd=str(browser.parent))
        return
    if not webbrowser.open(url, new=1, autoraise=True) and os.name == "nt":
        os.startfile(url)  # type: ignore[attr-defined]


def write_error_log(exc):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / ("startup_error_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    path.write_text("%s\n\n%s\n" % (exc, traceback.format_exc()), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description="锁成品V17离线启动器")
    parser.add_argument("--workbook", type=Path, default=None)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--port", type=int, default=8798)
    args = parser.parse_args()
    for directory in (STATE_DIR, PROJECT_DIR, LOG_DIR, USERDATA_DIR / "browser_profile_v17"):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        source = args.workbook or choose_workbook_native()
        if source is None:
            print("已取消选择，程序未启动。")
            return 0
        workbook = import_workbook(source)
        project = load_project_workbook(workbook, ignore_couplings=True)
        if args.validate_only:
            print("EXCEL_VALID")
            print("项目：%s" % project.project_name)
            print("属性：%s" % len(project.attributes))
            print("100分基准：%s" % len(project.requirement_profiles))
            return 0
        app = LockV17App(workbook, state_dir=STATE_DIR)
        if args.reset:
            app.load_state(reset=True)
        port = find_available_port(args.port)
        url = "http://127.0.0.1:%s" % port
        if not args.no_browser:
            timer = threading.Timer(1.2, open_application, args=(url,))
            timer.daemon = True
            timer.start()
        print("=" * 68)
        print("锁成品V17效能与复用评估")
        print("工作Excel：%s" % workbook)
        print("学习状态：%s" % app.state_path)
        print("页面地址：%s" % url)
        print("关闭本窗口即可停止程序；每次提交均会立即保存。")
        print("=" * 68)
        serve(app, "127.0.0.1", port)
        return 0
    except (ProjectDataError, ValueError, OSError) as exc:
        log_path = write_error_log(exc)
        message_box("项目载入失败：\n%s\n\n错误记录：%s" % (exc, log_path))
        return 2
    except Exception as exc:
        log_path = write_error_log(exc)
        message_box("程序启动失败：\n%s\n\n错误记录：%s" % (exc, log_path))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
