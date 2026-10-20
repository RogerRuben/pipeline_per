# -*- coding: utf-8 -*-
"""Price attribute priors, fixed-parameter seed search and complete nested audit."""
import copy
import hashlib
import json
import time
from pathlib import Path
from app.console_compat import enable_plain_console
enable_plain_console()
import numpy as np
import optuna
import sklearn
from scipy.optimize import minimize
from sklearn.model_selection import LeaveOneGroupOut
from threadpoolctl import threadpool_limits
from services.price_service.r4_estimators import R4PriceRegressor, MeanPriceRegressor, weighted_median
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import save_bundle,load_bundle,predict
from tools.product_price_automl import price_dataset,native_field_metadata,write_json,grouped_folds

LABELS={"baseline":"常量价格参考","ridge":"专家加权岭回归","relative_ridge":"相对误差加权岭回归","lasso":"专家加权稀疏回归","elasticnet":"专家加权弹性网络",
        "mape_lasso":"相对绝对误差回归","relative_huber":"相对Huber回归","pls":"价格潜变量回归",
        "svr":"专家加权支持向量回归","kernel_ridge":"专家加权核岭","gpr":"高斯过程回归",
        "expert_tree":"专家引导分裂树","extra_trees":"极端随机树","gbdt":"梯度提升树"}
LEGACY_MODELS=[m for m in LABELS if m not in ("baseline","gpr")]
LABELS.update(pca_ridge="主成分岭回归",poly_ridge="交互多项式岭回归",bayesian_ridge="贝叶斯岭回归",ard="稀疏贝叶斯回归")
FAMILIES={m:("baseline" if m=="baseline" else "tree" if m in ("expert_tree","extra_trees","gbdt") else "kernel" if m in ("svr","kernel_ridge","gpr") else "linear") for m in LABELS}
DEFAULT={"schema_version":"price-automl-r4-1.0","models":LEGACY_MODELS,
 "search":{"anchor_trials":32,"seed":20261012,"promote":3,"expert_strengths":[0.,.5,1.],"ridge_alphas":[.001,.01,.1,1.,10.,100.,1000.],"parameter_ranges":{},
           "warm_start":True,"restart_trials":24,"max_restarts":1,"seed_cost_ratio":8.,"wide_kernels":False,"optimize_gp":False,"family_first":False,"warm_start_fraction":1.},
 "pipeline":{"imputation":"median","scaler":"standard","target_transforms":["identity","log"],"expert_core_threshold":4.,"feature_views":["all","expert_core","screened"],"select_k":[3,5,8],"screening":"pearson","select_fraction":.5,"stability_resamples":0,"stability_fraction":.8,"stability_correlation":.98},
 "seed_search":{"profile":"deep","limits":{"quick":64,"standard":500,"deep":5000},"max_trials":None,"base":811,
                "milestones":[64,200,1000,5000],"minimum_trials":64,"early_stop":True,"patience":2,"minimum_gain":.0001,
                "sample_weight_strength":.25,"members_per_configuration":4,"stop_on_targets":True,"check_interval":64,"stop_when_unmet_stagnant":True},
 "validation":{"minimum_groups":10,"micro_max_groups":29,"inner_folds":5,"outer_folds":5,"seed":311,"inner_strategy":"auto","loo_inner_max_groups":12,
               "outer_strategy":"auto","inner_repeats":1,"outer_repeats":1},
 "ensemble":{"max_members":4,"alpha":.7,"regularization":.005,"jackknife":True,"calibration":True,"calibration_bounds":[.85,1.15],
             "min_members":1,"minimum_weight":.03,"maximum_weight":.95,"candidate_pool":24,"robust":False,"stability_penalty":.15,"max_mape_delta_pp":.2,"max_r2_drop":.005,"minimum_gain":1e-6,"candidate_loss_ratio":4.,"candidate_loss_slack":.05},
 "resources":{"total_seconds":1800,"cache_directory":None,"progress_interval_seconds":5.,"per_search_seconds":None},
 "quality":{"mape_percent_max":10.,"r2_min":.9,"minimum_groups_for_evidence":30,"hard_gate":False},
 "expert":{"scores":{},"records":[],"field_types":{},"attribute_groups":{},"group_strengths":{},"compare_individual_experts":False,"enabled":True},
 "diagnostics":{"sample_influence":False},"trust_region":{"enabled":False,"threshold_factor":1.25,"disagreement_multiplier":1.,"extrapolation_penalty":.1},"missing_tokens":[]}


