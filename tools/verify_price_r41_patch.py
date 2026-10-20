# -*- coding: utf-8 -*-
"""Install the fix on the original ZIP and run its full integrity/start checks."""
import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.verify_product_r4_portable import exercise


def extract(archive,destination):
    with zipfile.ZipFile(str(archive)) as z:
        if z.testzip() is not None: raise ValueError("Archive CRC failure")
        for member in z.infolist():
            target=(destination/member.filename).resolve()
            if destination.resolve() not in target.parents or ".." in Path(member.filename).parts or ":" in member.filename:
                raise ValueError("Unsafe archive path")
        z.extractall(str(destination))


def main():
    p=argparse.ArgumentParser(); p.add_argument("--base",required=True); p.add_argument("--patch",required=True); p.add_argument("--destination",required=True); p.add_argument("--report",required=True)
    a=p.parse_args(); destination=Path(a.destination).resolve()
    if ROOT/"deliverables" not in destination.parents or destination.exists(): raise ValueError("Use a new deliverables extraction directory")
    destination.mkdir(parents=True); extract(a.base,destination)
    packages=list(destination.glob("*/PACKAGE_MANIFEST.json")); assert len(packages)==1
    package=packages[0].parent; extract(a.patch,package)
    env=dict(os.environ,PRODUCT_NO_PAUSE="1",PYTHONPATH="",PYTHONIOENCODING="utf-8")
    commands=[[str(package/"runtime/python38/python.exe"),"-X","utf8","tools/upgrade_product_r41.py"],
              [str(package/"runtime/python38/python.exe"),"-X","utf8","tools/product_portable.py","verify","--full"]]
    for command in commands:
        run=subprocess.run(command,cwd=str(package),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
        if run.returncode: raise AssertionError(run.stdout.decode("utf-8",errors="replace"))
    report=Path(a.report).resolve(); report.parent.mkdir(parents=True,exist_ok=True)
    result=exercise(package,report.with_suffix(".log"),generation=False)
    result.update(installed_on_original_r4_zip=True,full_immutable_integrity_check=True)
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
if __name__=="__main__": main()
