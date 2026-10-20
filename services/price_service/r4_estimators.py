# -*- coding: utf-8 -*-
"""Fitted R4 estimators, usable in the pinned Python3.8/sklearn0.24 runtime."""
import warnings
import numpy as np
from scipy.optimize import linprog, minimize
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.preprocessing import OneHotEncoder
from sklearn.linear_model import Lasso, ElasticNet, BayesianRidge, ARDRegression
from sklearn.cross_decomposition import PLSRegression
from sklearn.svm import SVR
from sklearn.kernel_ridge import KernelRidge
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern
from sklearn.ensemble import ExtraTreesRegressor, GradientBoostingRegressor
from sklearn.decomposition import PCA
from sklearn.preprocessing import PolynomialFeatures,StandardScaler
from sklearn.gaussian_process.kernels import RBF,ConstantKernel


def weighted_median(values, weights):
    order = np.argsort(values, kind="mergesort")
    total = np.cumsum(np.asarray(weights)[order])
    return float(np.asarray(values)[order[min(np.searchsorted(total, total[-1] / 2), len(order)-1)]])


class PricePreparation(object):
    """Fold-fitted transforms with explicit original-attribute ownership."""
    def __init__(self, specs, scores, strength=1., view="all", imputation="median", scaler="standard", core_threshold=4., field_strengths=None, select_k=None, screening="pearson", select_fraction=.5, stability_resamples=0, stability_fraction=.8, stability_correlation=.98):
        self.specs, self.scores, self.strength = specs, scores, strength
        self.view, self.imputation, self.scaler = view, imputation, scaler
        self.core_threshold, self.field_strengths = core_threshold, field_strengths or {}
        self.select_k = select_k
        self.screening,self.select_fraction=screening,select_fraction
        self.stability_resamples,self.stability_fraction,self.stability_correlation=stability_resamples,stability_fraction,stability_correlation

    def fit(self, X, y=None):
        X = np.asarray(X, float)
        self.indices = [i for i in range(len(self.specs)) if self.view != "expert_core" or self.scores[i] >= self.core_threshold]
        if not self.indices:
            raise ValueError("没有评分至少4分的价格属性，不能使用专家子集候选。")
        self.defaults, self.parts, self.owners = {}, [], []
        strengths = np.asarray([self.strength*self.field_strengths.get(p["parameter_id"],1.) for p in self.specs])
        multipliers = np.power(2., strengths * (np.asarray(self.scores) - 3.) / 2.)
        multipliers /= np.exp(np.mean(np.log(multipliers[self.indices])))
        self.multipliers = multipliers
        for i in self.indices:
            column = X[:, i]; observed = column[np.isfinite(column)]
            kind = self.specs[i]["value_type"]
            if kind in ("boolean", "enum"):
                if len(observed):
                    values, counts = np.unique(observed, return_counts=True)
                    default = float(values[np.argmax(counts)])
                else:
                    default = 0.
            else:
                default = float((np.mean if self.imputation == "mean" else np.median)(observed)) if len(observed) else 0.
            self.defaults[i] = default
            filled = np.where(np.isfinite(column), column, default)
            if kind == "enum":
                sparse_key = "sparse_output" if "sparse_output" in OneHotEncoder().get_params() else "sparse"
                encoder = OneHotEncoder(handle_unknown="ignore", **{sparse_key: False}).fit(filled[:, None])
                count = len(encoder.categories_[0]); factor = multipliers[i] / np.sqrt(count)
                self.parts.append((i, encoder, 0., 1., factor))
                self.owners.extend([i] * count)
            else:
                center = float(np.median(filled) if self.scaler == "robust" else np.mean(filled))
                scale = float(np.quantile(filled, .75)-np.quantile(filled, .25)) if self.scaler == "robust" else float(np.std(filled))
                if self.scaler == "none": center, scale = 0., 1.
                scale = max(scale, 1e-12) if scale > 1e-12 else 1.
                self.parts.append((i, None, center, scale, multipliers[i])); self.owners.append(i)
        matrix = self.transform(X, filter_constants=False)
        self.kept = np.flatnonzero(np.var(matrix, axis=0) > 1e-14)
        self.selection_details_ = None
        if self.view == "screened":
            if y is None or self.select_k is None:
                raise ValueError("折内属性筛选需要训练价格和select_k。")
            target = np.asarray(y, float) - np.mean(y)
            centered = matrix - np.mean(matrix, axis=0)
            denominator = np.sqrt(np.sum(centered**2, axis=0)*target.dot(target))
            correlations = np.abs(centered.T.dot(target))/np.maximum(denominator, 1e-12)
            if self.screening=="nonlinear":
                for alternative in (centered**2,np.abs(centered)):
                    alternative=alternative-alternative.mean(axis=0)
                    denom=np.sqrt(np.sum(alternative**2,axis=0)*target.dot(target))
                    correlations=np.maximum(correlations,np.abs(alternative.T.dot(target))/np.maximum(denom,1e-12))
            ranks = {}
            for j in self.kept:
                owner = self.owners[j]
                ranks[owner] = max(ranks.get(owner, 0.), float(correlations[j]*np.sqrt(multipliers[owner])))
            if self.screening=="nonlinear":
                for owner in self.indices:
                    if self.specs[owner]["value_type"] in ("enum","boolean"):continue
                    raw=np.where(np.isfinite(X[:,owner]),X[:,owner],self.defaults[owner])
                    raw=raw/max(1.,float(np.max(np.abs(raw))))
                    for alternative in (raw**2,np.abs(raw)):
                        alternative=alternative-alternative.mean()
                        denominator=np.sqrt(alternative.dot(alternative)*target.dot(target))
                        value=float(abs(alternative.dot(target))/max(denominator,1e-12))
                        if owner in ranks:ranks[owner]=max(ranks[owner],value*np.sqrt(multipliers[owner]))
            ordered = sorted(ranks, key=lambda owner: (-ranks[owner], owner))
            stability=None
            if self.stability_resamples:
                from services.price_service.r43_support import stable_fields
                raw_owners=[i for i in self.indices if self.specs[i]['value_type'] not in ('enum','boolean')]
                raw=np.column_stack([np.where(np.isfinite(X[:,i]),X[:,i],self.defaults[i])/max(1.,np.max(np.abs(np.where(np.isfinite(X[:,i]),X[:,i],self.defaults[i])))) for i in raw_owners]) if raw_owners else np.empty((len(X),0))
                ordered,stability=stable_fields(matrix[:,self.kept],[self.owners[j] for j in self.kept],raw,raw_owners,y,multipliers,int(self.select_k),self.stability_resamples,self.stability_fraction,self.stability_correlation)
            chosen = set(ordered[:min(int(self.select_k), max(1, int(len(X)*self.select_fraction)), len(ordered))])
            self.kept = np.asarray([j for j in self.kept if self.owners[j] in chosen], dtype=int)
            self.selection_details_ = {"scope":"training_fold_only", "selected_fields":[self.specs[i]["parameter_id"] for i in ordered if i in chosen]}
            if stability is not None:
                stability['groups']=[dict(g,field_ids=[self.specs[i]['parameter_id'] for i in g['members']]) for g in stability['groups']]
                stability['fields']=[dict(f,field_id=self.specs[f['index']]['parameter_id']) for f in stability['fields']]
                self.selection_details_['stability']=stability
        self.transformed_owners = [self.owners[i] for i in self.kept]
        self.output_dimensions = max(1, len(self.kept))
        return self

    def transform(self, X, filter_constants=True):
        X = np.asarray(X, float); pieces = []
        for i, encoder, center, scale, factor in self.parts:
            filled = np.where(np.isfinite(X[:, i]), X[:, i], self.defaults[i])
            pieces.append(encoder.transform(filled[:, None]) * factor if encoder is not None else ((filled-center)/scale*factor)[:, None])
        matrix = np.column_stack(pieces)
        if not filter_constants: return matrix
        return matrix[:, self.kept] if len(self.kept) else np.zeros((len(X), 1))