def load_config(path=None,overrides=None):
    result=copy.deepcopy(DEFAULT)
    def merge(target,source,location=""):
        if not isinstance(source,dict): raise ValueError("R4设置必须是对象："+location)
        for key,value in source.items():
            if key not in target: raise ValueError("未知R4设置："+location+key)
            if isinstance(target[key],dict) and key not in ("scores","field_types","attribute_groups","group_strengths","parameter_ranges"):
                merge(target[key],value,location+key+".")
            else: target[key]=value
    if path: merge(result,json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if overrides: merge(result,overrides)
    if result["schema_version"]!=DEFAULT["schema_version"]: raise ValueError("请使用R4可执行配置。")
    if not result["models"] or set(result["models"])-set(LABELS) or len(set(result["models"]))!=len(result["models"]): raise ValueError("R4模型名单无效。")
    s=result["seed_search"]; v=result["validation"]; a=result["search"]
    if s["profile"] not in s["limits"]: raise ValueError("种子模式须为quick/standard/deep。")
    for value in [a["anchor_trials"],a["promote"],s["minimum_trials"],s["members_per_configuration"],s["check_interval"],v["minimum_groups"],v["inner_folds"],v["outer_folds"]]+list(s["limits"].values()):
        if not isinstance(value,int) or isinstance(value,bool) or value<1: raise ValueError("R4次数设置须为正整数。")
    if s["max_trials"] is not None and (not isinstance(s["max_trials"],int) or isinstance(s["max_trials"],bool) or s["max_trials"]<1): raise ValueError("max_trials须为正整数或null。")
    if max(s["limits"].values())>100000 or (s["max_trials"] or 1)>100000: raise ValueError("种子次数最多100000。")
    if not isinstance(s["patience"],int) or isinstance(s["patience"],bool) or s["patience"]<1 or not np.isfinite(s["minimum_gain"]) or s["minimum_gain"]<0:
        raise ValueError("稳定批次数须为正整数，最小增益须为非负数。")
    if not s["milestones"] or any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in s["milestones"]) or s["milestones"]!=sorted(set(s["milestones"])):
        raise ValueError("种子批次档位须为递增正整数。")
    for owner,key in ((a,"warm_start"),(a,"wide_kernels"),(a,"optimize_gp"),(a,"family_first"),(s,"early_stop"),(s,"stop_on_targets"),(s,"stop_when_unmet_stagnant"),(result["ensemble"],"calibration"),(result["ensemble"],"jackknife"),(result["quality"],"hard_gate"),(result["expert"],"compare_individual_experts"),(result["expert"],"enabled")):
        if not isinstance(owner[key],bool): raise ValueError(key+"须为true或false。")
    if not 0<=s["sample_weight_strength"]<1 or not 1<=s["members_per_configuration"]<=32: raise ValueError("样本扰动强度须在[0,1)，保留种子数为1—32。")
    if v["minimum_groups"]<10 or min(v["inner_folds"],v["outer_folds"])<2: raise ValueError("R4至少10组有效价格记录，折数至少2。")
    if a["anchor_trials"]<len(result["models"]): raise ValueError("初次搜索预算须覆盖模型名单。")
    if not np.isfinite(a['warm_start_fraction']) or not 0<a['warm_start_fraction']<=1:raise ValueError('预热预算比例须在(0,1]。')
    if not a["expert_strengths"] or any(not np.isfinite(x) or not 0<=x<=2 for x in a["expert_strengths"]): raise ValueError("专家先验强度无效。")
    if not a["ridge_alphas"] or any(not np.isfinite(x) or x<=0 for x in a["ridge_alphas"]): raise ValueError("岭回归收缩强度必须为正。")
    if result["pipeline"]["imputation"] not in ("mean","median") or result["pipeline"]["scaler"] not in ("standard","robust","none"): raise ValueError("补全或缩放设置无效。")
    if not result["pipeline"]["target_transforms"] or set(result["pipeline"]["target_transforms"])-{"identity","log"}: raise ValueError("价格变换须为identity/log。")
    if not result["pipeline"]["feature_views"] or set(result["pipeline"]["feature_views"])-{"all","expert_core","screened"}: raise ValueError("属性视图须为all/expert_core/screened。")
    if not result["pipeline"]["select_k"] or any(not isinstance(k,int) or isinstance(k,bool) or k<1 for k in result["pipeline"]["select_k"]): raise ValueError("select_k须为正整数列表。")
    if any(v[key] not in ("auto","loo","group_kfold","price_balanced") for key in ("inner_strategy","outer_strategy")) or not isinstance(v["loo_inner_max_groups"],int) or v["loo_inner_max_groups"]<3: raise ValueError("验证策略无效。")
    if any(not isinstance(v[key],int) or isinstance(v[key],bool) or not 1<=v[key]<=30 for key in ("inner_repeats","outer_repeats")): raise ValueError("CV重复次数须为1—30。")
    if result["pipeline"]["screening"] not in ("pearson","nonlinear") or not 0<result["pipeline"]["select_fraction"]<=1: raise ValueError("折内筛选设置无效。")
    e=result["ensemble"]
    if not isinstance(e['robust'],bool) or any(not np.isfinite(e[key]) or e[key]<0 for key in ('stability_penalty','max_mape_delta_pp','max_r2_drop','minimum_gain','candidate_loss_ratio','candidate_loss_slack')) or e['candidate_loss_ratio']<1:raise ValueError('稳健融合设置无效。')
    p=result['pipeline']
    if not isinstance(p['stability_resamples'],int) or not 0<=p['stability_resamples']<=200 or not 0<p['stability_fraction']<=1 or not 0<p['stability_correlation']<=1:raise ValueError('稳定性筛选设置无效。')
    if not isinstance(result['diagnostics']['sample_influence'],bool) or not isinstance(result['trust_region']['enabled'],bool):raise ValueError('诊断及可信域开关无效。')
    if any(not np.isfinite(result['trust_region'][key]) or result['trust_region'][key]<0 for key in ('threshold_factor','disagreement_multiplier','extrapolation_penalty')) or result['trust_region']['threshold_factor']<=0:raise ValueError('可信域参数无效。')
    if not isinstance(e["min_members"],int) or not 1<=e["min_members"]<=e["max_members"] or not isinstance(e["candidate_pool"],int) or e["candidate_pool"]<e["max_members"]: raise ValueError("融合数量设置无效。")
    if not 0<=e["minimum_weight"]<=1/e["max_members"] or not 1/e["min_members"]<=e["maximum_weight"]<=1:
        if e["min_members"]>1: raise ValueError("融合权重界限不相容。")
    if not isinstance(a["restart_trials"],int) or a["restart_trials"]<1 or not isinstance(a["max_restarts"],int) or a["max_restarts"]<0 or a["seed_cost_ratio"]<1: raise ValueError("重新选参与种子耗时比设置无效。")
    if not 1<=result["pipeline"]["expert_core_threshold"]<=5: raise ValueError("专家子集阈值须为1—5。")
    if not 0<=result["ensemble"]["alpha"]<=1 or not 1<=result["ensemble"]["max_members"]<=8 or result["ensemble"]["regularization"]<0: raise ValueError("融合设置无效。")
    bounds=result["ensemble"]["calibration_bounds"]
    if len(bounds)!=2 or not np.all(np.isfinite(bounds)) or not 0<bounds[0]<=1<=bounds[1]: raise ValueError("校准范围须为包含1的正区间。")
    if not np.isfinite(result["resources"]["total_seconds"]) or result["resources"]["total_seconds"]<=0: raise ValueError("训练时间预算必须为正。")
    if not np.isfinite(result["resources"]["progress_interval_seconds"]) or result["resources"]["progress_interval_seconds"]<=0: raise ValueError("日志间隔须为正秒数。")
    if result["resources"]["per_search_seconds"] is not None and (not np.isfinite(result["resources"]["per_search_seconds"]) or result["resources"]["per_search_seconds"]<=0): raise ValueError("每轮搜索秒数须为正或null。")
    for key,value in result["expert"]["scores"].items():
        if not isinstance(value,(int,float)) or isinstance(value,bool) or not np.isfinite(value) or not 1<=float(value)<=5: raise ValueError("价格属性%s的评分必须为1—5。"%key)
    for key,value in result["expert"]["group_strengths"].items():
        if not np.isfinite(value) or not 0<=value<=2: raise ValueError("价格属性组强度须为0—2："+key)
    ranges=a["parameter_ranges"]
    supported={"ridge":{"alpha"},"relative_ridge":{"alpha","relative_power"},"lasso":{"alpha"},"elasticnet":{"alpha","l1_ratio"},"mape_lasso":{"alpha"},"relative_huber":{"alpha","delta"},
               "pls":{"n_components"},"pca_ridge":{"alpha","n_components"},"poly_ridge":{"alpha","degree"},"svr":{"C","epsilon","gamma"},"kernel_ridge":{"alpha","gamma"},"gpr":{"length_scale","noise"},
               "expert_tree":{"max_depth","min_samples_leaf"},"extra_trees":{"n_estimators","max_depth","min_samples_leaf","max_features"},
               "gbdt":{"n_estimators","max_depth","min_samples_leaf","learning_rate","subsample"}}
    for model,parameters in ranges.items():
        if model not in supported or set(parameters)-supported[model]: raise ValueError("未知模型参数范围："+model)
        for name,bounds in parameters.items():
            if not isinstance(bounds,list) or len(bounds) not in (2,3) or not np.all(np.isfinite(bounds[:2])) or bounds[0]<=0 or bounds[0]>bounds[1]: raise ValueError("参数范围须为[正下限,上限,可选对数标志]："+name)
    return result


def score(y,p):
    y,p=np.asarray(y,float),np.asarray(p,float)
    if not np.all(np.isfinite(p)) or np.any(p<=0): raise ValueError("价格候选输出无效。")
    sst=float(np.sum((y-y.mean())**2))
    return {"mape_percent":float(np.mean(np.abs(p-y)/y)*100),"mae_wan":float(np.mean(np.abs(p-y))),"rmse_wan":float(np.sqrt(np.mean((p-y)**2))),
            "r2":float(1-np.sum((p-y)**2)/sst) if len(y)>1 and sst>1e-12 else None}


def loss(y,p,alpha=.7):
    r=score(y,p); return alpha*r["mape_percent"]/100+(1-alpha)*(1-r["r2"] if r["r2"] is not None else 0.)


def targets_met(metrics,quality):
    return metrics["r2"] is not None and metrics["mape_percent"]<=quality["mape_percent_max"] and metrics["r2"]>=quality["r2_min"]


