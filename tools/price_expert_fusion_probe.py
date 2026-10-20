# -*- coding: utf-8 -*-
"""Development-OOF experiment for expert priors and explicit error budgets.

The numerical routine is shared by the V2 trainer; the command-line demonstration
never installs a model. Supply predictions made without training on their own
rows; outer validation must assess the complete selection procedure.
"""
from __future__ import print_function

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import scipy
from scipy.optimize import minimize


class FusionConstraintError(ValueError):
    """A directly checkable input or hard-constraint conflict."""


class FusionOptimizationError(RuntimeError):
    """The numerical optimizer did not find an acceptable feasible solution."""


def _vector(value, size, default, name):
    result = np.full(size, default, dtype=float) if value is None else np.asarray(value, dtype=float)
    if result.shape != (size,) or not np.all(np.isfinite(result)):
        raise ValueError("Invalid " + name)
    return result


def _simplex_start(desired, lower, upper):
    """Project onto a feasible bounded simplex without random initialization."""
    if np.any(lower > upper) or np.sum(lower) > 1 + 1e-10 or np.sum(upper) < 1 - 1e-10:
        raise FusionConstraintError("Weight bounds cannot sum to one")
    left, right = -2.0, 2.0
    for _ in range(90):
        midpoint = (left + right) / 2.0
        if np.sum(np.clip(desired - midpoint, lower, upper)) > 1:
            left = midpoint
        else:
            right = midpoint
    return np.clip(desired - (left + right) / 2.0, lower, upper)


def _metrics(y, prediction):
    return {"mape_percent": float(np.mean(np.abs((prediction - y) / y)) * 100),
            "r2": float(1 - np.sum((prediction - y) ** 2) / np.sum((y - np.mean(y)) ** 2))}


def _constant_mape_baseline(y):
    ordered = np.argsort(y)
    cumulative = np.cumsum(1.0 / y[ordered])
    constant = y[ordered[np.searchsorted(cumulative, cumulative[-1] / 2)]]
    return _metrics(y, np.full(len(y), constant))


