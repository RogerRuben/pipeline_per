# -*- coding: utf-8 -*-
"""Paths and double-click entries for a self-contained offline kit."""
from pathlib import Path


def legacy_bat_names():
    return {'CHECK_ENVIRONMENT.bat': 'check', 'CHECK_INPUT.bat': 'inspect', 'TRAIN_AND_PACKAGE.bat': 'build',
            'BUILD_AND_PACKAGE.bat': 'build', 'SELF_TEST.bat': 'self-test', 'SELF_TEST_DS.bat': 'self-test-ds',
            'REPACK_LAST.bat': 'repack'}


ENTRY_DEV = '① 启动开发模式.bat'
ENTRY_REBUILD = '② 重新编译软件.bat'
ENTRY_PACKAGE = '③ 生成客户运行包.bat'
ENTRY_EXPORT = '④ 重新导出完整开发包.bat'

SKIP_PREFIXES = (
    'logs/', 'deliverables/', 'builder_state/', 'page_overrides/', 'input/', 'bootstrap/',
    'project/outputs/', 'project/data/', 'project/runtime/', 'project/backups/',
    'project/deliverables/',
)
SKIP_NAMES = {'LAST_ENGINE.json', '_frozen_resources.py'}


def kit_root_from_project(project):
    project = Path(project).resolve()
    parent = project.parent
    if (parent / 'KIT_MANIFEST.json').is_file():
        return parent
    return None


def include_in_reexport(relative):
    """Return whether a kit-relative path belongs in the next full kit."""
    relative = relative.replace('\\', '/').lstrip('/')
    if not relative or relative in SKIP_NAMES:
        return False
    name = relative.split('/')[-1]
    if name.endswith(('.pyc', '.pyo', '.log')):
        return False
    # Keep the interpreter and frozen-engine archives. Skip other zip files.
    if name.endswith('.zip') and name not in ('python38.zip', 'base_library.zip'):
        return False
    if relative.endswith('/_frozen_resources.py') or relative.endswith('_frozen_resources.py') and '/' not in relative:
        return False
    parts = relative.split('/')
    if '__pycache__' in parts or any(part == 'userdata' or part.startswith('userdata_') for part in parts):
        return False
    for prefix in SKIP_PREFIXES:
        if relative == prefix.rstrip('/') or relative.startswith(prefix):
            return False
    return True


def write_complete_sample(path):
    """Small synthetic table with every price field filled in."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['推力,速度,质量,响应时间,价格(万元)', 'thrust_n,speed_mm_s,mass_kg,response_time_ms,price_wan']
    for index in range(12):
        thrust = 100 + index * 15
        speed = 20 + index * 3
        mass = round(8 + index * 0.4, 2)
        response = 40 + index * 2
        price = round(18 + thrust * 0.08 + speed * 0.05, 4)
        lines.append('%s,%s,%s,%s,%s' % (thrust, speed, mass, response, price))
    path.write_bytes(('\ufeff' + '\r\n'.join(lines) + '\r\n').encode('utf-8'))


def _bat(body):
    text = '@echo off\r\ncd /d "%~dp0"\r\n' + body
    if not text.endswith('\r\n'):
        text += '\r\n'
    return text.encode('gbk')


def write_operator_bats(root):
    """Write the four no-argument entries. Commands stay inside this folder."""
    root = Path(root)
    python = r'"%~dp0runtime\python38\python.exe" -B -X utf8'
    files = {
        ENTRY_DEV: _bat(
            'set "PIPELINE_STATIC_DIR=%~dp0page_overrides"\r\n'
            + python + ' "%~dp0project\\run_app.py" %*\r\n'),
        ENTRY_REBUILD: _bat(
            python + ' "%~dp0project\\tools\\rebuild_engine_cli.py" %*\r\n'
            'set "KIT_EXIT=%ERRORLEVEL%"\r\n'
            'if not "%KIT_EXIT%"=="0" echo 编译失败，上一份可用引擎仍然保留。\r\n'
            'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b %KIT_EXIT%\r\n'),
        ENTRY_PACKAGE: _bat(
            python + ' "%~dp0project\\tools\\customer_package_cli.py" %*\r\n'
            'set "KIT_EXIT=%ERRORLEVEL%"\r\n'
            'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b %KIT_EXIT%\r\n'),
        ENTRY_EXPORT: _bat(
            python + ' "%~dp0project\\tools\\kit_self_export.py" %*\r\n'
            'set "KIT_EXIT=%ERRORLEVEL%"\r\n'
            'if not defined PRODUCT_NO_PAUSE pause\r\nexit /b %KIT_EXIT%\r\n'),
    }
    for name, content in files.items():
        (root / name).write_bytes(content)
    return files
