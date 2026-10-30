# -*- coding: utf-8 -*-
"""Windows-native entry point for upgrading an existing workbook to V11."""

import argparse
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from system_excel import upgrade_product_workbook


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def message_box(text: str, title: str, error: bool = False) -> None:
    flags = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)


def _dialog(open_file: bool, initial_dir: Path, initial_name: str = "") -> Optional[Path]:
    if os.name != "nt":
        raise RuntimeError("Excel升级图形入口只支持Windows；可使用--input和--output参数。")
    buffer = ctypes.create_unicode_buffer(32768)
    if initial_name:
        buffer.value = initial_name
    dialog = OPENFILENAMEW()
    dialog.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    dialog.lpstrFilter = "Excel工作簿 (*.xlsx;*.xlsm)\0*.xlsx;*.xlsm\0所有文件 (*.*)\0*.*\0\0"
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrInitialDir = str(initial_dir)
    dialog.lpstrDefExt = "xlsx"
    dialog.Flags = 0x00080000 | 0x00000800 | 0x00000008
    if open_file:
        dialog.lpstrTitle = "选择已有成品级或系统级Excel"
        dialog.Flags |= 0x00001000
        succeeded = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(dialog))
    else:
        dialog.lpstrTitle = "另存为系统级V11 Excel"
        dialog.Flags |= 0x00000002
        succeeded = ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(dialog))
    if succeeded:
        return Path(buffer.value).expanduser().resolve()
    error_code = ctypes.windll.comdlg32.CommDlgExtendedError()
    if error_code:
        raise RuntimeError("Windows文件选择窗口失败，错误码：%s" % error_code)
    return None


def choose_source() -> Optional[Path]:
    return _dialog(True, Path.cwd())


def choose_output(source: Path) -> Optional[Path]:
    return _dialog(False, source.parent, "%s_系统级V11.xlsx" % source.stem)


def main() -> int:
    parser = argparse.ArgumentParser(description="把已有Excel另存为系统级V11工作簿")
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    try:
        source = args.input.expanduser().resolve() if args.input else choose_source()
        if source is None:
            return 0
        output = args.output.expanduser().resolve() if args.output else choose_output(source)
        if output is None:
            return 0
        result = upgrade_product_workbook(source, output)
    except Exception as exc:
        if args.input or args.output:
            print("SYSTEM_EXCEL_V11_UPGRADE_FAILED: %s" % exc)
        else:
            message_box(str(exc), "升级失败", error=True)
        return 2
    text = "系统级V11 Excel已经生成：\n%s\n\n请填写系统任务要求，确认系统映射配置，并在有已知物理公式时填写派生属性公式。" % result
    if args.input or args.output:
        print("SYSTEM_EXCEL_V11_UPGRADE_PASS")
        print(result)
    else:
        message_box(text, "升级完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
