# Robust MAPE + R2 AutoML

Files:
- `automl_search.py`: multi-objective Optuna search
- `requirements.txt`: pinned environment

Example:
```powershell
python automl_search.py --csv your_data.csv --target "价格_万元" --trials 500 --folds 5 --cv-seeds "42,137,256,512,1024" --log-target --target-mape 10 --target-r2 0.85 --output outputs
```

If ID/text columns exist, exclude them first:
```powershell
--drop-cols "id,产品名称,备注"
```

Models searched: Ridge, Lasso, ElasticNet, BayesianRidge, SVR, RandomForest, ExtraTrees, GBDT.

Objectives:
- robust MAPE = mean MAPE + lambda * std(MAPE), minimize
- robust R2 = mean R2 - lambda * std(R2), maximize

The seed is NOT chosen for the best score. The program chooses the seed closest to median behavior across all validation seeds.

Outputs: `all_trials.csv`, `pareto_trials.csv`, `seed_report.csv`, `summary.json`, `final_model.joblib`.

Prediction:
```powershell
python automl_search.py --predict-model outputs/final_model.joblib --predict-csv new_data.csv --output predictions.csv
```

## 0.24 note
This edition uses sklearn 0.24 built-in MAPE, converted to percentage units. sklearn 0.24 supports Python 3.8.
