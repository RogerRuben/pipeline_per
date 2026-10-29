import argparse,json,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True);reports={}
    for version,script in [('v4','verify_product_v4_tests.py'),('v5','verify_product_v5_tests.py'),('v51','verify_product_v51_tests.py'),('v6','verify_product_v6_tests.py')]:
        with (out/(version+'.log')).open('w',encoding='utf-8') as f:
            code=subprocess.call([sys.executable,str(ROOT/'tools'/script),'--report',str(out/(version+'.json'))],cwd=str(ROOT),stdout=f,stderr=subprocess.STDOUT)
        reports[version]=json.loads((out/(version+'.json')).read_text(encoding='utf-8'))
        if code:break
    (out/'historical_summary.json').write_text(json.dumps(reports,indent=2),encoding='utf-8')
    return 0 if len(reports)==4 and all(r['status']=='PASS' for r in reports.values()) else 1
if __name__=='__main__':sys.exit(main())