def fit_expert_weights(y, oof_predictions, expert_prior=None, confidence=None,
                       mode="performance_budget", prior_strength=.01, alpha=.7,
                       lower_bounds=None, upper_bounds=None,
                       max_mape_delta_pp=.1, max_r2_drop=.005, expert_groups=None):
    """Learn constant nonnegative weights; budgets apply only to supplied OOF rows.

    Prior entries are fractions of the full ensemble, not percentages. None means
    unspecified; a soft zero remains eligible. Hard bans use upper_bounds=0.
    A data-only reference ignores expert bounds, but honors invalid-model bans.
    The exact absolute-error epigraph avoids differentiating MAPE at zero.
    """
    y = np.asarray(y, dtype=float)
    bank = np.asarray(oof_predictions, dtype=float)
    if y.ndim != 1 or len(y) < 3 or bank.ndim != 2 or bank.shape[0] != len(y) or bank.shape[1] < 1:
        raise ValueError("Expected y[n] and OOF predictions[n, models]")
    if not np.all(np.isfinite(y)) or np.any(y <= 0) or np.var(y) <= 1e-14:
        raise ValueError("MAPE needs positive prices; R2 needs nonconstant prices")
    if mode not in ("data_only", "soft_prior", "performance_budget"):
        raise ValueError("Unknown fusion mode")
    if not np.isfinite(alpha) or not 0 <= alpha <= 1 or not np.isfinite(prior_strength) or prior_strength < 0:
        raise ValueError("Invalid objective coefficients")
    size, count = bank.shape[1], len(y)
    invalid = np.any(~np.isfinite(bank) | (bank <= 0), axis=0)
    if np.all(invalid):
        raise FusionConstraintError("No valid price candidates")
    bank = bank.copy()
    bank[:, invalid] = 0.0  # A zero weight cannot neutralize NaN in matrix multiplication.
    lower = _vector(lower_bounds, size, 0.0, "lower bounds")
    upper = _vector(upper_bounds, size, 1.0, "upper bounds")
    if np.any(lower < 0) or np.any(upper > 1):
        raise ValueError("Weights must stay in [0, 1]")
    if np.any(invalid & (lower > 0)):
        raise FusionConstraintError("A mandatory member has invalid price predictions")
    upper[invalid] = 0.0
    _simplex_start(np.full(size, 1.0 / size), lower, upper)
    entries = [None] * size if expert_prior is None else list(expert_prior)
    if len(entries) != size:
        raise ValueError("Expert prior length differs from model count")
    known = np.asarray([value is not None for value in entries])
    prior = np.asarray([0.0 if value is None else value for value in entries], dtype=float)
    if not np.all(np.isfinite(prior)) or np.any(prior < 0) or np.any(prior > 1) or np.sum(prior) > 1 + 1e-8:
        raise ValueError("Expert priors must be consistent fractions")
    if np.all(known) and abs(np.sum(prior) - 1) > 1e-8:
        raise ValueError("A complete expert prior must sum to one")
    trust = _vector(confidence, size, 1.0, "expert confidence")
    if np.any(trust < 0) or np.any(trust > 1):
        raise ValueError("Expert confidence must stay in [0, 1]")
    trust *= known
    group_records, group_vectors = [], []
    for record in expert_groups or []:
        indices = list(record.get("members", []))
        if len(set(indices)) != len(indices) or any(not isinstance(i, int) or not 0 <= i < size for i in indices):
            raise ValueError("Invalid expert group members")
        vector = np.zeros(size)
        vector[indices] = 1.0
        item = dict(record, lower_bound=float(record.get("lower_bound", 0)), upper_bound=float(record.get("upper_bound", 1)),
                    confidence=float(record.get("confidence", 1)))
        if not 0 <= item["confidence"] <= 1 or not 0 <= item["lower_bound"] <= item["upper_bound"] <= 1:
            raise ValueError("Invalid expert group bounds/confidence")
        if item.get("weight") is not None and (not np.isfinite(item["weight"]) or not 0 <= item["weight"] <= 1):
            raise ValueError("Invalid expert group prior")
        if vector.dot(upper) < item["lower_bound"] - 1e-10 or vector.dot(lower) > item["upper_bound"] + 1e-10:
            raise FusionConstraintError("Expert group bounds conflict with available members")
        group_records.append(item)
        group_vectors.append(vector)
    specified_groups = [i for i, record in enumerate(group_records) if record.get("weight") is not None]
    prior_matrix = np.vstack([np.eye(size)[known]] + [group_vectors[i][None, :] for i in specified_groups])
    prior_targets = np.concatenate([prior[known], [group_records[i]["weight"] for i in specified_groups]])
    prior_confidence = np.concatenate([trust[known], [group_records[i]["confidence"] for i in specified_groups]])
    prior_count = max(len(prior_targets), 1)
    if mode != "data_only" and not np.any(prior_confidence):
        raise ValueError("Expert mode needs at least one specified, trusted prior")
    if mode == "performance_budget" and (not np.all(np.isfinite([max_mape_delta_pp, max_r2_drop])) or
                                         max_mape_delta_pp < 0 or max_r2_drop < 0):
        raise ValueError("Error budgets must be finite and nonnegative")
    relative_bank = bank / y[:, None]
    scale = np.sqrt(np.sum((y - np.mean(y)) ** 2))
    scaled_bank, scaled_y = bank / scale, y / scale
    zeros = np.zeros(count)
    identity = np.eye(count)
    equality_gradient = np.concatenate([np.ones(size), zeros])

    def data_loss(weights):
        metrics = _metrics(y, bank.dot(weights))
        return alpha * metrics["mape_percent"] / 100 + (1 - alpha) * (1 - metrics["r2"])

    def prior_loss(weights):
        return float(np.sum(prior_confidence * (prior_matrix.dot(weights) - prior_targets) ** 2) / prior_count)

    def solve(solve_mode, lb, ub, start, limits=None, honor_groups=True):
        weights = _simplex_start(start, lb, ub)
        initial = np.concatenate([weights, np.abs(relative_bank.dot(weights) - 1)])
        constraints = [
            {"type": "eq", "fun": lambda z: np.sum(z[:size]) - 1,
             "jac": lambda z: equality_gradient},
            {"type": "ineq", "fun": lambda z: z[size:] - relative_bank.dot(z[:size]) + 1,
             "jac": lambda z: np.column_stack([-relative_bank, identity])},
            {"type": "ineq", "fun": lambda z: z[size:] + relative_bank.dot(z[:size]) - 1,
             "jac": lambda z: np.column_stack([relative_bank, identity])}]
        if honor_groups:
            for record, vector in zip(group_records, group_vectors):
                constraints.extend([
                    {"type": "ineq", "fun": lambda z, v=vector, lo=record["lower_bound"]: v.dot(z[:size]) - lo,
                     "jac": lambda z, v=vector: np.concatenate([v, zeros])},
                    {"type": "ineq", "fun": lambda z, v=vector, hi=record["upper_bound"]: hi - v.dot(z[:size]),
                     "jac": lambda z, v=vector: np.concatenate([-v, zeros])}])
        if limits is not None:
            constraints.extend([
                {"type": "ineq", "fun": lambda z: limits["mape_percent"] / 100 - np.mean(z[size:]),
                 "jac": lambda z: np.concatenate([np.zeros(size), -np.ones(count) / count])},
                {"type": "ineq", "fun": lambda z: 1 - limits["r2"] - np.sum((scaled_bank.dot(z[:size]) - scaled_y) ** 2),
                 "jac": lambda z: np.concatenate([-2 * scaled_bank.T.dot(scaled_bank.dot(z[:size]) - scaled_y), zeros])}])

        def objective(z):
            base = alpha * np.mean(z[size:]) + (1 - alpha) * np.sum((scaled_bank.dot(z[:size]) - scaled_y) ** 2)
            if solve_mode == "performance_budget":
                return prior_loss(z[:size]) + 1e-10 * base
            return base + (prior_strength * prior_loss(z[:size]) if solve_mode == "soft_prior" else 0)

        def gradient(z):
            grad = np.concatenate([2 * (1 - alpha) * scaled_bank.T.dot(scaled_bank.dot(z[:size]) - scaled_y),
                                   np.full(count, alpha / count)])
            penalty_gradient = 2 * prior_matrix.T.dot(prior_confidence * (prior_matrix.dot(z[:size]) - prior_targets)) / prior_count
            if solve_mode == "performance_budget":
                grad *= 1e-10
                grad[:size] += penalty_gradient
            elif solve_mode == "soft_prior":
                grad[:size] += prior_strength * penalty_gradient
            return grad

        result = minimize(objective, initial, jac=gradient, method="SLSQP",
                          bounds=list(zip(lb, ub)) + [(0.0, None)] * count,
                          constraints=constraints, options={"maxiter": 500, "ftol": 1e-10})
        candidate = result.x[:size]
        feasible = (np.all(np.isfinite(candidate)) and abs(np.sum(candidate) - 1) <= 1e-7 and
                    np.all(candidate >= lb - 1e-7) and np.all(candidate <= ub + 1e-7))
        candidate_metrics = _metrics(y, bank.dot(candidate))
        if honor_groups:
            feasible = feasible and all(record["lower_bound"] - 1e-7 <= vector.dot(candidate) <= record["upper_bound"] + 1e-7
                                        for record, vector in zip(group_records, group_vectors))
        if limits is not None:
            feasible = feasible and candidate_metrics["mape_percent"] <= limits["mape_percent"] + 1e-6
            feasible = feasible and candidate_metrics["r2"] >= limits["r2"] - 1e-8
        if not result.success or not feasible:
            raise FusionOptimizationError("No accepted feasible solution: " + str(result.message))
        return candidate

    reference_upper = (~invalid).astype(float)
    best = min(np.flatnonzero(~invalid), key=lambda index: data_loss(np.eye(size)[index]))
    reference = solve("data_only", np.zeros(size), reference_upper, np.eye(size)[best], honor_groups=False)
    reference_metrics = _metrics(y, bank.dot(reference))
    limits = None
    if mode == "performance_budget":
        limits = {"mape_percent": reference_metrics["mape_percent"] + max_mape_delta_pp,
                  "r2": reference_metrics["r2"] - max_r2_drop}
        if np.all(np.abs(lower - upper) < 1e-12):
            fixed_metrics = _metrics(y, bank.dot(lower))
            if fixed_metrics["mape_percent"] > limits["mape_percent"] + 1e-6 or fixed_metrics["r2"] < limits["r2"] - 1e-8:
                raise FusionConstraintError("Fixed expert weights violate the development error budget")
    fitted = solve(mode, lower, upper, reference, limits)
    fitted_metrics = _metrics(y, bank.dot(fitted))
    baseline = _constant_mape_baseline(y)
    return {"mode": mode, "weights": fitted.tolist(), "expert_prior": entries,
            "expert_confidence": trust.tolist(), "invalid_model_indices": np.flatnonzero(invalid).tolist(),
            "expert_groups": [dict(record, learned_weight=float(vector.dot(fitted)),
                                   deviation=float(vector.dot(fitted) - record["weight"]) if record.get("weight") is not None else None)
                              for record, vector in zip(group_records, group_vectors)],
            "expert_weight_deltas": [float(fitted[i] - prior[i]) if known[i] else None for i in range(size)],
            "development_metrics": fitted_metrics, "data_reference_weights": reference.tolist(),
            "data_reference_metrics": reference_metrics, "error_budget_limits": limits,
            "mape_delta_percentage_points": fitted_metrics["mape_percent"] - reference_metrics["mape_percent"],
            "r2_drop": reference_metrics["r2"] - fitted_metrics["r2"],
            "constant_mape_baseline": baseline,
            "beats_constant_mape_baseline": fitted_metrics["mape_percent"] < baseline["mape_percent"],
            "interpretation": "Budgets constrain supplied development OOF rows, not future accuracy or outer validation."}