def blend(y,bank,config,repeats=1):
    e=config["ensemble"]; count=bank.shape[1]; alpha=e["alpha"]
    if e['robust']:
        from tools.product_r43_search import robust_blend
        return robust_blend(y,bank,config,repeats)
    if e["min_members"]>1:
        from tools.product_r42_search import bounded_blend
        return bounded_blend(y,bank,config)
    y=np.asarray(y,float); bank=np.asarray(bank,float)
    sst=float(np.sum((y-y.mean())**2)); variance=max(sst,1e-12)
    r2_weight=(1-alpha) if len(y)>1 and sst>1e-12 else 0.
    def batch_loss(predictions):
        return alpha*np.mean(np.abs(predictions-y[:,None])/y[:,None],axis=0)+r2_weight*np.sum((predictions-y[:,None])**2,axis=0)/variance
    def batch_feasible(predictions):
        q=config["quality"]
        return ((np.mean(np.abs(predictions-y[:,None])/y[:,None],axis=0)*100<=q["mape_percent_max"]) &
                (1-np.sum((predictions-y[:,None])**2,axis=0)/variance>=q["r2_min"]) & (len(y)>1 and sst>1e-12))
    singles=batch_loss(bank)
    feasible=np.flatnonzero(batch_feasible(bank)).tolist()
    first=min(feasible,key=lambda i:singles[i]) if feasible else int(np.argmin(singles))
    chosen=[first]; current=bank[:,first]; value=float(singles[first])
    while len(chosen)<min(e["max_members"],count):
        best=None
        remaining=[i for i in range(count) if i not in chosen]
        for weight in (.1,.25,.5):
            proposed=(1-weight)*current[:,None]+weight*bank[:,remaining]; values=batch_loss(proposed)
            if targets_met(score(y,current),config["quality"]):
                values[~batch_feasible(proposed)]=float("inf")
            local=int(np.argmin(values)); candidate=float(values[local])
            if best is None or candidate<best[0]: best=(candidate,remaining[local],proposed[:,local])
        if best is None or not np.isfinite(best[0]) or best[0]>=value-1e-9: break
        value,index,current=best; chosen.append(index)
    selected=bank[:,chosen]; k=len(chosen); reference=np.full(k,1./k)
    if k==1: return chosen,reference,selected[:,0]
    # Absolute percentage error epigraph plus normalized SSE and weight shrinkage.
    scaled=selected/y[:,None]; variance=max(float(np.sum((y-y.mean())**2)),1e-12)
    def objective(z):
        w=z[:k]; residual=selected.dot(w)-y
        return float(alpha*np.mean(z[k:])+r2_weight*residual.dot(residual)/variance+e["regularization"]*np.sum((w-reference)**2))
    def gradient(z):
        w=z[:k]; g=2*r2_weight*selected.T.dot(selected.dot(w)-y)/variance+2*e["regularization"]*(w-reference)
        return np.r_[g,np.full(len(y),alpha/len(y))]
    equality=np.r_[np.ones(k),np.zeros(len(y))]
    lower=np.column_stack([-scaled,np.eye(len(y))]); upper=np.column_stack([scaled,np.eye(len(y))])
    constraints=[{"type":"eq","fun":lambda z: z[:k].sum()-1,"jac":lambda z:equality},
                 {"type":"ineq","fun":lambda z: z[k:]-(scaled.dot(z[:k])-1),"jac":lambda z:lower},
                 {"type":"ineq","fun":lambda z: z[k:]+(scaled.dot(z[:k])-1),"jac":lambda z:upper}]
    initial=np.r_[reference,np.abs(scaled.dot(reference)-1)+1e-9]
    fit=minimize(objective,initial,jac=gradient,method="SLSQP",bounds=[(0,1)]*k+[(0,None)]*len(y),constraints=constraints,
                 options={"maxiter":500,"ftol":1e-10})
    weights=fit.x[:k] if fit.success and np.min(fit.x[k:]-np.abs(scaled.dot(fit.x[:k])-1))>=-1e-6 else reference
    # Keep simple alternatives if optimized shrinkage worsens the business loss.
    alternatives=[weights,reference]+list(np.eye(k))
    # If a tested alternative meets both targets, do not discard it for a
    # lower scalar loss that violates one of those targets.
    feasible=[w for w in alternatives if targets_met(score(y,selected.dot(w)),config["quality"])]
    weights=min(feasible or alternatives,key=lambda w:loss(y,selected.dot(w),alpha))
    return chosen,weights,selected.dot(weights)


def sample_config(trial,name,config,has_core):
    a=config["search"]; pipe=config["pipeline"]
    views=[v for v in pipe["feature_views"] if v!="expert_core" or (has_core and config["expert"]["enabled"])]
    if not views: raise ValueError("专家子集没有可用评分，请保留all视图或提供评分。")
    view=trial.suggest_categorical("view",views)
    target=trial.suggest_categorical("target",pipe["target_transforms"])
    strength=trial.suggest_categorical("expert_strength",a["expert_strengths"] if config["expert"]["enabled"] else [0.])
    def parameter(key,lo,hi,log=False,integer=False):
        bounds=a["parameter_ranges"].get(name,{}).get(key,[lo,hi,log]); lo,hi=bounds[:2]; logarithmic=bool(bounds[2]) if len(bounds)>2 else log
        return trial.suggest_int(name+"_"+key,int(lo),int(hi)) if integer else trial.suggest_float(name+"_"+key,float(lo),float(hi),log=logarithmic)
    p={}
    if name in ("ridge","relative_ridge"):
        p={"alpha":trial.suggest_categorical("ridge_alpha",a["ridge_alphas"])} if not a["parameter_ranges"].get(name) else {"alpha":parameter("alpha",.001,1000,True)}
        if name=="relative_ridge": p["relative_power"]=parameter("relative_power",1.,2.)
    elif name in ("lasso","elasticnet","mape_lasso","relative_huber"):
        p={"alpha":parameter("alpha",.0001,1.,True)}
        if name=="elasticnet": p["l1_ratio"]=parameter("l1_ratio",.2,.9)
        if name=="relative_huber": p["delta"]=parameter("delta",.05,.2)
    elif name=="pls": p={"n_components":parameter("n_components",1,8,integer=True)}
    elif name in ("pca_ridge","poly_ridge"):
        p={"alpha":parameter("alpha",1e-8,1e4,True)}
        if name=="pca_ridge": p["n_components"]=parameter("n_components",1,10,integer=True)
        else:
            p["degree"]=parameter("degree",2,3,integer=True); view="screened"
    elif name=="svr":
        p={"kernel":trial.suggest_categorical("svr_kernel",["linear","rbf","poly"] if a["wide_kernels"] else ["linear","rbf"]),"C":parameter("C",.1,100,True),"epsilon":parameter("epsilon",.005,.1,True),"gamma":parameter("gamma",.001,.1,True)}
    elif name=="kernel_ridge": p={"kernel":trial.suggest_categorical("kr_kernel",["rbf","laplacian","poly","linear"]) if a["wide_kernels"] else "rbf","alpha":parameter("alpha",.01,10,True),"gamma":parameter("gamma",.001,.1,True)}
    elif name=="gpr": p={"length_scale":parameter("length_scale",1,20,True),"noise":parameter("noise",.01,.5,True),"optimize":a["optimize_gp"],"kernel":trial.suggest_categorical("gp_kernel",["matern","rbf"]) if a["wide_kernels"] else "matern"}
    elif name in ("expert_tree","extra_trees","gbdt"):
        p={"max_depth":parameter("max_depth",1,4,integer=True),"min_samples_leaf":parameter("min_samples_leaf",1,3,integer=True)}
        if name!="expert_tree": p["n_estimators"]=parameter("n_estimators",32,128,integer=True)
        if name=="extra_trees": p["max_features"]=parameter("max_features",.4,1.)
        if name=="gbdt": p.update(learning_rate=parameter("learning_rate",.02,.2,True),subsample=parameter("subsample",.7,1.))
    if name in ("mape_lasso","relative_huber"): target="identity"
    scenarios=["consensus"]
    if config["expert"]["compare_individual_experts"] and config["expert"]["enabled"]:
        scenarios+=sorted({record["expert_id"] for record in config["expert"]["records"]})[:3]
    scenario=trial.suggest_categorical("expert_scenario",scenarios)
    overrides={record["field_id"]:record["score"] for record in config["expert"]["records"] if scenario!="consensus" and record["expert_id"]==scenario}
    field_strengths={key:config["expert"]["group_strengths"].get(group,1.) for key,group in config["expert"]["attribute_groups"].items()}
    candidate={"model":name,"parameters":p,"view":view,"target_transform":target,"expert_strength":strength,"expert_scenario":scenario,"score_overrides":overrides,
            "core_threshold":pipe["expert_core_threshold"],"field_strengths":field_strengths,
            "imputation":pipe["imputation"],"scaler":pipe["scaler"],"sample_weight_strength":config["seed_search"]["sample_weight_strength"],
            "screening":pipe["screening"],"select_fraction":pipe["select_fraction"]}
    if view=="screened": candidate["select_k"]=trial.suggest_categorical("select_k",pipe["select_k"])
    return candidate


