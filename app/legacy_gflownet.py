# -*- coding: utf-8 -*-
"""Experimental tabular Trajectory-Balance GFlowNet candidate explorer.

This module intentionally has no PyTorch dependency.  It is a small, auditable
prototype for testing whether reward-proportional multi-modal generation improves
the project's deep-extrapolation candidate set before a neural GFlowNet and its
large offline runtime are added to the Win7 delivery.

A state consists of one historical seed and an unordered set of attribute edits.
The forward policy chooses a seed, adds a previously-unmodified attribute/value,
and finally stops.  The backward policy uniformly removes one edit.  Consequently
the same terminal design can be reached through different edit orders, which is
the central flow-network property missing from ordinary random or genetic search.
Every terminal is finalized by the existing coupling/constraint repair pipeline
before the real price and effectiveness services calculate its reward.
"""
from __future__ import print_function

import math
import random
import time
from .recommender import effective_price


def _softmax(values):
    if not values:
        return []
    maximum = max(values)
    raw = [math.exp(max(-40.0, min(40.0, value - maximum))) for value in values]
    total = sum(raw)
    return [value / total for value in raw]


def _choice(rng, items, probabilities):
    point = rng.random()
    cumulative = 0.0
    for item, probability in zip(items, probabilities):
        cumulative += probability
        if point <= cumulative:
            return item
    return items[-1]


