# -*- coding: utf-8 -*-
import numpy as np
from services.price_service.r4_estimators import R4PriceRegressor


def influence_report(X,y,groups,specs,ids,quality):
    config={'model':'ridge','parameters':{'alpha':1.},'view':'all','expert_strength':0.,'target_transform':'identity'}
    scores=[3.]*len(specs);full=R4PriceRegressor(specs,scores,config).fit(X,y,groups);reference=full.predict(X);rows=[]
    for group in np.unique(groups):
        train=groups!=group;test=~train
        fitted=R4PriceRegressor(specs,scores,config).fit(X[train],y[train],groups[train]);prediction=fitted.predict(X)
        drift=float(np.mean(np.abs(prediction[train]-reference[train])/np.maximum(reference[train],1e-12))*100)
        for i in np.flatnonzero(test):
            ape=float(abs(prediction[i]-y[i])/y[i]*100)
            rows.append({'id':ids[i],'reference_loo_prediction':float(prediction[i]),'reference_loo_ape_percent':ape,'other_records_prediction_drift_percent':drift,
                         'review_suggested':bool(drift>10. or ape>2*quality['mape_percent_max'])})
    return {'reference':'fixed neutral Ridge(alpha=1), diagnostic only; no record removed and no outer score used for selection',
            'records':sorted(rows,key=lambda r:-r['other_records_prediction_drift_percent'])}