def demonstration():
    y = np.linspace(2.0, 10.0, 24)
    bank = np.column_stack([y, y * 1.01, y * 1.5])
    near = np.column_stack([y * .99, y * 1.01])
    biased = np.column_stack([np.full(len(y), .5), np.full(len(y), .8)])
    report = {"implementation_status": "isolated_probe_not_connected_to_training",
              "data_kind": "constructed_numeric_examples_not_business_accuracy",
              "runtime": {"python": sys.version.split()[0], "numpy": np.__version__, "scipy": scipy.__version__},
              "bad_model_prior_80_percent": fit_expert_weights(y, bank, [.1, .1, .8], max_mape_delta_pp=.2),
              "near_equal_models_respect_prior": fit_expert_weights(y, near, [.8, .2], max_mape_delta_pp=1.0),
              "all_models_share_bias": fit_expert_weights(y, biased, mode="data_only")}
    try:
        fit_expert_weights(y, bank, [.1, .1, .8], lower_bounds=[.1, .1, .8],
                           upper_bounds=[.1, .1, .8], max_mape_delta_pp=.2)
    except FusionConstraintError as exc:
        report["hard_prior_conflict"] = str(exc)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = demonstration()
    if args.report is not None:
        root = Path(__file__).resolve().parents[1]
        output = args.report.resolve()
        if not any(directory.resolve() in output.parents for directory in (root / "outputs", root / "logs")):
            parser.error("Report must be inside this project's outputs or logs directory")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
