# -*- coding: utf-8 -*-
"""Candidate-local failure isolation; blending uses training OOF targets only."""
import json
from pathlib import Path
import numpy as np
from tools.product_price_automl import metrics,write_json
from tools.product_price_automl_v2 import BudgetExpired
from tools.price_expert_fusion_probe import fit_expert_weights,FusionOptimizationError


class NoUsablePriceCandidates(ValueError):
    pass


def positive_predictions(values,size):
    values=np.asarray(values,dtype=float).reshape(-1)
    finite=np.isfinite(values)
    if len(values)!=size or not np.all(finite) or np.any(values<=0):
        minimum=float(np.min(values[finite])) if np.any(finite) else None
        raise ValueError('候选产生非有限或非正价格：数量%d/%d，非有限%d，非正%d，有限值最小值%s' %
                         (len(values),size,np.count_nonzero(~finite),np.count_nonzero(finite & (values<=0)),minimum))
    return values


def failure_event(config,exc,stage,seed=None,action='discard_configuration'):
    return {'config':config,'stage':stage,'seed':None if seed is None else int(seed),
            'error_type':type(exc).__name__,'reason':str(exc),'action':action}


def save_selection(selection,directory=None):
    directory=directory or selection.get('_report_directory')
    if not directory:return
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    write_json(directory/'candidate_report.json',{'candidates':[{key:value for key,value in row.items() if key not in ('oof','repeats')} for row in selection['candidates']],
                'failures':selection['failures'],'fusion':selection['fusion'],'cache_hits':selection['cache_hits'],
                'tabpfn_available':selection['best_tabpfn'] is not None,'refit_recoveries':selection.get('refit_recoveries',[])})


def blend_candidates(rows,y,config,failures,cache_hits=0,directory=None):
    if not rows:
        if directory:
            write_json(Path(directory)/'candidate_report.json',{'candidates':[],'failures':failures,'status':'no_usable_candidates'})
        raise NoUsablePriceCandidates('没有任何通过验证的价格候选；已保存逐项失败原因，不能导出不可用模型。')
    settings=config['ensemble']
    bank=np.column_stack([row['oof'] for row in rows])
    experts=[dict(item,name=item.get('label',item['family']),members=[i for i,row in enumerate(rows) if row['config']['family']==item['family']]) for item in settings['family_priors']]
    def optimize(upper=None):
        return fit_expert_weights(y,bank,mode=settings['mode'],prior_strength=settings['prior_strength'],alpha=settings['alpha'],
              max_mape_delta_pp=settings['max_mape_delta_pp'],max_r2_drop=settings['max_r2_drop'],expert_groups=experts,upper_bounds=upper)
    fusion=optimize();weights=np.asarray(fusion['weights'])
    disabled=set()
    for _ in rows:
        tiny=[i for i,value in enumerate(weights) if 0<value<settings['minimum_weight']]
        if not tiny:break
        disabled.update(tiny);upper=np.ones(len(rows));upper[list(disabled)]=0
        try:fusion=optimize(upper);weights=np.asarray(fusion['weights'])
        except (ValueError,FusionOptimizationError):break
    scores=[metrics(y,np.column_stack([row['repeats'][repeat] for row in rows]).dot(weights)) for repeat in range(config['validation']['repeats'])]
    micro=config['validation']['protocol']=='group_loo'
    cv={'mean_mape':float(np.mean([row['mape_percent'] for row in scores])),
        'std_mape':None if micro else float(np.std([row['mape_percent'] for row in scores])),
        'mean_r2':float(np.mean([row['r2'] for row in scores])),
        'std_r2':None if micro else float(np.std([row['r2'] for row in scores]))}
    tabs=[i for i,row in enumerate(rows) if row['config']['family']=='tabpfn']
    best=min(tabs,key=lambda i:settings['alpha']*rows[i]['metrics']['mape_percent']/100+(1-settings['alpha'])*(1-rows[i]['metrics']['r2'])) if tabs else None
    if best is None and config['resilience']['require_tabpfn']:
        raise NoUsablePriceCandidates('已启用require_tabpfn，但没有可用的TabPFN候选；其他模型仍已完成诊断。')
    selected={'candidates':rows,'weights':weights,'fusion':fusion,'oof':bank.dot(weights),'cv':cv,'failures':list(failures),
              'best_tabpfn':best,'cache_hits':cache_hits,'_selection_targets':np.array(y,copy=True),
              '_config':config,'_report_directory':directory}
    save_selection(selected)
    if directory:np.savez_compressed(Path(directory)/'oof_predictions.npz',predictions=bank,targets=y,weights=weights)
    return selected


def fit_selection_bank(selection,frame,y,validation,specs,worker,return_models=False,stage='refit'):
    cached={};recoveries=list(selection.get('refit_recoveries',[]))
    initial_count=len(selection['candidates'])
    for _ in range(initial_count+1):
        required={i for i,weight in enumerate(selection['weights']) if weight>0}
        if selection['best_tabpfn'] is not None:required.add(selection['best_tabpfn'])
        failed=[]
        for index in sorted(required):
            row=selection['candidates'][index]
            identity=json.dumps([row['config'],row['seeds']],sort_keys=True)
            if identity in cached:continue
            outputs,models=[],[];seed=None
            try:
                for seed in row['seeds']:
                    values,model=worker.fit(row['config'],seed,specs,frame,y,validation,return_models)
                    outputs.append(positive_predictions(values,len(validation)));models.append(model)
                cached[identity]=(positive_predictions(np.mean(outputs,axis=0),len(validation)),models)
            except BudgetExpired:raise
            except Exception as exc:
                event=failure_event(row['config'],exc,stage,seed)
                event['previous_weight']=float(selection['weights'][index])
                selection['failures'].append(event);recoveries.append(event);failed.append(index)
                progress=selection.get('_progress')
                if progress:progress('候选重拟合失败，淘汰后使用训练折外预测重新融合：%s；%s' % (row['config']['label'],exc))
                save_selection(selection)
        if not failed:
            bank=np.zeros((len(validation),len(selection['candidates'])))
            fitted=[[] for row in selection['candidates']]
            for index in required:
                row=selection['candidates'][index];key=json.dumps([row['config'],row['seeds']],sort_keys=True)
                bank[:,index],fitted[index]=cached[key]
            selection['refit_recoveries']=recoveries;save_selection(selection)
            return bank,fitted
        survivors=[row for i,row in enumerate(selection['candidates']) if i not in failed]
        progress=selection.get('_progress')
        updated=blend_candidates(survivors,selection['_selection_targets'],selection['_config'],selection['failures'],selection['cache_hits'],selection.get('_report_directory'))
        updated['refit_recoveries']=recoveries;updated['_progress']=progress
        selection.clear();selection.update(updated)
    raise NoUsablePriceCandidates('所有候选的重新拟合均失败，不能导出价格模型。')