"""Rebuild only this task's hash-verified, still-unverified candidate archives."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.build_product_v6 import build
import hashlib

def main():
    backup=ROOT/'outputs/v6_20261027/unverified_builds';backup.mkdir(parents=True,exist_ok=True)
    for kind in ('PATCH','Offline'):
        path=ROOT/('deliverables/ProductAutoML_V6_'+kind+'_20261027.zip')
        if path.is_file():
            meta=json.loads(Path(str(path)+'.json').read_text(encoding='utf-8'))
            if meta['status']!='BUILT_PENDING_VERIFICATION' or hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('Refuse replacing a verified/unknown artifact')
            for suffix in ('','.json','.sha256'):
                src=Path(str(path)+suffix);dest=backup/(path.stem+'_'+meta['sha256'][:10]+'.zip'+suffix)
                if dest.exists():raise ValueError('Backup already exists')
                src.rename(dest)
        build(path,kind=='Offline',ROOT/'outputs/v6_20261027/final_demos')

if __name__=='__main__':main()
