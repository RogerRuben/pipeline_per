# -*- coding: utf-8 -*-
"""Experimental tabular Trajectory-Balance GFlowNet (standalone).

Positioning
-----------
This is **not** a replacement for the adaptive beam search.  Beam search
exploits: it greedily keeps the top lexicographically-ranked neighbours and
concentrates on the single best region.  This GFlowNet *supplements* it with
diversity: it learns ``P(x) ∝ R(x)`` so every rewarding region keeps
probability mass, and sampling returns several distinct high-reward modes in
one batch instead of collapsing onto one.

It is intentionally **not** wired into the recommendation workflow.  Use it as
an offline diversity source: run it beside the beam search and merge the two
candidate sets, or sample extra exploration candidates when the beam is stuck.
It learns to sample parameter edits whose terminal objects follow
``P(x) ∝ R(x)`` via the trajectory-balance objective
``(log Z + log P_F - log P_B - log R)^2``.

State
-----
``(seed_index, mask_tuple)`` where ``mask_tuple[j]`` is ``-1`` (attribute not
yet edited) or a value index.  The edit *set* is order-free: each attribute has
a fixed slot, so ``{a:=x, b:=y}`` and ``{b:=y, a:=x}`` are the same state.

Actions (from a non-terminal state)
-----------------------------------
* ``stop`` — terminate; the sampled object is the seed merged with every edit.
* ``set j := values[j][k]`` — set one currently-unset attribute.

Backward policy
---------------
For a state with ``k`` set attributes, the backward action is "which attribute
was set last", i.e. a softmax over its ``k`` parent states.  This matches the
order-free definition where the same set is reachable through several paths.

Only the standard library is used so the module can be loaded anywhere.
"""
from __future__ import print_function

import math
import random


def _log_softmax(logits):
    if not logits:
        return []
    mx = max(logits)
    shifted = [float(value) - mx for value in logits]
    denom = math.log(sum(math.exp(value) for value in shifted))
    return [value - denom for value in shifted]


def _softmax(logits):
    return [math.exp(value) for value in _log_softmax(logits)]


