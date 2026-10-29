import hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.build_product_v7 import build
def main():
    backup=ROOT/'outputs/v7_20261030/unverified_builds';backup.mkdir(parents=True,exist_ok=True)
    for kind in ('PATCH','Offline'):
        path=ROOT/('deliverables/ProductAutoML_V7_'+kind+'_20261030.zip')
        if path.is_file():
            meta=json.loads(Path(str(path)+'.json').read_text(encoding='utf-8'))
            if meta['status']!='BUILT_PENDING_VERIFICATION' or hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('Refuse replacing verified/unknown archive')
            for suffix in ('','.json','.sha256'):
                source=Path(str(path)+suffix);destination=backup/(path.stem+'_'+meta['sha256'][:10]+'.zip'+suffix)
                if destination.exists():raise ValueError('Backup exists')
                source.rename(destination)
        build(path,kind=='Offline',ROOT/'outputs/v7_20261030/delivery_final_demos')
if __name__=='__main__':main()
