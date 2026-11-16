# -*- coding: utf-8 -*-
"""Freeze a new engine from the current sources. Refuses to pretend an old digest is still valid."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def resolve_rebuild_paths(root, out, tools, check_only):
    """Use the kit's own build_tools and a new engine folder when arguments are omitted."""
    root = Path(root).resolve()
    kit = root.parent if (root.parent / 'KIT_MANIFEST.json').is_file() else None
    if not tools:
        if kit and (kit / 'build_tools').is_dir():
            tools = str(kit / 'build_tools')
        else:
            raise SystemExit('缺少包内 build_tools。请使用完整开发包，不需要另行安装 PyInstaller。')
    if not out:
        if kit is None:
            raise SystemExit('请指定输出目录和构建工具目录。')
        if check_only:
            out = str(kit / 'logs' / 'compile_check')
        else:
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            out = str(root / 'outputs' / 'engines' / ('engine_' + stamp))
    return out, tools


def resolve_expert_engine(root, explicit):
    """Keep the sealed expert runtime that already ships inside the kit."""
    if explicit:
        path = Path(explicit)
        if not (path / 'effect_worker.exe').is_file() or not (path / 'EXPERT_RUNTIME.json').is_file():
            raise SystemExit('指定的专家引擎不完整：' + str(path))
        return str(path.resolve())
    root = Path(root).resolve()
    kit = root.parent if (root.parent / 'KIT_MANIFEST.json').is_file() else None
    if kit is None:
        return None
    candidates = [kit / 'bootstrap' / 'expert']
    from tools.engine_pointer import resolve_engine
    current = resolve_engine()
    if current is not None:
        candidates.append(Path(current) / 'expert')
    seen = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if (path / 'effect_worker.exe').is_file() and (path / 'EXPERT_RUNTIME.json').is_file():
            return str(path.resolve())
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线重新冻结运行引擎')
    parser.add_argument('--out')
    parser.add_argument('--build-tools')
    parser.add_argument('--expert-engine')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    args.out, args.build_tools = resolve_rebuild_paths(ROOT, args.out, args.build_tools, args.check_only)
    args.expert_engine = resolve_expert_engine(ROOT, args.expert_engine)
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
    record = {'source_files': len(digest), 'pyinstaller': pyinstaller, 'build_tools': str(tools.resolve()),
              'expert_engine': args.expert_engine, 'frozen': False}
    if args.check_only:
        out.mkdir(parents=True, exist_ok=True)
        (out / 'rebuild_check.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=False))
        return 0
    from tools.build_frozen_engine import build
    try:
        result = build(args.out, args.build_tools, args.expert_engine)
    except Exception as exc:
        failed = {'frozen': False, 'out': str(args.out), 'build_tools': str(tools.resolve()), 'error': str(exc)}
        print(json.dumps(failed, ensure_ascii=False))
        raise SystemExit('编译失败，上一份可用引擎仍然保留。' + str(exc))
    from tools.engine_pointer import remember_engine
    result['source_files'] = len(digest)
    result['build_tools'] = str(tools.resolve())
    result['expert_engine'] = args.expert_engine
    result['engine_pointer'] = remember_engine(result['engine'])
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