class TabularTBGFlowNet(object):
    def __init__(self, attributes, seeds, reward_fn, temperature=1.0, seed=None):
        """Build a tabular TB-GFlowNet.

        ``attributes``: list of ``{"id", "label", "values": [...]}``.
        ``seeds``: list of dicts mapping attribute id -> seed value.
        ``reward_fn``: ``callable(finalized_params_dict) -> float >= 0``.
        """
        self.attributes = list(attributes)
        self.seeds = [dict(item) for item in seeds]
        self.reward_fn = reward_fn
        self.temperature = float(temperature)
        self.rng = random.Random(seed)
        self.attr_ids = [attr["id"] for attr in self.attributes]
        self.n_attrs = len(self.attributes)
        self.logZ = 0.0
        # forward[(seed_idx, mask_tuple)] -> list of (action, logit)
        # action is ("stop",) or ("set", attr_idx, value_idx)
        self.forward = {}
        # backward[(seed_idx, mask_tuple)] -> list of (attr_idx, logit) over parents
        self.backward = {}
        self._init_policies()

    # ------------------------------------------------------------------ state
    def _actions(self, mask):
        actions = [("stop",)]
        for j, attr in enumerate(self.attributes):
            if mask[j] != -1:
                continue
            for k in range(len(attr["values"])):
                actions.append(("set", j, k))
        return actions

    def _parents(self, mask):
        return [j for j in range(self.n_attrs) if mask[j] != -1]

    def _ensure_state(self, seed_idx, mask):
        key = (seed_idx, tuple(mask))
        if key not in self.forward:
            self.forward[key] = [(a, 0.0) for a in self._actions(mask)]
            self.backward[key] = [(j, 0.0) for j in self._parents(mask)]
        return key

    def _init_policies(self):
        for seed_idx in range(len(self.seeds)):
            self._ensure_state(seed_idx, [-1] * self.n_attrs)

    # ------------------------------------------------------------------ policy
    def _forward_probs(self, key):
        entries = self.forward[key]
        actions = [a for a, _logit in entries]
        probs = _softmax([logit for _a, logit in entries])
        return actions, probs

    def _backward_probs(self, key):
        entries = self.backward[key]
        parents = [j for j, _logit in entries]
        probs = _softmax([logit for _j, logit in entries]) if entries else []
        return parents, probs

    def _finalize(self, seed_idx, mask):
        params = dict(self.seeds[seed_idx])
        for j, attr in enumerate(self.attributes):
            if mask[j] != -1:
                params[attr["id"]] = attr["values"][mask[j]]
        return params

    # -------------------------------------------------------------- sampling
    def sample_trajectory(self):
        """Sample one trajectory.

        Returns ``(finalized_params, reward, logP_F, logP_B, steps)`` where each
        step is a dict with the state, chosen action and forward/backward probs.
        """
        seed_idx = self.rng.randrange(len(self.seeds))
        mask = [-1] * self.n_attrs
        logP_F = 0.0
        logP_B = 0.0
        steps = []
        while True:
            key = self._ensure_state(seed_idx, mask)
            actions, probs = self._forward_probs(key)
            choice = self.rng.choices(range(len(actions)), weights=probs)[0]
            action = actions[choice]
            logP_F += math.log(max(probs[choice], 1e-300))
            step = {"key": key, "action_idx": choice, "action": action, "pf": probs[choice]}
            if action[0] == "stop":
                step["next_key"] = None
                step["parent_idx"] = None
                step["pb"] = None
                steps.append(step)
                break
            _kind, j, k = action
            mask[j] = k
            new_key = self._ensure_state(seed_idx, mask)
            parents, bprobs = self._backward_probs(new_key)
            pindex = parents.index(j)
            logP_B += math.log(max(bprobs[pindex], 1e-300))
            step["next_key"] = new_key
            step["parent_idx"] = pindex
            step["pb"] = bprobs[pindex]
            steps.append(step)
        finalized = self._finalize(seed_idx, mask)
        reward = max(float(self.reward_fn(finalized)), 1e-12)
        return finalized, reward, logP_F, logP_B, steps

    def sample(self, n=8):
        """Return ``n`` terminal objects ``(finalized_params, reward)``."""
        result = []
        for _ in range(int(n)):
            finalized, reward, _pf, _pb, _steps = self.sample_trajectory()
            result.append((finalized, reward))
        return result

    def sample_unique(self, n=8, max_attempts=40):
        """Return up to ``n`` *distinct* terminal objects.

        Diversity is the whole point of the GFlowNet supplement: deduplicating
        on the finalized parameter vector surfaces the several distinct
        rewarding modes that beam search would collapse into one.
        """
        result = []
        seen = set()
        for _ in range(int(max_attempts)):
            if len(result) >= int(n):
                break
            finalized, reward, _pf, _pb, _steps = self.sample_trajectory()
            signature = tuple(sorted((str(k), str(v)) for k, v in finalized.items()))
            if signature in seen:
                continue
            seen.add(signature)
            result.append((finalized, reward))
        return result

    # --------------------------------------------------------------- training
    def train(self, episodes, learning_rate=0.05, logZ_lr=None):
        """Minimize the trajectory-balance loss with manual SGD.

        Updates ``logZ``, the forward-policy logits and the backward-policy
        logits along every sampled trajectory.  Returns the per-episode squared
        losses.
        """
        logZ_lr = float(learning_rate if logZ_lr is None else logZ_lr)
        losses = []
        for _episode in range(1, int(episodes) + 1):
            finalized, reward, logP_F, logP_B, steps = self.sample_trajectory()
            delta = self.logZ + logP_F - logP_B - math.log(reward)
            losses.append(delta * delta)
            grad = 2.0 * delta

            # log Z
            self.logZ -= logZ_lr * grad

            # forward logits along the trajectory
            for step in steps:
                key = step["key"]
                action_idx = step["action_idx"]
                pf = step["pf"]
                entries = self.forward[key]
                for i, (action, logit) in enumerate(entries):
                    indicator = 1.0 if i == action_idx else 0.0
                    d_logp = indicator - pf  # d log P_F(a_i | s) / d logit_i
                    entries[i] = (action, logit - learning_rate * grad * d_logp)

            # backward logits along the trajectory
            for step in steps:
                if step["next_key"] is None:
                    continue
                nkey = step["next_key"]
                parent_idx = step["parent_idx"]
                pb = step["pb"]
                entries = self.backward[nkey]
                for i, (parent, logit) in enumerate(entries):
                    indicator = 1.0 if i == parent_idx else 0.0
                    d_logp = indicator - pb  # d log P_B(s | s') / d logit_i
                    # logP_B appears with a minus sign in the loss.
                    entries[i] = (parent, logit + learning_rate * grad * d_logp)

        return losses


# --------------------------------------------------------------------------- #
# Offline merge helpers (not part of the online recommendation flow)
# --------------------------------------------------------------------------- #
def _params_of(item):
    """Extract a plain params dict from several accepted item shapes."""
    if isinstance(item, dict) and isinstance(item.get("params"), dict):
        return item["params"]
    if isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], dict):
        return item[0]
    if isinstance(item, dict):
        return item
    return {}


def dedupe_candidates(items):
    """Dedupe a list of candidates by their finalized parameter vector.

    Accepts recommendation items (dicts carrying ``params``), ``(params,
    reward)`` tuples produced by :meth:`TabularTBGFlowNet.sample`, or plain
    params dicts.  Preserves first-seen order.
    """
    seen = set()
    out = []
    for item in items:
        params = _params_of(item)
        signature = tuple(sorted((str(k), str(v)) for k, v in params.items()))
        if signature in seen:
            continue
        seen.add(signature)
        out.append(item)
    return out


def merge_beam_gflownet(beam_candidates, gflownet_samples):
    """Offline example: merge beam-search results with GFlowNet samples, deduped.

    Beam search exploits and may collapse onto one region; the GFlowNet
    supplement contributes distinct high-reward modes.  This helper is a
    *documentation-grade* offline example — callers wire it into their own
    offline experiments, not into the production ``HistorySeededGenerator``.
    """
    merged = list(beam_candidates or []) + list(gflownet_samples or [])
    return dedupe_candidates(merged)

# Stable import for earlier exported experiments; V21 uses TabularTBGFlowNet.
from .legacy_gflownet import TabularTrajectoryBalanceGFlowNet
