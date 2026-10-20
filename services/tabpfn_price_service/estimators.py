# -*- coding: utf-8 -*-
"""Serializable training context; checkpoint is loaded only on the Win11 host."""
from __future__ import print_function

import hashlib
import os
from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, RegressorMixin
from services.price_service.r4_estimators import R4PriceRegressor

ROOT = Path(__file__).resolve().parents[2]


def checkpoint_path(explicit=None):
    configured = os.environ.get("TABPFN_PRICE_CHECKPOINT") or explicit
    if configured:
        path = Path(configured).resolve()
        if not path.is_file():
            raise FileNotFoundError("TabPFN回归权重不存在：" + str(path))
        return path
    for path in (ROOT / "assets" / "model" / "tabpfn-v2-regressor.ckpt",
                 ROOT / "runtime" / "tabpfn_offline_assets" / "model" / "tabpfn-v2-regressor.ckpt"):
        if path.is_file():
            return path
    raise FileNotFoundError("缺少离线TabPFN权重，请设置TABPFN_PRICE_CHECKPOINT。")


class TabPFNContextRegressor(RegressorMixin, BaseEstimator):
    def __init__(self, n_estimators=4, random_state=101, target_transform="identity", output_type="mean",
                 categorical_indices=None, checkpoint=None, checkpoint_sha256=None, threads=2,
                 softmax_temperature=.9, fit_mode="fit_preprocessors", inference_config=None, feature_indices=None):
        self.n_estimators = n_estimators
        self.random_state = random_state
        self.target_transform = target_transform
        self.output_type = output_type
        self.categorical_indices = categorical_indices
        self.checkpoint = checkpoint
        self.checkpoint_sha256 = checkpoint_sha256
        self.threads = threads
        self.softmax_temperature = softmax_temperature
        self.fit_mode = fit_mode
        self.inference_config = inference_config
        self.feature_indices = feature_indices

    def fit(self, X, y):
        self.X_ = np.asarray(X, dtype=float).copy()
        self.y_ = np.asarray(y, dtype=float).copy()
        self.n_features_in_ = self.X_.shape[1]
        indices = getattr(self, "feature_indices", None)
        if indices is not None and (not len(indices) or len(set(indices)) != len(indices) or any(i < 0 or i >= self.n_features_in_ for i in indices)):
            raise ValueError("TabPFN价格属性子集索引无效。")
        if not np.all(np.isfinite(self.y_)) or np.any(self.y_ <= 0):
            raise ValueError("价格须为有限正数。")
        self._model = None
        return self

    def _load(self):
        if self._model is not None:
            return
        import torch
        from tabpfn import TabPFNRegressor
        path = checkpoint_path(self.checkpoint)
        if self.checkpoint_sha256 and hashlib.sha256(path.read_bytes()).hexdigest() != self.checkpoint_sha256:
            raise ValueError("TabPFN模型权重摘要不一致。")
        torch.set_num_threads(int(self.threads))
        indices = getattr(self, "feature_indices", None)
        categorical = self.categorical_indices
        if indices is not None:
            categorical = [j for j, original in enumerate(indices) if original in (categorical or [])]
        self._model = TabPFNRegressor(n_estimators=int(self.n_estimators), random_state=int(self.random_state),
                                     categorical_features_indices=categorical, model_path=path, device="cpu",
                                     n_jobs=1, softmax_temperature=float(self.softmax_temperature),
                                     fit_mode=self.fit_mode, inference_config=self.inference_config)
        targets = np.log(self.y_) if self.target_transform == "log" else self.y_
        self._model.fit(self.X_ if indices is None else self.X_[:, indices], targets)

    def predict(self, X):
        self._load()
        data = np.asarray(X, dtype=float)
        indices = getattr(self, "feature_indices", None)
        values = np.asarray(self._model.predict(data if indices is None else data[:, indices], output_type=self.output_type), dtype=float)
        return np.exp(values) if self.target_transform == "log" else values

    def __getstate__(self):
        state = dict(self.__dict__)
        state["_model"] = None
        state["checkpoint"] = None  # Runtime explicitly supplies its own local checkpoint.
        return state


class AttributePriorRidge(R4PriceRegressor):
    """Reuse the R4 fold-fitted price-attribute prior in the modern ensemble."""
    def explanation(self, X):
        return {"members": [super(AttributePriorRidge, self).explanation(X)], "seed_instances": 1, "calibration": 1.0}