def input_diagnostics(X,y,specs,scores,groups=None):
    """Describe evidence and suspect fields without changing rows or targets."""
    fields=[]; warnings=[]
    for j,spec in enumerate(specs):
        values=X[:,j]; observed=values[np.isfinite(values)]; unique=len(np.unique(observed))
        flags=[]
        if unique<=1: flags.append("常量或全缺失，折内会移除")
        if spec["value_type"]=="enum" and unique>=max(8,.8*len(y)): flags.append("类别几乎逐条唯一，请确认是否编号")
        if len(observed)<.7*len(y): flags.append("超过30%记录缺失")
        fields.append({"field_id":spec["parameter_id"],"label":spec["label"],"type":spec["value_type"],"expert_score":float(scores[j]),
                       "observed":len(observed),"unique":unique,"flags":flags})
    if X.shape[1]>=len(y): warnings.append("属性数不少于记录数，必须使用折内降维/筛选与正则化；单轮开发指标可能偏乐观。")
    if np.std(y)/np.mean(y)<.1: warnings.append("价格变动较小，R²对少数误差十分敏感；同时查看每条百分比误差。")
    if len(y)<30: warnings.append("独立成品少于30条，无法仅凭此表认证真实业务精度。")
    duplicates=[]; best_ape=0.; within_sse=0.
    if groups is not None:
        for group in np.unique(groups):
            values=y[groups==group]
            best_ape+=float(np.sum(np.abs(values-weighted_median(values,1/values))/values))
            within_sse+=float(np.sum((values-values.mean())**2))
            if len(values)>1 and np.ptp(values)>1e-12:
                duplicates.append({"group":int(group),"records":len(values),"min_price":float(values.min()),"max_price":float(values.max())})
    if duplicates: warnings.append("存在完全相同属性却不同价格的记录，现有属性无法解释这部分差异。")
    sst=float(np.sum((y-y.mean())**2))
    return {"records":len(y),"attributes":len(specs),"price_min":float(np.min(y)),"price_max":float(np.max(y)),
            "price_cv":float(np.std(y)/np.mean(y)),"fields":fields,"warnings":warnings,"conflicting_groups":duplicates,
            "identical_inputs_mape_lower_bound_percent":best_ape/len(y)*100,
            "identical_inputs_r2_upper_bound":1-within_sse/sst if sst>1e-12 else None}


class BudgetEnd(RuntimeError): pass


