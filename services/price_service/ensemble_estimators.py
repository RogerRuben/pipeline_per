# -*- coding: utf-8 -*-
"""Pickle-stable estimators requiring only the existing price runtime stack."""
import numpy as np
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin, clone
from sklearn.feature_selection import f_regression
from sklearn.neighbors import KNeighborsRegressor
from sklearn.decomposition import PCA
from sklearn.linear_model import Ridge


class EffectiveDFRidge(BaseEstimator, RegressorMixin):
    """Scale regularization by dimension and cap fitted linear degrees of freedom."""
    def __init__(self, alpha=1., max_effective_df=3.):
        self.alpha, self.max_effective_df = alpha, max_effective_df
    def fit(self, X, y):
        X = np.asarray(X, dtype=float)
        eigenvalues = np.linalg.svd(X - np.mean(X, axis=0), compute_uv=False) ** 2
        self.df_limit_ = min(float(self.max_effective_df), max(1.1, len(X) / 2.))
        def degrees(alpha):
            return 1. + float(np.sum(eigenvalues / (eigenvalues + alpha)))
        alpha = float(self.alpha) * max(1, X.shape[1])
        if degrees(alpha) > self.df_limit_:
            low, high = alpha, alpha
            while degrees(high) > self.df_limit_:
                high *= 2.
            for _ in range(40):
                midpoint = (low + high) / 2.
                if degrees(midpoint) > self.df_limit_:
                    low = midpoint
                else:
                    high = midpoint
            alpha = high
        self.fitted_alpha_, self.effective_df_ = alpha, degrees(alpha)
        self.model_ = Ridge(alpha=alpha).fit(X, y)
        self.coef_, self.intercept_ = self.model_.coef_, self.model_.intercept_
        self.n_features_in_ = X.shape[1]
        return self
    def predict(self, X):
        return self.model_.predict(X)


class StableFoldImputer(BaseEstimator, TransformerMixin):
    """Keep fold-empty columns so old sklearn never drops enum/boolean inputs."""
    def __init__(self, strategy="median", add_indicator=False):
        self.strategy = strategy
        self.add_indicator = add_indicator

    def fit(self, X, y=None):
        values = np.asarray(X, dtype=float)
        self.statistics_ = []
        for column in values.T:
            observed = column[np.isfinite(column)]
            if not len(observed):
                statistic = 0.
            elif self.strategy == "most_frequent":
                options, counts = np.unique(observed, return_counts=True)
                statistic = options[np.argmax(counts)]
            elif self.strategy == "mean":
                statistic = np.mean(observed)
            else:
                statistic = np.median(observed)
            self.statistics_.append(float(statistic))
        self.statistics_ = np.asarray(self.statistics_)
        self.indicator_columns_ = np.flatnonzero(np.any(~np.isfinite(values), axis=0))
        return self

    def transform(self, X):
        values = np.asarray(X, dtype=float)
        missing = ~np.isfinite(values)
        result = np.where(missing, self.statistics_, values)
        return np.column_stack([result, missing[:, self.indicator_columns_].astype(float)]) if self.add_indicator else result


class RawFeatureSubset(BaseEstimator, TransformerMixin):
    def __init__(self, indices):
        self.indices = indices
    def fit(self, X, y=None):
        return self
    def transform(self, X):
        return np.asarray(X, dtype=float)[:, list(self.indices)]


class FoldwiseProjection(BaseEstimator, TransformerMixin):
    """Unsupervised constant filtering and optional PCA, learned inside each fit."""
    def __init__(self, components=None):
        self.components = components
    def fit(self, X, y=None):
        values = np.asarray(X, dtype=float)
        self.kept_columns_ = np.flatnonzero(np.var(values, axis=0) > 1e-12)
        self.pca_ = None
        if not len(self.kept_columns_):
            self.output_dimensions_ = 1
        elif self.components is not None:
            count = min(int(self.components), len(self.kept_columns_), max(1, len(values) - 2))
            self.pca_ = PCA(n_components=count, svd_solver="full").fit(values[:, self.kept_columns_])
            self.output_dimensions_ = count
        else:
            self.output_dimensions_ = len(self.kept_columns_)
        return self
    def transform(self, X):
        values = np.asarray(X, dtype=float)
        if not len(self.kept_columns_):
            return np.zeros((len(values), 1))
        values = values[:, self.kept_columns_]
        return self.pca_.transform(values) if self.pca_ is not None else values


