"""Explicit V3 build entry, including embedded-Python checkouts with custom _pth."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    p=argparse.ArgumentParser(description='复用现有构建流程并显式锁定optuna_v3，不修改训练算法')
    p.add_argument('--input',required=True);p.add_argument('--output',required=True)
    p.add_argument('--config',required=True);p.add_argument('--product-code',required=True)
    p.add_argument('--product-name',required=True);p.add_argument('--virtual-data',action='store_true')
    a=p.parse_args()
    from automl_pipeline.__main__ import main as pipeline_main
    args=['build',str(Path(a.input).resolve()),'--output',str(Path(a.output).resolve()),'--price-engine','optuna_v3',
          '--price-config',str(Path(a.config).resolve()),'--product-code',a.product_code,'--product-name',a.product_name]
    if a.virtual_data:args.append('--virtual-data')
    return pipeline_main(args)


if __name__=='__main__':main()
