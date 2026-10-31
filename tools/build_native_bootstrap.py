"""Build a CRT-free x64 Windows 6.1 PE bootstrap on the development host."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import sha,write_json

SOURCES=('product_entry.py','product/__init__.py','product/common.py','product/launcher.py',
         'product/windows.py','product/compatibility.py','product/recovery.py','product/desktop_entry.py',
         'product/native/launcher.c','product/native/launcher.manifest','app/static/launcher.ico')
SOURCES=SOURCES+('product/supervisor.py','product/desktop_config.py')


def source_fingerprints():return {relative:sha(ROOT/relative) for relative in SOURCES}


def build(zig,out):
    zig,out=Path(zig).resolve(),Path(out).resolve()
    if out.exists():raise ValueError('Choose a new bootstrap output')
    out.mkdir(parents=True)
    work=out.parent/(out.name+'_build');work.mkdir()
    rc=work/'launcher.rc'
    rc.write_text('1 24 "'+(ROOT/'product/native/launcher.manifest').as_posix()+'"\n101 ICON "'+
                  (ROOT/'app/static/launcher.ico').as_posix()+'"\n',encoding='utf-8')
    env=dict(os.environ,ZIG_GLOBAL_CACHE_DIR=str(work/'cache'),ZIG_LOCAL_CACHE_DIR=str(work/'cache-local'))
    commands=[[str(zig),'rc','/fo',str(work/'launcher.res'),str(rc)],
              [str(zig),'cc','-target','x86_64-windows-gnu','-O2','-nostdlib','-fno-builtin','-fno-stack-protector',
               '-isystem',str(zig.parent/'lib/libc/include/any-windows-any'),
               '-Wl,--subsystem,windows','-Wl,--major-subsystem-version,6','-Wl,--minor-subsystem-version,1','-Wl,-e,entry',str(ROOT/'product/native/launcher.c'),str(work/'launcher.res'),
               '-lkernel32','-luser32','-o',str(out/'product.exe')]]
    for number,command in enumerate(commands):
        result=subprocess.run(command,env=env,cwd=str(out),stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (work/('compile-%d.log'%number)).write_bytes(result.stdout)
        if result.returncode:raise RuntimeError('Native bootstrap compile failed: '+str(work/('compile-%d.log'%number)))
    sys.path.insert(0,str(ROOT/'outputs/p4_build_tools'))
    import pefile
    pe=pefile.PE(str(out/'product.exe'))
    imports={entry.dll.decode():[symbol.name.decode() for symbol in entry.imports] for entry in pe.DIRECTORY_ENTRY_IMPORT}
    if set(name.upper() for name in imports)-{'KERNEL32.DLL','USER32.DLL'}:
        raise ValueError('Unexpected native bootstrap dependencies: '+str(imports))
    if pe.FILE_HEADER.Machine!=0x8664 or pe.OPTIONAL_HEADER.Subsystem!=2 or pe.OPTIONAL_HEADER.MajorSubsystemVersion>6:
        raise ValueError('Unexpected PE target')
    source=source_fingerprints()
    write_json(out/'BOOTSTRAP_SOURCE.json',source)
    write_json(out/'BOOTSTRAP_META.json',{'kind':'win32-native-cpython-onedir','target':'Windows 7 SP1 x64',
               'compiler':'Zig 0.13.0 clang/lld; no compiler required on client','exe_sha256':sha(out/'product.exe'),
               'pe_imports':imports,'subsystem_version':[pe.OPTIONAL_HEADER.MajorSubsystemVersion,pe.OPTIONAL_HEADER.MinorSubsystemVersion],
               'win7_machine_tested':False,'icon':'original PipelinePer mark','source':source})
    return {'bootstrap':str(out),'sha256':sha(out/'product.exe'),'imports':imports}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--zig',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    print(json.dumps(build(a.zig,a.out),ensure_ascii=True,indent=2))