class TabularTrajectoryBalanceGFlowNet(object):
    """A bounded experimental GFlowNet over mixed attribute edits."""

    def __init__(self, generator, evaluate_batch_callback, seed=None):
        self.generator = generator
        self.evaluate_batch_callback = evaluate_batch_callback
        self.rng = random.Random(seed)
        self.logits = {}
        self.log_z = 0.0

    @staticmethod
    def _value_token(value):
        if isinstance(value, float):
            return "%.12g" % value
        return str(value)

    def _state_key(self, seed_index, edits):
        if seed_index is None:
            return ("root",)
        return (
            "design", int(seed_index),
            tuple(sorted((str(key), self._value_token(value)) for key, value in edits.items())),
        )

    def _options(self, seeds, definitions, stds):
        result = {}
        for key, definition in definitions.items():
            if not definition.get("enabled", 1) or not definition.get("auto_adjustable", 1):
                continue
            values = []
            for seed in seeds:
                current = (seed.get("params") or {}).get(key)
                if current in (None, ""):
                    continue
                neighbors = self.generator._attribute_neighbors(
                    current, definition, stds.get(key, 0.0), 0.70,
                    include_bounds=True, exhaustive_discrete=True,
                )
                values.extend(neighbors)
            unique = []
            for value in values:
                if not any(self.generator._value_equal(value, old, definition) for old in unique):
                    unique.append(value)
            kind = self.generator._search_type(definition)
            if kind in ("continuous", "integer") and len(unique) > 8:
                numeric = sorted(unique, key=float)
                indexes = [0, 1, len(numeric) // 4, len(numeric) // 2, 3 * len(numeric) // 4, len(numeric) - 2, len(numeric) - 1]
                unique = [numeric[index] for index in indexes]
                unique = list(dict.fromkeys(unique))
            elif len(unique) > 10:
                unique = unique[:8] + unique[-2:]
            if unique:
                result[key] = unique
        return result

    def _actions(self, seed_index, edits, seeds, options, max_steps):
        if seed_index is None:
            return [("seed", index) for index in range(len(seeds))]
        if len(edits) >= max_steps:
            return [("stop",)]
        actions = [("stop",)] if edits else []
        for key in sorted(options):
            if key in edits:
                continue
            for value in options[key]:
                actions.append(("set", key, value))
        return actions or [("stop",)]

    def _probabilities(self, state_key, actions, temperature):
        state_logits = self.logits.setdefault(state_key, {})
        for action in actions:
            if action not in state_logits:
                state_logits[action] = 1.25 if action == ("stop",) else 0.0
        values = [float(state_logits[action]) / max(float(temperature), 0.25) for action in actions]
        return _softmax(values)

    def _sample_trajectory(self, seeds, options, max_steps, temperature):
        seed_index = None
        edits = {}
        decisions = []
        log_pf = 0.0
        log_pb = 0.0
        while True:
            state_key = self._state_key(seed_index, edits)
            actions = self._actions(seed_index, edits, seeds, options, max_steps)
            probabilities = self._probabilities(state_key, actions, temperature)
            action = _choice(self.rng, actions, probabilities)
            probability = max(probabilities[actions.index(action)], 1e-12)
            decisions.append((state_key, list(actions), list(probabilities), action))
            log_pf += math.log(probability)
            if action[0] == "seed":
                seed_index = int(action[1])
                continue
            if action[0] == "stop":
                break
            edits[str(action[1])] = action[2]
            # Uniform backward deletion among the k edits in the successor state.
            log_pb += -math.log(max(len(edits), 1))
        return {
            "seed_index": seed_index,
            "edits": edits,
            "decisions": decisions,
            "log_pf": log_pf,
            "log_pb": log_pb,
        }

    def _materialize(self, trajectory, seeds, definitions):
        base = seeds[trajectory["seed_index"]]
        params = dict(base.get("params") or {})
        params.update(trajectory["edits"])
        repairs, soft_moves = self.generator._finalize_params(
            params, base, {}, definitions, soft_strength=0.24,
        )
        signature = tuple((key, self._value_token(params[key])) for key in sorted(params))
        return signature, params, base, repairs, soft_moves

    @staticmethod
    def _log_reward(evaluation, target_price):
        price = effective_price(evaluation) or 1e9
        target = max(float(target_price), 1e-9)
        relative_gap = (price - target) / target
        feasibility = float(evaluation.get("feasibility_probability") or 0.0)
        capability = float(evaluation.get("conservative_capability_score", evaluation.get("capability_score")) or 0.0) / 100.0
        gate_failed = (evaluation.get("physical_gate") or {}).get("passed") is False
        hard_messages = [
            item for item in evaluation.get("rule_messages") or []
            if item.get("severity") == "error" and item.get("source") != "anomaly"
        ]
        # Above-target candidates retain a smooth reward gradient so the flow can
        # learn where the currently unreachable frontier lies.  Feasible designs
        # below target receive an additional but bounded reward advantage.
        log_reward = -80.0 * max(relative_gap, 0.0) + 4.0 * max(-relative_gap, 0.0)
        log_reward += 0.35 * feasibility + 0.25 * capability
        if gate_failed or hard_messages:
            log_reward -= 8.0
        return max(-30.0, min(8.0, log_reward))

    def _update(self, trajectories, learning_rate=0.025):
        losses = []
        for trajectory in trajectories:
            delta = self.log_z + trajectory["log_pf"] - trajectory["log_pb"] - trajectory["log_reward"]
            delta = max(-10.0, min(10.0, delta))
            losses.append(delta * delta)
            step = max(-0.5, min(0.5, 2.0 * learning_rate * delta))
            self.log_z -= step
            for state_key, actions, probabilities, selected in trajectory["decisions"]:
                state_logits = self.logits[state_key]
                for action, probability in zip(actions, probabilities):
                    gradient = (1.0 if action == selected else 0.0) - probability
                    state_logits[action] -= step * gradient
                    state_logits[action] = max(-12.0, min(12.0, state_logits[action]))
        return sum(losses) / max(len(losses), 1)

    def explore(self, request, seeds, definitions, max_evaluations=360, batch_size=36,
                max_steps=4, temperature=1.15):
        started = time.time()
        _numeric, _means, stds, _lower = self.generator._local_statistics(seeds, definitions)
        options = self._options(seeds, definitions, stds)
        target_price = float(request["max_price"])
        cache = {}
        training_batches = 0
        last_loss = None
        duplicate_trajectories = 0
        stagnant_batches = 0
        while len(cache) < int(max_evaluations) and stagnant_batches < 5:
            sampled = [
                self._sample_trajectory(seeds, options, max_steps, temperature)
                for _ in range(int(batch_size))
            ]
            pending = []
            pending_signatures = set()
            for trajectory in sampled:
                signature, params, base, repairs, soft_moves = self._materialize(
                    trajectory, seeds, definitions,
                )
                trajectory.update({
                    "signature": signature, "params": params, "base": base,
                    "repairs": repairs, "soft_moves": soft_moves,
                })
                if signature in cache or signature in pending_signatures:
                    duplicate_trajectories += 1
                    continue
                if len(cache) + len(pending) >= int(max_evaluations):
                    continue
                pending_signatures.add(signature)
                pending.append(trajectory)
            if pending:
                evaluations = self.evaluate_batch_callback([
                    {
                        "candidate_id": "GFN-%04d-%04d" % (training_batches, index),
                        "parameters": item["params"],
                        "base_parameters": item["base"].get("params"),
                        "target_protocol": request.get("target_protocol"),
                    }
                    for index, item in enumerate(pending)
                ])
                for trajectory, evaluation in zip(pending, evaluations):
                    log_reward = self._log_reward(evaluation, target_price)
                    cache[trajectory["signature"]] = {
                        "params": trajectory["params"], "base": trajectory["base"],
                        "evaluation": evaluation, "log_reward": log_reward,
                        "repairs": trajectory["repairs"], "soft_moves": trajectory["soft_moves"],
                    }
                stagnant_batches = 0
            else:
                stagnant_batches += 1
            trainable = []
            for trajectory in sampled:
                value = cache.get(trajectory.get("signature"))
                if value is None:
                    continue
                item = dict(trajectory)
                item["log_reward"] = value["log_reward"]
                trainable.append(item)
            if trainable:
                last_loss = self._update(trainable)
            training_batches += 1
        records = sorted(cache.values(), key=lambda item: (
            float(item["evaluation"].get("predicted_price_wan") or 1e99),
            -float(item["evaluation"].get("feasibility_probability") or 0.0),
        ))
        return {
            "records": records,
            "evaluated_count": len(cache),
            "training_batches": training_batches,
            "duplicate_trajectories": duplicate_trajectories,
            "final_tb_loss": last_loss,
            "state_count": len(self.logits),
            "option_count": sum(len(values) for values in options.values()),
            "elapsed_seconds": time.time() - started,
            "log_z": self.log_z,
            "method": "tabular_trajectory_balance_gflownet_experiment",
        }
