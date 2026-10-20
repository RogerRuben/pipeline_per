# -*- coding: utf-8 -*-
"""Fold-local stability selection and a small, persisted price trust region."""
import numpy as np


def stable_fields(matrix,owners,raw,raw_owners,y,prior,k,resamples,fraction,correlation,seed=619):
    matrix=np.asarray(matrix,float);y=np.asarray(y,float);owners=list(owners)
    fields=sorted(set(owners));parent={i:i for i in fields}
    def find(i):
        while parent[i]!=i:i=parent[i]
        return i
    centered=matrix-matrix.mean(axis=0)
    norm=np.sqrt(np.sum(centered**2,axis=0));corr=np.abs(centered.T.dot(centered)/np.maximum(norm[:,None]*norm[None,:],1e-12))
    for a in range(len(owners)):
        for b in range(a):
            if owners[a]!=owners[b] and corr[a,b]>=correlation:
                ra,rb=find(owners[a]),find(owners[b]);parent[max(ra,rb)]=min(ra,rb)
    groups={i:find(i) for i in fields};members={g:[i for i in fields if groups[i]==g] for g in set(groups.values())}
    expanded=np.column_stack([matrix,matrix**2,np.abs(matrix),raw**2,np.abs(raw)])
    expanded_owners=owners*3+list(raw_owners)*2
    selected={i:0 for i in fields};group_selected={g:0 for g in members};signs={i:[] for i in fields};means={i:0. for i in fields}
    rng=np.random.RandomState(seed);size=min(len(y),max(4,int(np.ceil(len(y)*fraction))))
    for unused in range(resamples):
        ix=rng.choice(len(y),size,replace=False);target=y[ix]-y[ix].mean();z=expanded[ix]-expanded[ix].mean(axis=0)
        c=z.T.dot(target)/np.maximum(np.sqrt(np.sum(z*z,axis=0)*target.dot(target)),1e-12)
        ranks={i:0. for i in fields}
        for j,i in enumerate(expanded_owners):
            if i in ranks:ranks[i]=max(ranks[i],float(abs(c[j])*np.sqrt(prior[i])))
        seen=set()
        for i in sorted(fields,key=lambda i:(-ranks[i],i)):
            g=groups[i]
            if g in seen:continue
            seen.add(g);selected[i]+=1;group_selected[g]+=1
            if len(seen)>=k:break
        for i in fields:means[i]+=ranks[i]
    representatives=[]
    for g,items in members.items():representatives.append(max(items,key=lambda i:(selected[i],means[i],-i)))
    order=sorted(representatives,key=lambda i:(-group_selected[groups[i]],-selected[i],-means[i],i))
    details={"resamples":resamples,"scope":"current training fold only; selection frequencies are diagnostics, not error-rate guarantees",
             "groups":[{"members":items,"selection_frequency":group_selected[g]/float(resamples)} for g,items in sorted(members.items())],
             "fields":[{"index":i,"selection_frequency":selected[i]/float(resamples),"group_frequency":group_selected[groups[i]]/float(resamples)} for i in fields]}
    return order,details


class PriceTrustRegion(object):
    def __init__(self,threshold_factor=1.25):self.threshold_factor=threshold_factor
    def fit(self,X,specs,prices=None):
        self.reference_=np.asarray(X,float).copy();self.specs_=specs
        self.fill_=np.nanmedian(self.reference_,axis=0);self.fill_=np.where(np.isfinite(self.fill_),self.fill_,0.)
        self.reference_=np.where(np.isfinite(self.reference_),self.reference_,self.fill_)
        self.low_=self.reference_.min(axis=0);self.high_=self.reference_.max(axis=0)
        self.scale_=np.maximum(self.high_-self.low_,np.maximum(np.abs(self.fill_)*1e-6,1e-9))
        self.categorical_=np.asarray([s['value_type'] in ('enum','boolean') for s in specs])
        self.prices_=np.asarray(prices,float) if prices is not None else None
        distances=self.distances(self.reference_);np.fill_diagonal(distances,np.inf)
        nearest=distances.min(axis=1)
        self.threshold_=max(.05,float(np.quantile(nearest,.95))*self.threshold_factor)
        return self
    def distances(self,X):
        X=np.asarray(X,float);X=np.where(np.isfinite(X),X,self.fill_)
        delta=np.abs(X[:,None,:]-self.reference_[None,:,:])/self.scale_
        if np.any(self.categorical_):delta[:,:,self.categorical_]=(X[:,None,self.categorical_]!=self.reference_[None,:,self.categorical_]).astype(float)
        return np.mean(np.minimum(delta,10.),axis=2)
    def assess(self,X):
        X=np.asarray(X,float);filled=np.where(np.isfinite(X),X,self.fill_);distances=self.distances(filled);nearest=distances.min(axis=1)
        output=[]
        for row,distance,d in zip(filled,nearest,distances):
            outside=[]
            for j,spec in enumerate(self.specs_):
                bad=(row[j] not in self.reference_[:,j]) if self.categorical_[j] else row[j]<self.low_[j]-1e-9 or row[j]>self.high_[j]+1e-9
                if bad:outside.append(spec['parameter_id'])
            severity=max(0.,float(distance/self.threshold_-1))+.25*len(outside)
            output.append({'nearest_distance':float(distance),'distance_threshold':self.threshold_,'outside_fields':outside,'is_extrapolation':bool(outside or distance>self.threshold_),
                           'nearest_reference_price_native':float(np.median(self.prices_[d<=distance+1e-9])) if self.prices_ is not None else None,
                           'severity':min(5.,severity),'interpretation':'historical joint-distance risk, not a calibrated probability'})
        return output
