# -*- coding: utf-8 -*-
"""Freeze a new engine from the current sources. Refuses to pretend an old digest is still valid."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线重新冻结运行引擎')
    parser.add_argument('--out', required=True)
    parser.add_argument('--build-tools', required=True)
    parser.add_argument('--expert-engine')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    tools = Path(args.build_tools)
    if not tools.is_dir():
        raise SystemExit('缺少构建工具目录：' + str(tools))
    out = Path(args.out)
    sys.path.insert(0, str(tools.resolve()))
    try:
        import PyInstaller
        pyinstaller = PyInstaller.__version__
    except ImportError:
        pyinstaller = None
    if not pyinstaller:
        record = {'pyinstaller': False, 'frozen': False, 'build_tools': str(tools.resolve()),
                  'message': '当前 Python 没有安装 PyInstaller，不能离线冻结。未生成 EXE。'}
        out.mkdir(parents=True, exist_ok=True)
        (out / 'rebuild_check.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        raise SystemExit(record['message'])
    from tools.build_frozen_engine import engine_sources
    digest = engine_sources()
    record = {'source_files': len(digest), 'pyinstaller': pyinstaller, 'build_tools': str(tools.resolve()), 'frozen': False}
    if args.check_only:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'rebuild_check.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False))
        return 0
    from tools.build_frozen_engine import build
    result = build(args.out, args.build_tools, args.expert_engine)
    from tools.engine_pointer import remember_engine
    result['source_files'] = len(digest)
    result['engine_pointer'] = remember_engine(result['engine'])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
