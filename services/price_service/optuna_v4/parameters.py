# -*- coding: utf-8 -*-
from ..optuna_v2 import price_automl_core as core


def space_limits(train_rows,adaptive):
    return {'depth':max(1,min(9,train_rows-1)) if adaptive else 9,
            'split':[x for x in (2,4,6,8,10) if not adaptive or x<=train_rows],
            'leaf':[x for x in (1,2) if not adaptive or x<=max(1,train_rows//2)],
            'et_depth':sorted(set(min(x,max(1,train_rows-1)) if adaptive else x for x in (1,2,3,5,10,20)))}


def suggest(trial,limits,seeds,seed_bounds=None):
    cfg={'split_seed':0,'model_seed':trial.suggest_int('model_seed',*seed_bounds) if seed_bounds else trial.suggest_categorical('model_seed',seeds)}
    cfg['lasso_alpha']=trial.suggest_float('lasso_alpha',1e-8,1.,log=True)
    cfg['ridge_alpha']=trial.suggest_float('ridge_alpha',1e-8,1e4,log=True)
    cfg['svr_kernel']=trial.suggest_categorical('svr_kernel',['linear','rbf'])
    cfg['svr_C']=trial.suggest_float('svr_C',.01,1e4,log=True)
    cfg['svr_epsilon']=trial.suggest_float('svr_epsilon',1e-5,.3,log=True)
    cfg['svr_gamma']=trial.suggest_categorical('svr_gamma',['scale','auto',.0001,.001,.01,.1,1.])
    cfg['gbdt_n_estimators']=trial.suggest_categorical('gbdt_n_estimators',[50,100,200,300])
    cfg['gbdt_max_depth']=trial.suggest_int('gbdt_max_depth',1,min(5,limits['depth']))
    cfg['gbdt_min_samples_split']=trial.suggest_categorical('gbdt_min_samples_split',limits['split'])
    cfg['gbdt_learning_rate']=trial.suggest_float('gbdt_learning_rate',.005,.5,log=True)
    cfg['gbdt_subsample']=trial.suggest_float('gbdt_subsample',.5,1.)
    cfg['et_n_estimators']=trial.suggest_categorical('et_n_estimators',[50,100,200,300])
    cfg['et_max_depth']=trial.suggest_categorical('et_max_depth',limits['et_depth'])
    cfg['et_min_samples_split']=trial.suggest_categorical('et_min_samples_split',limits['split'])
    cfg['et_max_features']=trial.suggest_categorical('et_max_features',[.3,.5,.8,1.])
    cfg['tree_max_depth']=trial.suggest_int('tree_max_depth',1,limits['depth'])
    cfg['tree_min_samples_leaf']=trial.suggest_categorical('tree_min_samples_leaf',limits['leaf'])
    cfg['tree_lambda_expert']=trial.suggest_float('tree_lambda_expert',0.,2.)
    cfg['tree_min_gain_fraction']=trial.suggest_float('tree_min_gain_fraction',0.,.5)
    cfg['tree_min_gain_split']=0.
    for name in core.MODEL_NAMES:cfg['weight_'+name]=trial.suggest_float('weight_'+name,0.,1.)
    return cfg


def baselines(limits,seeds):
    depth=limits['depth'];cfg={'model_seed':seeds[0],'lasso_alpha':.001,'ridge_alpha':.1,'svr_kernel':'linear','svr_C':1.,'svr_epsilon':.001,'svr_gamma':.01,
        'gbdt_n_estimators':100,'gbdt_max_depth':min(3,depth),'gbdt_min_samples_split':min(limits['split']),'gbdt_learning_rate':.1,'gbdt_subsample':.7,
        'et_n_estimators':100,'et_max_depth':limits['et_depth'][-1],'et_min_samples_split':min(limits['split']),'et_max_features':1.,
        'tree_max_depth':min(5,depth),'tree_min_samples_leaf':1,'tree_lambda_expert':.8,'tree_min_gain_fraction':0.}
    result=[]
    for active in (('ridge',),('expert_tree',),core.MODEL_NAMES,('extratrees',),('svr',),('gbdt',),('lasso',),('ridge','extratrees'),('svr','gbdt')):
        result.append(dict(cfg,**{'weight_'+name:(.5 if name in active else 0.) for name in core.MODEL_NAMES}))
    return result