class MAPEConstantRegressor(BaseEstimator, RegressorMixin):
    """The positive weighted median minimizes constant absolute percentage error."""
    def fit(self, X, y):
        y = np.asarray(y, dtype=float)
        order = np.argsort(y)
        weights = 1. / y[order]
        index = np.searchsorted(np.cumsum(weights), .5 * np.sum(weights))
        self.constant_ = float(y[order[min(index, len(y) - 1)]])
        return self
    def predict(self, X):
        return np.full(len(X), self.constant_)


class EngineeringCalibratedRegressor(BaseEstimator, RegressorMixin):
    """At most two correction coefficients, shrunk toward the fixed price prior."""
    def __init__(self, core, strength=8.0, slope_limit=.5):
        self.core, self.strength, self.slope_limit = core, strength, slope_limit
    def fit(self, X, y):
        self.core_ = clone(self.core).fit(X, y)
        base = self.core_.predict(X)
        self.base_mean_ = float(np.mean(base))
        self.base_scale_ = max(float(np.std(base)), 1e-8)
        design = np.column_stack([np.ones(len(base)), (base - self.base_mean_) / self.base_scale_])
        self.correction_ = np.linalg.solve(design.T.dot(design) + float(self.strength) * np.eye(2),
                                           design.T.dot(np.asarray(y) - base))
        self.correction_[1] = np.clip(self.correction_[1], -float(self.slope_limit) * self.base_scale_, float(self.slope_limit) * self.base_scale_)
        return self
    def predict(self, X):
        base = self.core_.predict(X)
        correction = self.correction_[0] + self.correction_[1] * (base - self.base_mean_) / self.base_scale_
        return np.maximum(base + correction, 1e-8)


class AttributeInteractions(BaseEstimator, TransformerMixin):
    def __init__(self, pairs=()):
        self.pairs = pairs

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        values = np.asarray(X, dtype=float)
        return np.column_stack([values] + [values[:, a] * values[:, b] for a, b in self.pairs])


class SmallSampleKNN(BaseEstimator, RegressorMixin):
    def __init__(self, n_neighbors=3, weights="distance", p=2):
        self.n_neighbors = n_neighbors
        self.weights = weights
        self.p = p

    def fit(self, X, y):
        self.model_ = KNeighborsRegressor(n_neighbors=min(int(self.n_neighbors), len(X)), weights=self.weights, p=self.p).fit(X, y)
        return self

    def predict(self, X):
        return self.model_.predict(X)


class SmallSampleSelector(BaseEstimator, TransformerMixin):
    def __init__(self, k="all"):
        self.k = k

    def fit(self, X, y):
        X = np.asarray(X, dtype=float)
        self.n_features_in_ = X.shape[1]
        if self.k == "all":
            self.indices_ = np.arange(X.shape[1])
        else:
            count = min(int(self.k), X.shape[1], max(1, len(X) - 2))
            scores = np.nan_to_num(f_regression(X, y)[0], nan=0.0, posinf=1e100, neginf=0.0)
            self.indices_ = np.sort(np.argsort(-scores, kind="mergesort")[:count])
        return self

    def transform(self, X):
        return np.asarray(X, dtype=float)[:, self.indices_]


class EngineeringPriceRegressor(BaseEstimator, RegressorMixin):
    def __init__(self, feature_names, coefficients, intercept=0.0):
        self.feature_names = feature_names
        self.coefficients = coefficients
        self.intercept = intercept

    def fit(self, X, y=None):
        values = np.asarray(X, dtype=float)
        self.defaults_ = np.nanmedian(values, axis=0)
        self.defaults_ = np.nan_to_num(self.defaults_)
        self.n_features_in_ = values.shape[1]
        self.coef_ = np.asarray([self.coefficients.get(key, 0.0) for key in self.feature_names])
        return self

    def contributions(self, X):
        values = np.asarray(X, dtype=float)
        values = np.where(np.isfinite(values), values, self.defaults_)
        return values * self.coef_

    def predict(self, X):
        return np.maximum(float(self.intercept) + np.sum(self.contributions(X), axis=1), 1e-8)


class EngineeringResidualRegressor(BaseEstimator, RegressorMixin):
    def __init__(self, core, residual):
        self.core = core
        self.residual = residual

    def fit(self, X, y):
        # The engineering coefficients are fixed before data generation/selection.
        # They do not learn target labels; residual preprocessing stays in-fold.
        self.core_ = clone(self.core).fit(X, y)
        self.residual_ = clone(self.residual).fit(X, np.asarray(y) - self.core_.predict(X))
        self.n_features_in_ = np.asarray(X).shape[1]
        return self

    def predict(self, X):
        return np.maximum(self.core_.predict(X) + self.residual_.predict(X), 1e-8)
