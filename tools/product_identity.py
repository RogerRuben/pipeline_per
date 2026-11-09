"""Read a single workbook-level product name, never a sample's name."""
from pathlib import Path
from app.xlsx_utils import read_workbook_bytes


def workbook_product_name(path,fallback):
    path=Path(path)
    if path.suffix.lower()!='.xlsx':return fallback
    sheets=read_workbook_bytes(path.read_bytes());rows=sheets.get('产品信息') or []
    names=[]
    for row in rows:
        if len(row)>1 and str(row[0]).strip() in ('产品名称','product_name') and row[1] is not None:
            name=str(row[1]).strip()
            if name:names.append(name)
    if len(set(names))>1:raise ValueError('产品信息表存在多个不同的产品名称')
    return names[0] if names else fallback
