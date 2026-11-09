from pathlib import Path
from product.common import read_json
from tools.build_ds_release import build

def inspect_ds(kit,raw):
    cfg=read_json(Path(kit)/'settings/ds.json');model=Path(cfg['model_file'])
    cfg['model_file']=str(model if model.is_absolute() else Path(kit)/model)
    return build(raw['source'],None,raw['product_code'],raw['product_name'],cfg,virtual_data=raw['data_kind']=='synthetic',inspect_only=True)