class DualRidge(BaseEstimator, RegressorMixin):
    """Exact weighted Ridge in the smaller sample-space system."""
    def __init__(self, alpha=1.): self.alpha = alpha
    def fit(self, X, y, sample_weight=None):
        X, y = np.asarray(X, float), np.asarray(y, float)
        w = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, float)
        mean_x = np.average(X, axis=0, weights=w); mean_y = np.average(y, weights=w)
        Z = (X-mean_x)*np.sqrt(w)[:, None]
        gram = Z.dot(Z.T)
        # Solve coefficients and degrees of freedom together, reusing one factorization.
        solved = np.linalg.solve(gram+self.alpha*np.eye(len(y)), np.column_stack([(y-mean_y)*np.sqrt(w), gram]))
        dual = solved[:, 0]
        self.coef_ = Z.T.dot(dual); self.intercept_ = float(mean_y-mean_x.dot(self.coef_))
        self.effective_df_ = float(1+np.trace(solved[:, 1:]))
        return self
    def predict(self, X): return np.asarray(X).dot(self.coef_)+self.intercept_


class RelativeRegression(BaseEstimator, RegressorMixin):
    def __init__(self, alpha=.001, kind="mape", delta=.1, max_iter=800):
        self.alpha, self.kind, self.delta, self.max_iter = alpha, kind, delta, max_iter
    def fit(self, X, y, sample_weight=None):
        X, y = np.asarray(X, float), np.asarray(y, float)
        n, p = X.shape; D = np.column_stack([np.ones(n), X])
        w = np.ones(n) if sample_weight is None else np.asarray(sample_weight, float)
        w = w*n/w.sum()
        if self.kind == "mape":
            # Variables: intercept/coefficients, absolute residuals, abs(coefficients).
            c = np.r_[np.zeros(p+1), w/y/n, np.full(p, self.alpha)]
            A = np.vstack([np.column_stack([D,-np.eye(n),np.zeros((n,p))]),
                           np.column_stack([-D,-np.eye(n),np.zeros((n,p))]),
                           np.column_stack([np.zeros(p),np.eye(p),np.zeros((p,n)),-np.eye(p)]),
                           np.column_stack([np.zeros(p),-np.eye(p),np.zeros((p,n)),-np.eye(p)])])
            b = np.r_[y,-y,np.zeros(2*p)]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = linprog(c, A_ub=A, b_ub=b, bounds=[(None,None)]*(p+1)+[(0,None)]*(n+p),
                                 method="interior-point", options={"maxiter": self.max_iter, "tol":1e-7,"autoscale":True})
            if not result.success or np.max(A.dot(result.x)-b)>1e-5:
                raise ValueError("相对绝对误差求解未收敛："+str(result.message))
            coefficients = result.x[:p+1]
        else:
            def objective(beta):
                e=(D.dot(beta)-y)/y; a=np.abs(e); d=self.delta
                losses=np.where(a<=d,.5*e**2,d*(a-.5*d))
                gradient=D.T.dot(w*np.clip(e,-d,d)/y)/n
                gradient[1:] += 2*self.alpha*beta[1:]
                return float(w.dot(losses)/n+self.alpha*beta[1:].dot(beta[1:])),gradient
            initial=np.r_[weighted_median(y,w/y),np.zeros(p)]
            result=minimize(objective,initial,jac=True,method="L-BFGS-B",options={"maxiter":self.max_iter,"ftol":1e-10})
            if not np.all(np.isfinite(result.x)) or (not result.success and np.max(np.abs(result.jac))>1e-3):
                raise ValueError("相对Huber求解未收敛："+str(result.message))
            coefficients=result.x
        self.intercept_,self.coef_=float(coefficients[0]),coefficients[1:]
        return self
    def predict(self,X): return np.asarray(X).dot(self.coef_)+self.intercept_


