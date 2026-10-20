import json,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np,pandas as pd
from tools.product_price_tabpfn import load_config,_select,_fit_bank


def main():
    X=pd.DataFrame({'x0':np.arange(8,dtype=float),'x1':np.arange(8,dtype=float)**2})
    y=10+X.x0.to_numpy();groups=np.arange(8)
    specs=[{'parameter_id':key,'label':'属性'+key,'value_type':'number'} for key in X]
    config=load_config(overrides={'validation':{'protocol':'group_loo'}})
    bad_seed=int(np.random.SeedSequence(config['seed_bagging']['base']).generate_state(4)[1])
    class ExpansionWorker:
        def fit(self,candidate,seed,specs,frame,target,validation,return_model=False):
            if candidate['family']=='tabpfn' and seed==bad_seed: raise ValueError('候选产生非有限或非正价格')
            return 10+validation.x0.to_numpy(),None
    class DiagnosticWorker:
        def fit(self,candidate,seed,specs,frame,target,validation,return_model=False):
            if candidate['family']=='tabpfn': raise ValueError('候选产生非有限或非正价格')
            return 10+validation.x0.to_numpy(),None
    result=[]
    base=ROOT/'outputs/candidate_failure_fix_20261015';base.mkdir(exist_ok=True)
    try:
        _select(X,y,groups,specs,config,ExpansionWorker(),base/'cache',base/'selection',lambda _:None)
    except Exception as exc:result.append({'phase':'seed_expansion','unhandled':True,'type':type(exc).__name__,'reason':str(exc)})
    selection={'candidates':[{'config':{'model':'ridge','family':'linear','label':'好模型'},'seeds':[1]},
                             {'config':{'model':'tabpfn','family':'tabpfn','label':'零权重对比模型'},'seeds':[1]}],
               'weights':np.array([1.,0.]),'best_tabpfn':1}
    try:_fit_bank(selection,X,y,X,specs,DiagnosticWorker(),[0,1])
    except Exception as exc:result.append({'phase':'zero_weight_benchmark_refit','unhandled':True,'type':type(exc).__name__,'reason':str(exc)})
    assert len(result)==2
    (base/'before_fix_reproduction.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()