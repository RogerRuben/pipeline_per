# -*- coding: utf-8 -*-
"""Average risk across independent CV repetitions, with guarded ensemble gains."""
import numpy as np
from scipy.optimize import minimize


def metrics(y,p):
    y,p=np.asarray(y,float),np.asarray(p,float);sst=float(np.sum((y-y.mean())**2))
    return {'mape_percent':float(np.mean(np.abs(p-y)/y)*100),'r2':float(1-np.sum((p-y)**2)/sst) if sst>1e-12 else None,'mae_wan':float(np.mean(np.abs(p-y))),'rmse_wan':float(np.sqrt(np.mean((p-y)**2)))}


def repeat_summary(y,p,repeats):
    y,p=np.asarray(y,float),np.asarray(p,float);matrix=p.reshape(repeats,len(y))
    rows=[metrics(y,row) for row in matrix];pooled=metrics(np.tile(y,repeats),p)
    pooled.update(std_mape=float(np.std([r['mape_percent'] for r in rows])),std_r2=float(np.std([r['r2'] for r in rows])) if all(r['r2'] is not None for r in rows) else None)
    return {'pooled':pooled,'repetitions':rows,'averaged_prediction_metrics':metrics(y,matrix.mean(axis=0)),'aggregation':'mean error across independent CV repetitions; averaged-prediction metrics are diagnostic only'}


def risk_loss(y,p,alpha,repeats,penalty):
    y,p=np.asarray(y,float),np.asarray(p,float);n=len(y)//repeats;values=[]
    for a,b in zip(y.reshape(repeats,n),p.reshape(repeats,n)):
        m=metrics(a,b);values.append(alpha*m['mape_percent']/100+(1-alpha)*(1-m['r2']) if m['r2'] is not None else alpha*m['mape_percent']/100)
    return float(np.mean(values)+penalty*np.std(values))


def robust_blend(y,bank,config,repeats=1):
    y,bank=np.asarray(y,float),np.asarray(bank,float);e=config['ensemble'];alpha=e['alpha'];penalty=e['stability_penalty']
    objective=lambda p:risk_loss(y,p,alpha,repeats,penalty)
    q=config['quality']
    def qualified(p):
        m=metrics(y,p)
        return m['r2'] is not None and m['mape_percent']<=q['mape_percent_max'] and m['r2']>=q['r2_min']
    order=sorted(range(bank.shape[1]),key=lambda i:(not qualified(bank[:,i]),objective(bank[:,i])))[:e['candidate_pool']]
    anchor=order[0];anchor_metrics=metrics(y,bank[:,anchor]);anchor_loss=objective(bank[:,anchor]);chosen=[anchor];weights=np.ones(1)
    # An extremely poor predictor must not sneak in with a tiny weight merely
    # to correct the anchor's bias on these few development records.
    order=[i for i in order if i==anchor or objective(bank[:,i])<=anchor_loss*e['candidate_loss_ratio']+e['candidate_loss_slack']]
    def admissible(p):
        m=metrics(y,p)
        if qualified(bank[:,anchor]) and not qualified(p):return False
        return (m['mape_percent']<=anchor_metrics['mape_percent']+e['max_mape_delta_pp']+1e-8 and
                (m['r2'] is None or m['r2']>=anchor_metrics['r2']-e['max_r2_drop']-1e-8) and objective(p)<=anchor_loss+1e-9)
    while len(chosen)<min(e['max_members'],len(order)):
        current=bank[:,chosen].dot(weights);proposals=[]
        for i in order:
            if i in chosen or any(np.allclose(bank[:,i],bank[:,j],rtol=1e-9,atol=1e-10) for j in chosen):continue
            best=min(((objective((1-t)*current+t*bank[:,i]),t) for t in (.03,.1,.25,.5,.75,.9,.97)),key=lambda x:x[0])
            proposals.append((best[0],i,best[1]))
        if not proposals:break
        improvement=None
        for unused,index,t in sorted(proposals)[:4]:
            ids=chosen+[index];selected=bank[:,ids];k=len(ids);reference=np.full(k,1./k)
            start=np.r_[(1-t)*weights,t]
            error=(selected-y[:,None])/y[:,None]
            centered=error-error.mean(axis=0);norm=np.sqrt(np.sum(centered**2,axis=0))
            corr=centered.T.dot(centered)/np.maximum(norm[:,None]*norm[None,:],1e-12);np.fill_diagonal(corr,1.)
            collinearity=float((np.sum(np.abs(corr))-k)/max(1,k*(k-1)))
            matrices=error.reshape(repeats,len(y)//repeats,k) if repeats>1 else np.asarray([np.delete(error,i,axis=0) for i in range(min(len(y),16))])
            perturb_weights=[]
            for matrix in matrices:
                gram=matrix.T.dot(matrix)/max(1,len(matrix))+.001*np.eye(k)
                w=np.maximum(np.linalg.solve(gram,np.ones(k)),0.)
                perturb_weights.append(w/w.sum() if w.sum()>0 else reference)
            instability=float(np.mean(np.std(perturb_weights,axis=0)))
            shrink=e['regularization']*(1+max(0.,collinearity)*k+5*instability)
            def fit_objective(w):return objective(selected.dot(w))+shrink*np.sum((w-reference)**2)
            candidates=[start,reference]
            fit=minimize(fit_objective,start,method='SLSQP',bounds=[(0.,1.)]*k,constraints=[{'type':'eq','fun':lambda w:w.sum()-1,'jac':lambda w:np.ones(k)}],options={'maxiter':100,'ftol':1e-8})
            if fit.success and np.all(np.isfinite(fit.x)) and abs(fit.x.sum()-1)<1e-6:candidates.append(fit.x)
            # Keep complementary pair solutions even if shrinkage prefers a worse mix.
            for candidate in candidates:
                pred=selected.dot(candidate);loss=objective(pred)
                if not admissible(pred) or loss>=objective(current)-e['minimum_gain']:continue
                if improvement is None or loss<improvement[0]:improvement=(loss,ids,candidate)
        if improvement is None:break
        unused,chosen,weights=improvement
        keep=weights>1e-6;chosen=[i for i,active in zip(chosen,keep) if active];weights=weights[keep];weights/=weights.sum()
    return chosen,weights,bank[:,chosen].dot(weights)