class BasisRidge(BaseEstimator,RegressorMixin):
    """A fold-fitted low-dimensional or polynomial basis; never fit on test X."""
    def __init__(self,alpha=1.,basis="pca",n_components=3,degree=2):
        self.alpha,self.basis,self.n_components,self.degree=alpha,basis,n_components,degree
    def fit(self,X,y,sample_weight=None):
        if self.basis=="pca": self.mapping_=PCA(n_components=max(1,min(self.n_components,len(y)-1,X.shape[1])),svd_solver="full")
        else:
            if X.shape[1]>32: raise ValueError("多项式候选须先做折内筛选，展开前最多32列。")
            self.mapping_=PolynomialFeatures(degree=self.degree,include_bias=False)
        mapped=self.mapping_.fit_transform(X)
        if mapped.shape[1]>4096: raise ValueError("多项式展开超过4096列。")
        self.scaler_=StandardScaler().fit(mapped)
        self.model_=DualRidge(self.alpha).fit(self.scaler_.transform(mapped),y,sample_weight)
        return self
    def predict(self,X): return self.model_.predict(self.scaler_.transform(self.mapping_.transform(X)))


class WeightedBayesian(BaseEstimator,RegressorMixin):
    def __init__(self,ard=False):self.ard=ard
    def fit(self,X,y,sample_weight=None):
        w=np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight,float)
        mean_x=np.average(X,axis=0,weights=w);mean_y=float(np.average(y,weights=w));root=np.sqrt(w)
        model=(ARDRegression if self.ard else BayesianRidge)(fit_intercept=False,n_iter=300)
        model.fit((X-mean_x)*root[:,None],(y-mean_y)*root)
        self.coef_=model.coef_;self.intercept_=mean_y-mean_x.dot(self.coef_);self.model_=model
        return self
    def predict(self,X):return np.asarray(X).dot(self.coef_)+self.intercept_