class InnerSearch(object):
    def __init__(self,X,y,groups,specs,scores,config,deadline,directory,progress):
        self.X,self.y,self.groups,self.specs,self.scores=X,y,groups,specs,(scores if config["expert"]["enabled"] else [3.]*len(scores))
        self.config,self.deadline,self.directory,self.progress=copy.deepcopy(config),deadline,Path(directory),progress
        self.directory.mkdir(parents=True,exist_ok=True); self.prepared={}; self.predictions={}; self.fits=0; self.cache_hits=0
        self.started=time.monotonic(); self.last_progress=self.started; self.phase="基准与参数筛查"; self.timings={}; self.restarts=0
        v=config["validation"]
        from tools.product_r42_search import folds
        self.folds=folds(X,y,groups,v,"inner")
        coverage=np.zeros(len(y),int)
        for unused,test in self.folds:coverage[test]+=1
        if len(set(coverage.tolist()))!=1 or coverage[0]<1:raise ValueError('重复CV覆盖不完整。')
        self.repeats=int(coverage[0]);self.folds_per_repeat=len(self.folds)//self.repeats
        self.eval_y=np.tile(y,self.repeats);self.eval_groups=np.tile(groups,self.repeats)
        # Prediction identity contains data, partitions and estimator code.
        # Increasing seed/time limits or changing a progress interval must not
        # invalidate already computed predictions for the same candidate.
        partitions=[(train.tolist(),test.tolist()) for train,test in self.folds]
        identity=hashlib.sha256(X.tobytes()+y.tobytes()+groups.tobytes()+json.dumps([specs,scores,partitions],sort_keys=True,ensure_ascii=False).encode("utf-8")+
                                Path(__file__).read_bytes()+Path(__import__('services.price_service.r4_estimators',fromlist=['x']).__file__).read_bytes()+
                                Path(__import__('services.price_service.r43_support',fromlist=['x']).__file__).read_bytes()+sklearn.__version__.encode()).hexdigest()
        cache=Path(config["resources"]["cache_directory"] or self.directory/"cache"); cache.mkdir(parents=True,exist_ok=True)
        self.cache_path=cache/(identity+".npz")
        if self.cache_path.is_file():
            with np.load(str(self.cache_path),allow_pickle=False) as old:
                if old["values"].shape[1]==len(self.eval_y):
                    self.predictions={str(k):v for k,v in zip(old["keys"],old["values"])}
    def check(self):
        if time.monotonic()>self.deadline: raise BudgetEnd("训练时间预算已用完。")
    def heartbeat(self,detail="",force=False):
        now=time.monotonic()
        if force or now-self.last_progress>=self.config["resources"]["progress_interval_seconds"]:
            self.progress("R4.3 %s：%s，累计%d次拟合，已用%.1f秒。"%(self.phase,detail,self.fits,now-self.started))
            self.last_progress=now
    def flush(self):
        if self.predictions:
            np.savez_compressed(str(self.cache_path),keys=np.asarray(list(self.predictions)),values=np.asarray(list(self.predictions.values())))
    def oof(self,candidate,seed):
        return self.oof_repeated(candidate,seed).reshape(self.repeats,len(self.y)).mean(axis=0)
    def risk(self,prediction):
        from tools.product_r43_search import risk_loss
        return risk_loss(self.eval_y,prediction,self.config['ensemble']['alpha'],self.repeats,self.config['ensemble']['stability_penalty'] if self.config['ensemble']['robust'] else 0.)
    def oof_repeated(self,candidate,seed):
        candidate=dict(candidate)
        for name in ("screening","select_fraction","stability_resamples","stability_fraction","stability_correlation"): candidate.setdefault(name,self.config["pipeline"][name])
        if not self.config["expert"]["enabled"]:
            candidate["expert_strength"]=0.; candidate["score_overrides"]={}; candidate["field_strengths"]={}
            if candidate.get("view")=="expert_core": candidate["view"]="all"
        key=json.dumps([candidate,int(seed)],sort_keys=True)
        if key in self.predictions:
            self.cache_hits+=1; return self.predictions[key]
        prediction=np.zeros((self.repeats,len(self.y))); counts=np.zeros_like(prediction)
        for fold,(train,test) in enumerate(self.folds):
            self.check()
            if set(self.groups[train])&set(self.groups[test]): raise AssertionError("业务组越过验证边界。")
            self.heartbeat("%s，内层折%d/%d"%(candidate["model"],fold+1,len(self.folds)))
            transform_key=json.dumps([candidate.get(k) for k in ("view","expert_strength","imputation","scaler","core_threshold","field_strengths","score_overrides","select_k","screening","select_fraction","stability_resamples","stability_fraction","stability_correlation")]+[candidate.get("target_transform") if candidate.get("view")=="screened" else None],sort_keys=True)+str(fold)
            if transform_key not in self.prepared:
                p=R4PriceRegressor(self.specs,self.scores,candidate).prepare(self.X[train],self.y[train])
                self.prepared[transform_key]=(p,p.transform(self.X[train]),p.transform(self.X[test]))
            p,Z,Ztest=self.prepared[transform_key]
            started=time.monotonic()
            fitted=R4PriceRegressor(self.specs,self.scores,candidate,int(seed)).fit_prepared(p,Z,self.y[train],self.groups[train])
            repeat=fold//self.folds_per_repeat
            prediction[repeat,test]=fitted.predict_prepared(Ztest); counts[repeat,test]+=1; self.fits+=1
            timing=self.timings.setdefault(candidate["model"],{"fit_calls":0,"seconds":0.})
            timing["fit_calls"]+=1; timing["seconds"]+=time.monotonic()-started
        if np.any(counts==0): raise AssertionError("CV未覆盖所有训练记录。")
        prediction=(prediction/counts).reshape(-1)
        score(self.eval_y,prediction); self.predictions[key]=prediction; return prediction
    def run(self):
        config=self.config; a=config["search"]; s=config["seed_search"]; alpha=config["ensemble"]["alpha"]
        rows=[]; failures=[]; seen=set(); has_core=config["expert"]["enabled"] and any(x>=config["pipeline"]["expert_core_threshold"] for x in self.scores)
        baseline={"model":"baseline","parameters":{},"view":"all","expert_strength":0.,"target_transform":"identity"}
        rows.append({"config":baseline,"seeds":[0],"oofs":[self.oof_repeated(baseline,0)]})
        warming=False;warm_until=self.deadline
        def add(candidate):
            if warming and time.monotonic()>=warm_until:raise BudgetEnd('预热预算已用完，转入后续搜索。')
            candidate=dict(candidate)
            for name in ("screening","select_fraction","stability_resamples","stability_fraction","stability_correlation"): candidate.setdefault(name,config["pipeline"][name])
            if not config["expert"]["enabled"]:
                candidate.update(expert_strength=0.,score_overrides={},field_strengths={})
                if candidate.get("view")=="expert_core": candidate["view"]="all"
            signature=json.dumps(candidate,sort_keys=True)
            if signature in seen: return None
            seen.add(signature)
            try:
                prediction=self.oof_repeated(candidate,0); rows.append({"config":candidate,"seeds":[0],"oofs":[prediction]})
                return self.risk(prediction)
            except BudgetEnd: raise
            except Exception as exc:
                failures.append({"config":candidate,"error":str(exc)}); return None
        def evaluate(trial):
            name=trial.suggest_categorical("model",config["models"])
            try:value=add(sample_config(trial,name,config,has_core))
            except BudgetEnd:
                trial.study.stop();raise optuna.TrialPruned('search_time_budget')
            if value is None: raise optuna.TrialPruned("重复或失败的参数组合")
            return value
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study=optuna.create_study(sampler=optuna.samplers.TPESampler(seed=a['seed']))
        for name in config['models']:
            strength=1. if has_core else 0.
            if strength not in a['expert_strengths']:strength=a['expert_strengths'][0]
            view='expert_core' if has_core and name in ('ridge','lasso','mape_lasso','pls') else 'all'
            if view not in config['pipeline']['feature_views']:view=config['pipeline']['feature_views'][0]
            preset={'model':name,'expert_strength':strength,'view':view,'expert_scenario':'consensus'}
            if name=='ridge' and not a['parameter_ranges'].get(name):preset.update(ridge_alpha=a['ridge_alphas'][0],target=config['pipeline']['target_transforms'][0])
            study.enqueue_trial(preset)
        initial_trials=0
        if a['family_first']:
            study.optimize(evaluate,n_trials=len(config['models']),timeout=max(0.,self.deadline-time.monotonic()))
            initial_trials=len([t for t in study.trials if t.state!=optuna.trial.TrialState.WAITING])
        warm_until=self.started+(self.deadline-self.started)*a['warm_start_fraction']*(.5 if a['family_first'] and a['wide_kernels'] else 1.)
        warming=True
        # Cover shrinkage and feature assumptions systematically before dividing
        # a small TPE budget among many model families. All ranking uses this fold.
        if a["warm_start"]:
            pipe=config["pipeline"]; views=[v for v in pipe["feature_views"] if v!="expert_core" or has_core]
            strengths=sorted(set(a["expert_strengths"])); has_prior=np.ptp(self.scores)>1e-12
            try:
                for view in views:
                    strength_values=strengths[::max(1,len(strengths)-1)] if has_prior and view=="all" else [strengths[-1] if view=="expert_core" else strengths[0]]
                    counts=pipe["select_k"] if view=="screened" else [None]
                    for strength in strength_values:
                        for k in counts:
                            for target in pipe["target_transforms"]:
                                for model in ("ridge","relative_ridge"):
                                    if model not in config["models"]: continue
                                    bounds=a["parameter_ranges"].get(model,{}).get("alpha")
                                    alphas=sorted(set(np.geomspace(bounds[0],bounds[1],5).tolist()+[x for x in a["ridge_alphas"] if bounds[0]<=x<=bounds[1]])) if bounds else a["ridge_alphas"]
                                    if model=="relative_ridge": alphas=alphas[::max(1,len(alphas)//3)]
                                    for penalty in alphas:
                                        parameters={"alpha":float(penalty)}
                                        if model=="relative_ridge":
                                            rb=a["parameter_ranges"].get(model,{}).get("relative_power",[1.,1.])
                                            parameters["relative_power"]=float(rb[0])
                                        candidate={"model":model,"parameters":parameters,"view":view,"target_transform":target,"expert_strength":strength,
                                                   "imputation":pipe["imputation"],"scaler":pipe["scaler"],"core_threshold":pipe["expert_core_threshold"],
                                                   "field_strengths":{key:config["expert"]["group_strengths"].get(group,1.) for key,group in config["expert"]["attribute_groups"].items()},
                                                   "sample_weight_strength":s["sample_weight_strength"]}
                                        if k is not None: candidate["select_k"]=k
                                        add(candidate)
                # Sparse objectives need their own path. A good Ridge path
                # cannot substitute for L1 selection when p > n, and a random
                # warm trial can otherwise miss the neutral expert branch.
                for model in ("lasso","elasticnet","mape_lasso"):
                    if model not in config["models"]: continue
                    bounds=a["parameter_ranges"].get(model,{}).get("alpha")
                    penalties=np.geomspace(bounds[0],bounds[1],5).tolist() if bounds else [.0003,.001,.003,.01,.03,.1]
                    for strength in ([strengths[0],strengths[-1]] if has_prior else [strengths[0]]):
                        for target in (["identity"] if model=="mape_lasso" else pipe["target_transforms"]):
                            for penalty in penalties:
                                if "all" not in views: continue
                                parameters={"alpha":float(penalty)}
                                if model=="elasticnet":
                                    rb=a["parameter_ranges"].get(model,{}).get("l1_ratio",[.5,.5])
                                    parameters["l1_ratio"]=float(np.mean(rb[:2]))
                                add({"model":model,"parameters":parameters,"view":"all","target_transform":target,"expert_strength":strength,
                                     "imputation":pipe["imputation"],"scaler":pipe["scaler"],"core_threshold":pipe["expert_core_threshold"],
                                     "field_strengths":{key:config["expert"]["group_strengths"].get(group,1.) for key,group in config["expert"]["attribute_groups"].items()},
                                     "sample_weight_strength":s["sample_weight_strength"]})
            except BudgetEnd: pass
        if a["wide_kernels"]:
            # A broad parameter box alone still misses useful model families.
            # Cover small bases/kernel scales explicitly before TPE refinement.
            pipe=config["pipeline"]
            warm_until=self.started+(self.deadline-self.started)*a['warm_start_fraction']
            try:
                for name in ("poly_ridge","pca_ridge","svr","kernel_ridge","gpr"):
                    if name not in config["models"]: continue
                    views=[("all",None)] if name=="pca_ridge" else [("all",None),("screened",1),("screened",2),("screened",5)]
                    for view,k in views:
                        if name=="poly_ridge" and view=="all":continue
                        if view not in pipe["feature_views"]:continue
                        for target in pipe["target_transforms"]:
                            if name=="poly_ridge": parameters=[{"alpha":alpha,"degree":2} for alpha in (1e-6,.001,.1,10.)]
                            elif name=="pca_ridge": parameters=[{"alpha":alpha,"n_components":n} for n in (1,2,4,8) for alpha in (.001,.1,10.)]
                            elif name=="svr": parameters=[{"C":C,"gamma":g,"epsilon":.001,"kernel":"rbf"} for C in (1.,100.,10000.) for g in (.001,.1,1.)]
                            elif name=="kernel_ridge": parameters=[{"alpha":alpha,"gamma":g,"kernel":kernel} for alpha in (1e-6,.01,1.) for g in (.01,.1,1.) for kernel in ("rbf","laplacian")]
                            else: parameters=[{"length_scale":1.,"noise":noise,"kernel":"matern","optimize":a["optimize_gp"]} for noise in (.001,.1)]
                            for parameters_used in parameters:
                                ranges=a["parameter_ranges"].get(name,{})
                                if any(key in parameters_used and not bound[0]<=parameters_used[key]<=bound[1] for key,bound in ranges.items()):continue
                                candidate={"model":name,"parameters":parameters_used,"view":view,"target_transform":target,"expert_strength":0.,
                                           "imputation":pipe["imputation"],"scaler":pipe["scaler"],"sample_weight_strength":s["sample_weight_strength"]}
                                if k is not None:candidate["select_k"]=k
                                add(candidate)
            except BudgetEnd:pass
        warming=False
        try: study.optimize(evaluate,n_trials=max(0,a['anchor_trials']-initial_trials),timeout=max(0.,self.deadline-time.monotonic()))
        except BudgetEnd: pass
        means=lambda r:np.mean(r["oofs"],axis=0)
        routing={'mode':'not_requested'}
        if config['diagnostics']['sample_influence']:
            best_row=min(rows,key=lambda r:self.risk(means(r)))
            fitted=R4PriceRegressor(self.specs,self.scores,best_row['config'],0).fit(self.X,self.y,self.groups)
            training_metric=score(self.y,fitted.predict(self.X));cv_metric=score(self.eval_y,means(best_row))
            gap=cv_metric['mape_percent']-training_metric['mape_percent']
            mode='high_variance' if gap>max(3.,.5*config['quality']['mape_percent_max']) else 'underfit_or_missing_information' if training_metric['mape_percent']>config['quality']['mape_percent_max'] else 'mixed_or_acceptable'
            routing={'mode':mode,'training':training_metric,'validation':cv_metric,'gap_mape_pp':gap,'interpretation':'heuristic scout within current outer training data'}
            if mode=='high_variance' and config['ensemble']['robust']:
                config['ensemble']['regularization']*=3.;config['ensemble']['stability_penalty']*=1.5
                routing['action']='increased ensemble shrinkage and repeat-stability penalty'
            elif mode=='underfit_or_missing_information':
                routing['action']='extra parameter search before seed perturbation'
                try:study.optimize(evaluate,n_trials=a['restart_trials'])
                except BudgetEnd:pass
        def promoted():
            ranked=sorted([r for r in rows if r["config"]["model"] not in ("baseline","pls","gpr") and r["seeds"]==[0]],key=lambda r:self.risk(means(r)))
            if not ranked: return []
            best=self.risk(means(ranked[0]))
            competitive=[r for r in ranked if self.risk(means(r))<=best+max(.01,.25*best)]
            def cost(row):
                t=self.timings.get(row["config"]["model"],{}); return t.get("seconds",0.)/max(1,t.get("fit_calls",0))
            quickest=min(cost(r) for r in competitive)
            affordable=[r for r in competitive if cost(r)<=max(.005,quickest*a["seed_cost_ratio"])]
            # The best candidate survives the speed filter; bad or expensive
            # runners-up do not receive thousands of blind repeated fits.
            ordered=[ranked[0]]+[r for r in affordable if r is not ranked[0]]
            chosen=[]; names=set()
            for r in ordered:
                if r["config"]["model"] in names: continue
                names.add(r["config"]["model"]); chosen.append(r)
                if len(chosen)>=a["promote"]: break
            return chosen
        # Preserve unperturbed candidates in the bank as explicit fallbacks.
        def seed_rows():
            selected=[]
            for reference in promoted():
                row={"config":reference["config"],"seeds":[0],"oofs":list(reference["oofs"])}
                rows.append(row); selected.append(row)
            return selected
        eligible=seed_rows(); self.phase="固定参数种子探索"; self.heartbeat("参数筛查完成%d组，内层%d折"%(len(seen),len(self.folds)),True)
        cap=s["max_trials"] or s["limits"][s["profile"]]
        stream=np.random.SeedSequence(s["base"]).generate_state(cap)
        attempted=completed=failed=0; stale=0; reason="configured_cap"; next_check=None
        initial_bank=np.column_stack([means(r) for r in rows]); unused,initial_weights,initial_pred=blend(self.eval_y,initial_bank,config,self.repeats)
        best_before=self.risk(initial_pred)
        milestones=sorted(set([min(cap,x) for x in s["milestones"]]+[cap]))
        with (self.directory/"seed_trials.jsonl").open("w",encoding="utf-8") as ledger:
            for index,seed in enumerate(stream):
                if not eligible: reason="no_eligible_stochastic_candidate"; break
                try: self.check()
                except BudgetEnd: reason="time_budget"; break
                row=eligible[index%len(eligible)]; attempted+=1
                self.phase="固定参数种子%d/%d探索"%(attempted,cap)
                signature=hashlib.sha256(json.dumps(row["config"],sort_keys=True).encode("utf-8")).hexdigest()
                record={"attempt":attempted,"seed":int(seed),"model":row["config"]["model"],"configuration_sha256":signature}
                try:
                    prediction=self.oof_repeated(row["config"],int(seed)); completed+=1
                    options=list(zip(row["seeds"],row["oofs"]))+[(int(seed),prediction)]
                    options=sorted(options,key=lambda item:self.risk(item[1]))[:s["members_per_configuration"]]
                    old=self.risk(means(row)); proposed=np.mean([item[1] for item in options],axis=0)
                    accepted=self.risk(proposed)<old-1e-10
                    if accepted: row["seeds"],row["oofs"]=list(zip(*options)); row["seeds"],row["oofs"]=list(row["seeds"]),list(row["oofs"])
                    record.update(status="completed",accepted=accepted,metrics=score(self.eval_y,prediction))
                except BudgetEnd: record.update(status="incomplete"); reason="time_budget"
                except Exception as exc: failed+=1; record.update(status="failed",error=str(exc))
                ledger.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+"\n")
                if reason=="time_budget": break
                if attempted in milestones or attempted==next_check or (s["early_stop"] and attempted%s["check_interval"]==0):
                    bank=np.column_stack([means(r) for r in rows]); unused,w,p=blend(self.eval_y,bank,config,self.repeats); value=self.risk(p)
                    stale=stale+1 if best_before-value<s["minimum_gain"] else 0; best_before=min(best_before,value)
                    self.flush(); self.progress("R4.3 种子%d/%d：内层开发MAPE %.3f%%，R² %s；不是本轮外层审核分数。"%(attempted,cap,score(self.eval_y,p)["mape_percent"],score(self.eval_y,p)["r2"]))
                    current_metrics=score(self.eval_y,p); quality=config["quality"]
                    if s["early_stop"] and s["stop_on_targets"] and attempted>=s["minimum_trials"] and current_metrics["r2"] is not None and current_metrics["mape_percent"]<=quality["mape_percent_max"] and current_metrics["r2"]>=quality["r2_min"]:
                        reason="development_targets_reached"; break
                    if s["early_stop"] and attempted>=s["minimum_trials"] and stale>=s["patience"]:
                        if not targets_met(current_metrics,quality) and self.restarts<a["max_restarts"]:
                            self.restarts+=1; self.phase="未达标，重新选择超参数"
                            self.heartbeat("第%d次，增加%d次选参，保留已冻结的种子候选"%(self.restarts,a["restart_trials"]),True)
                            try: study.optimize(evaluate,n_trials=a["restart_trials"])
                            except BudgetEnd: reason="time_budget"; break
                            eligible=seed_rows(); stale=0; self.phase="固定参数种子探索"
                            next_check=min(cap,attempted+s["minimum_trials"])
                        elif s["stop_when_unmet_stagnant"] or targets_met(current_metrics,quality):
                            reason="targets_unmet_after_parameter_restart" if not targets_met(current_metrics,quality) else "development_improvement_stable"
                            break
                        else:
                            stale=0
                            self.heartbeat("当前未达标；按配置继续至种子/时间预算，未因停滞退出",True)
                    if self.restarts and reason=="configured_cap": next_check=min(cap,attempted+s["minimum_trials"])
        bank=np.column_stack([means(r) for r in rows]); indices,weights,prediction=blend(self.eval_y,bank,config,self.repeats)
        chosen=[rows[i] for i in indices]
        calibration=1.
        if config["ensemble"]["calibration"]:
            raw=weighted_median(self.eval_y/prediction,prediction/self.eval_y); lo,hi=config["ensemble"]["calibration_bounds"]
            candidates=[1.,float(np.clip((1+raw)/2,lo,hi)),float(np.clip(raw,lo,hi))]
            feasible=[v for v in candidates if targets_met(score(self.eval_y,v*prediction),config["quality"])]
            calibration=min(feasible or candidates,key=lambda v:self.risk(v*prediction)); prediction=prediction*calibration
        anchor_index=min(range(bank.shape[1]),key=lambda i:(not targets_met(score(self.eval_y,bank[:,i]),config['quality']),self.risk(bank[:,i]))) if config['ensemble']['robust'] else min(range(bank.shape[1]),key=lambda i:self.risk(bank[:,i]))
        anchor_metrics=score(self.eval_y,bank[:,anchor_index])
        if config['ensemble']['robust']:
            after=score(self.eval_y,prediction);e=config['ensemble']
            if (targets_met(anchor_metrics,config['quality']) and not targets_met(after,config['quality'])) or after['mape_percent']>anchor_metrics['mape_percent']+e['max_mape_delta_pp']+1e-8 or (after['r2'] is not None and after['r2']<anchor_metrics['r2']-e['max_r2_drop']-1e-8) or self.risk(prediction)>self.risk(bank[:,anchor_index])+1e-8:
                indices=[anchor_index];chosen=[rows[anchor_index]];weights=np.ones(1);prediction=bank[:,anchor_index];calibration=1.
        from tools.product_r43_search import repeat_summary
        validation_summary=repeat_summary(self.y,prediction,self.repeats)
        fusion_diagnostics={'anchor_metrics':anchor_metrics,'ensemble_metrics':score(self.eval_y,prediction),'minimum_requested_members':config['ensemble']['min_members'],
                            'active_members':int(np.sum(weights>1e-8)),'regularization':config['ensemble']['regularization'],'repeat_stability_penalty':config['ensemble']['stability_penalty'],
                            'minimum_count_met':len(chosen)>=config['ensemble']['min_members'],'policy':'nondegrading robust blend' if config['ensemble']['robust'] else 'legacy bounded blend'}
        weight_stability=None
        if config["ensemble"]["jackknife"] and len(chosen)>1 and time.monotonic()<self.deadline:
            coefficients=[]
            for group in np.unique(self.groups):
                mask=self.eval_groups!=group
                selected,w,p=blend(self.eval_y[mask],bank[mask][:,indices],config,self.repeats)
                full=np.zeros(len(chosen)); full[selected]=w; coefficients.append(full)
            weight_stability={"std":np.std(coefficients,axis=0).tolist(),"interpretation":"固定折外预测的删除敏感性，不是独立验证。"}
        self.flush()
        self.heartbeat("停止原因=%s，目标%s；已完成%d/%d次种子评估"%(reason,"达到" if targets_met(score(self.eval_y,prediction),config["quality"]) else "未达到",completed,cap),True)
        write_json(self.directory/"anchor_candidates.json",[{"configuration":r["config"],"seeds":r["seeds"],"metrics":score(self.eval_y,means(r))} for r in rows])
        coverage={'family_first':a['family_first'],'warm_start_fraction':a['warm_start_fraction'],'attempted_models':sorted(self.timings),'requested_models':list(config['models']),
                  'unattempted_models':[name for name in config['models'] if name not in self.timings]}
        write_json(self.directory/"search_diagnostics.json",{"inner_folds":len(self.folds),"per_model":self.timings,"parameter_restarts":self.restarts,"failures":failures,'routing':routing,'fusion':fusion_diagnostics,'validation':validation_summary,'coverage':coverage})
        return {"rows":chosen,"weights":weights,"oof":prediction.reshape(self.repeats,len(self.y)).mean(axis=0),"validation_oof":prediction,"validation_repeats":self.repeats,"calibration":calibration,"failures":failures,"anchor_completed":len(rows)-1,
                "seed_report":{"attempted":attempted,"completed":completed,"failed":failed,"incomplete":attempted-completed-failed,
                               "cap":cap,"stop_reason":reason,"scope":"一次尝试为一组固定参数在完整内层验证上的种子/样本扰动评估",
                               "fit_calls":self.fits,"cache_hits":self.cache_hits,"export_seed_counts":[len(r["seeds"]) for r in chosen]},
                "weight_stability":weight_stability,'validation_summary':validation_summary,'fusion_diagnostics':fusion_diagnostics,"diagnostics":{"inner_folds":len(self.folds),"per_model":self.timings,"parameter_restarts":self.restarts,'routing':routing,'coverage':coverage}}


def train_price_r4(data,output_dir,config_path=None,progress=None,overrides=None):
    config=load_config(config_path,overrides); output=Path(output_dir); output.mkdir(parents=True,exist_ok=True)
    from tools.product_r4_input import apply_field_types
    apply_field_types(data,config["expert"]["field_types"])
    progress=progress or (lambda text:print(text,flush=True)); started=time.monotonic(); deadline=started+config["resources"]["total_seconds"]
    frame,y,specs,mappings,groups,ids=price_dataset(data,config["validation"]["minimum_groups"]); X=frame.values
    unknown=set(config["expert"]["scores"])-set(frame.columns)
    if unknown and config["expert"]["enabled"]: raise ValueError("价格评分引用未知属性："+",".join(sorted(unknown)))
    raw_scores=[float(config["expert"]["scores"].get(p["parameter_id"],3.)) for p in specs]
    scores=raw_scores if config["expert"]["enabled"] else [3.]*len(specs)
    diagnostics=input_diagnostics(X,y,specs,scores,groups); write_json(output/"input_diagnostics.json",diagnostics)
    progress("R4.3 输入检查：%d条、%d属性；价格%.6g—%.6g万元。"%(len(y),len(specs),y.min(),y.max()))
    from tools.product_r42_search import folds
    v=config["validation"]; outer=folds(X,y,groups,v,"outer")
    audit_sum=np.zeros(len(y)); audit_count=np.zeros(len(y)); baseline_sum=np.zeros(len(y)); audit=np.full(len(y),np.nan); audits=[]; incomplete=False
    expected_counts=np.zeros(len(y))
    for unused,test in outer: expected_counts[test]+=1
    outer_repeats=int(expected_counts[0]);outer_folds_per_repeat=len(outer)//outer_repeats
    audit_repeated=np.full((outer_repeats,len(y)),np.nan);baseline_repeated=np.full_like(audit_repeated,np.nan)
    per_search=config["resources"]["per_search_seconds"]
    with threadpool_limits(limits=1):
        for index,(train,test) in enumerate(outer):
            progress("R4完整外层审核 %d/%d：训练%d条，审核%d条。"%(index+1,len(outer),len(train),len(test)))
            try:
                search_deadline=min(deadline,time.monotonic()+per_search) if per_search is not None else deadline
                selection=InnerSearch(X[train],y[train],groups[train],specs,scores,config,search_deadline,output/"nested"/("fold_%02d"%index),progress).run()
                models=[]
                for row in selection["rows"]:
                    fitted=[R4PriceRegressor(specs,scores,row["config"],seed).fit(X[train],y[train],groups[train]) for seed in row["seeds"]]
                    models.append(MeanPriceRegressor(fitted,selection["calibration"]))
                current=np.column_stack([m.predict(X[test]) for m in models]).dot(selection["weights"])
                repetition=index//outer_folds_per_repeat
                audit_repeated[repetition,test]=current;baseline_repeated[repetition,test]=np.mean(y[train])
                audit_sum[test]+=current; audit_count[test]+=1
                baseline_sum[test]+=np.mean(y[train]); audit[test]=audit_sum[test]/audit_count[test]
                audits.append({"train_ids":[ids[i] for i in train],"validation_ids":[ids[i] for i in test],"seed_report":selection["seed_report"],"diagnostics":selection["diagnostics"]})
                progress("R4.3 外层审核%d/%d完成：本轮平均百分比误差%.3f%%；单条审核不计算R²。"%(index+1,len(outer),np.mean(np.abs(y[test]-current)/y[test])*100))
                if selection["seed_report"]["stop_reason"]=="time_budget" and per_search is None: incomplete=True; break
            except BudgetEnd: incomplete=True; break
        # A completed audit is never used to alter the predeclared search policy.
        progress("R4按相同策略进行全量开发选择和最终模型拟合。")
        # Reserve a fresh bounded final stage if audit used up its budget; disclose it.
        final_deadline=max(deadline,time.monotonic()+min(120.,config["resources"]["total_seconds"]))
        if per_search is not None: final_deadline=min(final_deadline,time.monotonic()+per_search)
        selection=InnerSearch(X,y,groups,specs,scores,config,final_deadline,output/"development",progress).run()
        variables={}; ledger=[]
        for index,(row,weight) in enumerate(zip(selection["rows"],selection["weights"])):
            if weight<=1e-8: continue
            name="R4_C%03d"%index
            fitted=[R4PriceRegressor(specs,scores,row["config"],seed).fit(X,y,groups) for seed in row["seeds"]]
            variables[name]=MeanPriceRegressor(fitted,selection["calibration"])
            ledger.append({"name":name,"configuration":index,"label":LABELS[row["config"]["model"]],"family":FAMILIES[row["config"]["model"]],"weight":float(weight),
                           "seeds":row["seeds"],"seed_instances":len(fitted),"target_transform":row["config"]["target_transform"],"expert_strength":row["config"].get("expert_strength",0),"configuration_parameters":row["config"]})
        product=data["products"][0]; path=output/"price_native_bundle.pkl"
        metadata=native_field_metadata(frame,specs,mappings)
        for field in metadata.values():
            if not field["required"]: field["missing_policy"]="model_imputer"
        bundle=export_from_notebook(dict(variables,X_train=frame),output=path,product_code=product["product_code"],product_name=product["product_name"],model_version="prototype-automl-r4-price-priors",
             target_divisor_to_wan=1,field_metadata=metadata,model_variables={name:name for name in variables},ensemble_model_names=list(variables),
             ensemble_weights=[item["weight"] for item in ledger],strict=True,model_source="namespace",model_output_transform="direct")
        fully_audited=bool(not incomplete and np.all(np.isfinite(audit)) and np.array_equal(audit_count,expected_counts))
        from tools.product_r43_search import repeat_summary
        audit_summary=repeat_summary(y,audit_repeated.reshape(-1),outer_repeats) if fully_audited else None
        audit_score=audit_summary['pooled'] if fully_audited else {"mape_percent":None,"r2":None,"mae_wan":None}
        dev_score=selection['validation_summary']['pooled']; q=config["quality"]
        met=lambda s:s["mape_percent"] is not None and s["r2"] is not None and s["mape_percent"]<=q["mape_percent_max"] and s["r2"]>=q["r2_min"]
        ratings=[{"field_id":p["parameter_id"],"label":p["label"],"score":raw_scores[i],"enabled":config["expert"]["enabled"],"specified":p["parameter_id"] in config["expert"]["scores"],
                  "group":config["expert"]["attribute_groups"].get(p["parameter_id"]),"multiplier_full_strength":float(2**((scores[i]-3)/2))} for i,p in enumerate(specs)]
        bundle["automl_explanation"]={"member_ledger":ledger,"expert_groups":[],"engineering":{},"field_labels":{p["parameter_id"]:p["label"] for p in specs},
              "price_attribute_scores":ratings,"expert_records":config["expert"]["records"],"expert_scores_enabled":config["expert"]["enabled"],"calibration":selection["calibration"],"weight_stability":selection["weight_stability"],
              "evaluation_scope":"complete_nested_grouped_selection" if fully_audited else "incomplete_audit_trial_model",
              "evidence_sufficient":fully_audited and len(np.unique(groups))>=q["minimum_groups_for_evidence"],"sample_warning":"微样本试用模型；专家价格评分与效能偏好独立，真实精度尚需业务验证。"}
        residuals=np.abs(np.tile(y,outer_repeats)-audit_repeated.reshape(-1)) if fully_audited else np.abs(np.tile(y,selection['validation_repeats'])-selection['validation_oof'])
        bundle["residual_calibration"]={"space":"native","half_width_native":float(np.quantile(residuals,.95)),"coverage":None,"source":"outer_oof_uncalibrated" if fully_audited else "development_uncalibrated"}
        if config['trust_region']['enabled']:
            from services.price_service.r43_support import PriceTrustRegion
            bundle['price_trust_region']=PriceTrustRegion(config['trust_region']['threshold_factor']).fit(X,specs,y)
            bundle['price_risk_policy']=dict(config['trust_region'])
        hard_gate_failed=q["hard_gate"] and (not fully_audited or not met(audit_score) or len(ledger)<config["ensemble"]["min_members"])
        save_bundle(path,bundle); native=load_bundle(path)
        records={row["agreement_id"]:row for row in data["agreements"]}
        memory=np.column_stack([model.predict(X[:4]) for model in variables.values()]).dot(np.asarray([x["weight"] for x in ledger]))
        deltas=[abs(predict(native,records[ids[i]]["params"])["predicted_price_wan"]-memory[i]) for i in range(min(4,len(X)))]
        if max(deltas)>1e-5: raise AssertionError("R4原生包与内存预测不一致。")
        cv={"mean_mape":dev_score["mape_percent"],"mean_r2":dev_score["r2"],"std_mape":dev_score.get('std_mape'),"std_r2":dev_score.get('std_r2')}
        audit_cv={"mean_mape":audit_score["mape_percent"],"mean_r2":audit_score["r2"],"std_mape":audit_score.get('std_mape'),"std_r2":audit_score.get('std_r2')}
        training_predictions=np.column_stack([m.predict(X) for m in variables.values()]).dot(np.asarray([r["weight"] for r in ledger]))
        training_score=score(y,training_predictions)
        report={"engine":"r4","implementation_version":"r4.3","selected_model":"价格交叉验证集成" if not config["expert"]["enabled"] else "专家价格属性与相对误差集成","representative_seed":None,"seed_selection":"fixed_parameters_with_weighted_seed_exploration",
                "training_rows":len(y),"feature_count":len(specs),"independent_groups":len(np.unique(groups)),"member_count":sum(x["seed_instances"] for x in ledger),
                "configuration_count":len(ledger),"member_ledger":ledger,"feature_order":list(frame.columns),"cv_metrics":cv,"nested_cv_metrics":audit_cv,
                "accuracy_constraints":{"price_std_wan":float(np.std(y)),"rmse_max_for_target_r2_wan":float(np.sqrt(max(0.,1-q["r2_min"]))*np.std(y)),
                                        "actual_outer_rmse_wan":audit_score.get("rmse_wan"),"formula":"R2 >= target requires RMSE <= sqrt(1-target)*std(price)"},
                "holdout_metrics":{"mape_percent":None,"r2":None,"scope":"external_test_not_provided"},"audit_complete":fully_audited,"targets_met":fully_audited and met(audit_score) and len(ledger)>=config["ensemble"]["min_members"],
                "cv_targets_met":met(dev_score),"holdout_targets_met":None,"business_accuracy_certified":False,"export_refitted_on_all_valid_rows":True,
                "selected_configurations":[r["config"] for r in selection["rows"]],"weights":selection["weights"].tolist(),"calibration":selection["calibration"],
                "seed_report":selection["seed_report"],"failed_candidates":selection["failures"],"native_equivalence_max_delta_wan":max(deltas),
                "elapsed_seconds":round(time.monotonic()-started,2),"warning":bundle["automl_explanation"]["sample_warning"],"targets":q,
                "search_diagnostics":selection["diagnostics"],"input_diagnostics":diagnostics,"expert_scores_enabled":config["expert"]["enabled"],
                "training_fit_metrics":training_score,"baseline_outer_metrics":score(np.tile(y,outer_repeats),baseline_repeated.reshape(-1)) if fully_audited else None,
                "generalization_gap_mape_pp":audit_score["mape_percent"]-training_score["mape_percent"] if fully_audited else None,
                "validation_policy":dict(v,outer_total_folds=len(outer),aggregation="mean error across independent repetitions; never score only the averaged prediction",per_search_seconds=per_search),
                'development_repetitions':selection['validation_summary'],'outer_repetitions':audit_summary,'fusion_diagnostics':selection['fusion_diagnostics'],
                "ensemble_requirement_met":len(ledger)>=config["ensemble"]["min_members"],"quality_gate_rejected":bool(hard_gate_failed)}
        write_json(output/"training_report.json",report); write_json(output/"seed_report.json",selection["seed_report"])
        write_json(output/"effective_config.json",config); write_json(output/"price_attribute_scores.json",{"attributes":ratings,"records":config["expert"]["records"]})
        write_json(output/"nested_audit.json",{"complete":fully_audited,"folds":audits,"metrics":audit_score,'repetitions':audit_summary,"records":[{"id":ids[i],"true_price":float(y[i]),"prediction":float(audit[i]) if np.isfinite(audit[i]) else None,
                   "absolute_percentage_error":float(np.mean(np.abs(y[i]-audit_repeated[:,i])/y[i])*100) if fully_audited else None,
                   'predictions_by_repetition':[float(x) if np.isfinite(x) else None for x in audit_repeated[:,i]],"validation_count":int(audit_count[i])} for i in range(len(y))]})
        write_json(output/"weight_report.json",{"weights":selection["weights"].tolist(),"calibration":selection["calibration"],"stability":selection["weight_stability"],"expert_scores_scope":"price_attributes"})
        write_json(output/"explanation.json",bundle["automl_explanation"])
        manifest_path=Path(str(path)+".manifest.json"); manifest=json.loads(manifest_path.read_text(encoding="utf-8")); manifest.update(bundle_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),automl_explanation=bundle["automl_explanation"])
        write_json(manifest_path,manifest)
        if config['diagnostics']['sample_influence']:
            from tools.product_r43_diagnostics import influence_report
            write_json(output/'sample_influence.json',influence_report(X,y,groups,specs,ids,q))
            write_json(output/'feature_stability.json',{'scope':'fitted final models, with fold-local selection during validation',
                'configurations':[{'name':name,'members':[getattr(m.preparation_,'selection_details_',None) for m in model.models]} for name,model in variables.items()]})
        if fully_audited:
            progress("R4.3 最终完整外层审核：MAPE %.3f%%，R² %s；精度及融合数量目标%s。内层开发MAPE %.3f%%。"%(audit_score["mape_percent"],audit_score["r2"],"达到" if report['targets_met'] else "未达到",dev_score["mape_percent"]))
        else: progress("R4.3 外层审核未完成；本次导出为试用模型，不能报告审核通过。")
        if hard_gate_failed: raise ValueError("完整审核精度和融合数量要求未同时达到；旧运行版本保留。诊断报告："+str(output/"training_report.json"))
        return bundle,report