class ExpertSplitTree(BaseEstimator, RegressorMixin):
    def __init__(self, scores, max_depth=3, min_samples_leaf=2, expert_strength=1., feature_strengths=None):
        self.scores,self.max_depth,self.min_samples_leaf,self.expert_strength=scores,max_depth,min_samples_leaf,expert_strength
        self.feature_strengths=feature_strengths
    def fit(self,X,y,sample_weight=None):
        X,y=np.asarray(X,float),np.asarray(y,float)
        w=np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight,float)
        def sse(indices):
            return float(np.sum(w[indices]*(y[indices]-np.average(y[indices],weights=w[indices]))**2))
        def build(indices,depth):
            node={"value":float(np.average(y[indices],weights=w[indices])),"samples":len(indices),"min":float(y[indices].min()),"max":float(y[indices].max())}
            parent=sse(indices); best=None
            if depth>=self.max_depth or len(indices)<2*self.min_samples_leaf or parent<=1e-14: return node
            for j in range(X.shape[1]):
                values=np.unique(X[indices,j]); thresholds=(values[:-1]+values[1:])/2
                for threshold in thresholds:
                    left=indices[X[indices,j]<=threshold]; right=indices[X[indices,j]>threshold]
                    if min(len(left),len(right))<self.min_samples_leaf: continue
                    gain=parent-sse(left)-sse(right)
                    if gain<=1e-12: continue
                    factor=1+self.expert_strength*(self.feature_strengths[j] if self.feature_strengths is not None else 1.)*(self.scores[j]-1)/4
                    score=gain*factor
                    if best is None or score>best[0]: best=(score,j,float(threshold),left,right,gain)
            if best:
                score,j,t,left,right,gain=best
                node.update(feature=j,threshold=t,data_gain=gain,expert_gain=score,left=build(left,depth+1),right=build(right,depth+1))
            return node
        self.root_=build(np.arange(len(y)),0); return self
    def predict(self,X):
        values=[]
        for row in np.asarray(X):
            node=self.root_
            while "feature" in node: node=node["left" if row[node["feature"]]<=node["threshold"] else "right"]
            values.append(node["value"])
        return np.asarray(values)


class R4PriceRegressor(BaseEstimator, RegressorMixin):
    def __init__(self,specs,scores,config,seed=0):
        self.specs,self.scores,self.config,self.seed=specs,scores,config,seed
    def prepare(self,X,y=None):
        c=self.config
        effective=[c.get("score_overrides",{}).get(p["parameter_id"],score) for p,score in zip(self.specs,self.scores)]
        target = np.log(y) if y is not None and c.get("target_transform")=="log" else y
        return PricePreparation(self.specs,effective,c.get("expert_strength",1.),c.get("view","all"),c.get("imputation","median"),c.get("scaler","standard"),c.get("core_threshold",4.),c.get("field_strengths"),c.get("select_k"),c.get("screening","pearson"),c.get("select_fraction",.5),c.get('stability_resamples',0),c.get('stability_fraction',.8),c.get('stability_correlation',.98)).fit(X,target)
    def fit(self,X,y,groups=None):
        prep=self.prepare(X,y)
        return self.fit_prepared(prep,prep.transform(X),y,groups)
    def fit_prepared(self,preparation,Z,y,groups=None):
        self.preparation_=preparation; y=np.asarray(y,float); c=self.config; p=dict(c.get("parameters") or {})
        self.price_scale_=float(np.median(y)); self.price_floor_=max(self.price_scale_*1e-8,1e-9)
        self.target_transform_=c.get("target_transform","identity")
        target=np.log(y) if self.target_transform_=="log" else y/self.price_scale_
        self.target_scale_=1. if self.target_transform_=="log" else self.price_scale_
        rho=c.get("sample_weight_strength",0.) if self.seed else 0.
        groups=np.arange(len(y)) if groups is None else np.asarray(groups)
        unique,inverse=np.unique(groups,return_inverse=True)
        rng=np.random.RandomState(self.seed)
        g=rng.exponential(size=len(unique)); group_w=(1-rho)+rho*len(unique)*g/g.sum()
        weights=group_w[inverse]; weights*=len(weights)/weights.sum()
        self.weight_ess_=float(weights.sum()**2/weights.dot(weights))
        self.sample_weights_=weights.copy()
        kind=c["model"]
        if kind=="baseline":
            self.constant_=weighted_median(y,weights/y); self.model_=None; return self
        self.kernel_intercept_ = 0.
        if kind in ("ridge","relative_ridge"):
            if kind=="relative_ridge":
                power = p.pop("relative_power", 1.)
                weights = weights / (y/self.price_scale_)**power
                weights *= len(weights)/weights.sum()
            model=DualRidge(**p)
        elif kind in ("pca_ridge","poly_ridge"):
            model=BasisRidge(basis="pca" if kind=="pca_ridge" else "polynomial",**p)
        elif kind in ('bayesian_ridge','ard'):model=WeightedBayesian(ard=kind=='ard')
        elif kind in ("lasso","elasticnet"):
            model=(Lasso if kind=="lasso" else ElasticNet)(max_iter=4000,**p)
        elif kind in ("mape_lasso","relative_huber"):
            model=RelativeRegression(kind="mape" if kind=="mape_lasso" else "huber",**p)
        elif kind=="svr": model=SVR(max_iter=10000,**p)
        elif kind=="kernel_ridge":
            model=KernelRidge(**p)
            # KernelRidge has no intercept. Without centering, a regularized RBF
            # model shrinks prices (and log prices) toward zero rather than their mean.
            self.kernel_intercept_ = float(np.average(target,weights=weights))
            target = target-self.kernel_intercept_
        elif kind=="pls":
            model=PLSRegression(n_components=min(p.get("n_components",1),Z.shape[1],len(y)-1),scale=False)
        elif kind=="extra_trees": model=ExtraTreesRegressor(random_state=self.seed,n_jobs=1,**p)
        elif kind=="gbdt": model=GradientBoostingRegressor(random_state=self.seed,**p)
        elif kind=="expert_tree":
            owners=preparation.transformed_owners or [0]
            strengths=[c.get("field_strengths",{}).get(self.specs[i]["parameter_id"],1.) for i in owners]
            model=ExpertSplitTree([preparation.scores[i] for i in owners],expert_strength=c.get("expert_strength",1.),feature_strengths=strengths,**p)
        elif kind=="gpr":
            optimize=p.get("optimize",False); bound=(.01,1000.) if optimize else "fixed"
            if p.get("kernel","matern")=="rbf": kernel=RBF(length_scale=p.get("length_scale",np.sqrt(Z.shape[1])),length_scale_bounds=bound)
            else: kernel=Matern(length_scale=p.get("length_scale",np.sqrt(Z.shape[1])),length_scale_bounds=bound,nu=1.5)
            model=GaussianProcessRegressor(kernel=ConstantKernel(1.,(.01,100.))*kernel if optimize else kernel,alpha=p.get("noise",.1)/weights,
                                          optimizer="fmin_l_bfgs_b" if optimize else None,normalize_y=True,random_state=self.seed)
        else: raise ValueError("未知R4模型："+kind)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if kind in ("pls","gpr"): model.fit(Z,target)
            else: model.fit(Z,target,sample_weight=weights)
        self.model_=model; return self
    def predict(self,X):
        if self.model_ is None: return np.full(len(X),self.constant_)
        return self.predict_prepared(self.preparation_.transform(X))
    def predict_prepared(self,Z):
        if self.model_ is None: return np.full(len(Z),self.constant_)
        raw=np.asarray(self.model_.predict(Z)).reshape(-1)+getattr(self,"kernel_intercept_",0.)
        if not np.all(np.isfinite(raw)): raise ValueError("价格预测包含非有限值。")
        price=np.exp(np.clip(raw,-40,40)) if self.target_transform_=="log" else raw*self.target_scale_
        return np.maximum(price,self.price_floor_)
    def description(self):
        return {"model":self.config["model"],"seed":self.seed,"target_transform":self.target_transform_,
                "expert_strength":self.config.get("expert_strength",1.),"weight_ess":self.weight_ess_,
                "transformed_dimensions":self.preparation_.output_dimensions,
                "effective_df":getattr(self.model_,"effective_df_",None),
                "feature_selection":getattr(self.preparation_,"selection_details_",None),
                "kernel_intercept":getattr(self,"kernel_intercept_",0.)}
    def explanation(self,X):
        row=np.asarray(X,float)[0]; preparation=self.preparation_
        result={"configuration":self.config,"seed":self.seed,"kind":self.config["model"],"details":self.description()}
        if self.model_ is not None and hasattr(self.model_,"coef_") and self.config["model"]!="pls":
            coefficients=np.asarray(self.model_.coef_).reshape(-1)
            contributions={}; z=preparation.transform([row])[0]
            for j,owner in enumerate(preparation.transformed_owners):
                contributions[owner]=contributions.get(owner,0.)+float(z[j]*coefficients[j])
            result["attribute_terms"]=[{"field_id":self.specs[i]["parameter_id"],"label":self.specs[i]["label"],"term":v*self.target_scale_,
                                        "space":"log_price" if self.target_transform_=="log" else "万元"} for i,v in contributions.items()]
            result["intercept"]=float(getattr(self.model_,"intercept_",0.))*self.target_scale_
            slopes=[]
            offset=0
            for owner,encoder,center,scale,factor in preparation.parts:
                width=len(encoder.categories_[0]) if encoder is not None else 1
                if encoder is None and offset in preparation.kept:
                    local=int(np.where(preparation.kept==offset)[0][0])
                    slopes.append({"field_id":self.specs[owner]["parameter_id"],"label":self.specs[owner]["label"],
                                   "coefficient":float(coefficients[local]*factor/scale*self.target_scale_),
                                   "space":"log_price_per_unit" if self.target_transform_=="log" else "万元/原单位"})
                offset+=width
            result["original_unit_coefficients"]=slopes
        if isinstance(self.model_,ExpertSplitTree):
            node=self.model_.root_; z=preparation.transform([row])[0]; path=[]
            while "feature" in node:
                col=node["feature"]; owner=preparation.transformed_owners[col]
                part=next(part for part in preparation.parts if part[0]==owner)
                threshold=node["threshold"] if part[1] is not None else part[2]+node["threshold"]*part[3]/part[4]
                category=None
                if part[1] is not None:
                    offset=sum(len(p[1].categories_[0]) if p[1] is not None else 1 for p in preparation.parts[:preparation.parts.index(part)])
                    local_column=int(preparation.kept[col])-offset
                    code=int(part[1].categories_[0][local_column])
                    allowed=__import__('json').loads(self.specs[owner].get("allowed_values_json") or "[]")
                    category=str(allowed[code]) if code<len(allowed) else str(code)
                path.append({"field_id":self.specs[owner]["parameter_id"],"label":self.specs[owner]["label"],"threshold":float(threshold),"category":category,
                             "encoding":"one_hot" if part[1] is not None else "original_unit","expert_score":preparation.scores[owner],
                             "data_gain":node["data_gain"],"expert_gain":node["expert_gain"],"branch":"小于等于" if z[col]<=node["threshold"] else "大于"})
                node=node["left" if z[col]<=node["threshold"] else "right"]
            inverse=lambda v:float(np.exp(v) if self.target_transform_=="log" else v*self.target_scale_)
            result.update(tree_path=path,leaf_samples=node["samples"],leaf_price_range=[inverse(node["min"]),inverse(node["max"])])
        return result


class MeanPriceRegressor(BaseEstimator, RegressorMixin):
    def __init__(self,models,calibration=1.): self.models,self.calibration=models,calibration
    def predict(self,X): return self.calibration*np.mean([model.predict(X) for model in self.models],axis=0)
    def explanation(self,X):
        return {"seed_instances":len(self.models),"calibration":self.calibration,
                "members":[model.explanation(X) for model in self.models[:3]],"member_details_truncated":len(self.models)>3}
