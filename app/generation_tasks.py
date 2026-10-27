# -*- coding: utf-8 -*-
from __future__ import print_function

import hashlib
import json
import threading
import time
import traceback
import uuid

from .generation_profiles import apply_generation_profile


class GenerationTaskInvalidated(RuntimeError):
    pass


class GenerationTaskManager(object):
    def __init__(self, application, ttl_seconds=3600):
        self.app = application
        self.ttl = ttl_seconds
        self.lock = threading.RLock()
        self.tasks = {}
        self.by_fingerprint = {}
        self.epoch = 0

    def fingerprint(self, request):
        # sort_by / page / page_size / source_mode are presentation-only and must
        # never trigger a new generation: they re-rank an existing batch.
        relevant = dict((key, request.get(key)) for key in (
            "session_id", "selected_tags", "max_price", "min_capability", "min_cost_effectiveness",
            "min_feasibility", "indicator_filter_mode", "indicator_filters", "count",
            "target_protocol", "scenario", "scenario_options", "optimization_intensity"
        ))
        # Frozen parameters change which seed values are locked during search, so
        # a different frozen set must never reuse a previously cached batch.
        relevant["frozen_parameters"] = sorted(set(request.get("frozen_parameters") or []))
        # User-tunable budget/rounds change the search itself, so they participate
        # in the fingerprint.
        relevant["generation_budget"] = request.get("generation_budget")
        relevant["generation_rounds"] = request.get("generation_rounds")
        relevant["exploration_profile"] = request.get("exploration_profile")
        relevant["effective_exploration_profile"] = request.get("effective_exploration_profile")
        relevant["time_budget_seconds"] = request.get("time_budget_seconds")
        relevant["beam_width"] = request.get("beam_width")
        relevant["seed_count"] = request.get("seed_count")
        relevant["product_code"] = self.app.runtime.schema["product_code"]
        semantic_fingerprint = getattr(self.app.store, "generation_semantics_fingerprint", None)
        relevant["master_data_semantics"] = (
            semantic_fingerprint() if semantic_fingerprint else self.app.store.master_data_version()
        )
        relevant["models"] = self.app.runtime.manifest().get("model_versions") or {
            "effectiveness": self.app.runtime.manifest()["effectiveness"]["model_version"],
            "price": self.app.runtime.manifest()["price"]["model_version"],
        }
        raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def canonicalize_generation_controls(self, request):
        """Return the single canonical representation used by generation and batch identity."""
        req = apply_generation_profile(request)
        req["count"] = max(1, min(int(req.get("count") or req.get("generation_count") or 5), 30))
        if req.get("generation_budget") not in (None, ""):
            req["generation_budget"] = max(
                1, min(int(req["generation_budget"]), self.app.generation_budget_limit())
            )
        else:
            req["generation_budget"] = None
        if req.get("generation_rounds") not in (None, ""):
            req["generation_rounds"] = max(
                1, min(int(req["generation_rounds"]), self.app.generation_rounds_limit())
            )
        else:
            req["generation_rounds"] = None
        req["time_budget_seconds"] = max(1, min(int(req.get("time_budget_seconds") or 35), 600))
        req["beam_width"] = max(2, min(int(req.get("beam_width") or 10), 40))
        req["seed_count"] = max(2, min(int(req.get("seed_count") or 12), 40))
        return req

    def start(self, request, force=False):
        self.cleanup()
        req = self.canonicalize_generation_controls(request)
        session_id = str(req.get("session_id") or "default")
        req["session_id"] = session_id
        fp = self.fingerprint(req)
        with self.lock:
            old_id = self.by_fingerprint.get(fp)
            if old_id and old_id in self.tasks and not force:
                task = self.tasks[old_id]
                if task["status"] == "completed":
                    result = task.get("result") or {}
                    if not result.get("empty_result"):
                        return self.public(task)
                    # An empty completed task is not a reusable success: the next
                    # identical request must actually search again.
                    self.tasks.pop(old_id, None)
                    if self.by_fingerprint.get(fp) == old_id:
                        self.by_fingerprint.pop(fp, None)
                elif task["status"] in ("queued", "running"):
                    return self.public(task)
                else:
                    # A failed cached task must not permanently block the same request.
                    self.tasks.pop(old_id, None)
                    if self.by_fingerprint.get(fp) == old_id:
                        self.by_fingerprint.pop(fp, None)
            task_id = "GENTASK-" + uuid.uuid4().hex[:12].upper()
            task = {
                "task_id": task_id,
                "fingerprint": fp,
                "session_id": session_id,
                "status": "queued",
                "progress": 0,
                "message": "已进入生成队列",
                "search_profile": dict(req.get("search_profile") or {}),
                "created_at": time.time(),
                "updated_at": time.time(),
                "request": req,
                "result": None,
                "error": None,
                "generation_epoch": self.epoch,
                "invalidated": False,
            }
            self.tasks[task_id] = task
            self.by_fingerprint[fp] = task_id
            thread = threading.Thread(target=self._worker, args=(task_id,), name=task_id)
            thread.daemon = True
            thread.start()
            return self.public(task)

    def _worker(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task.get("invalidated"):
                return
            profile = task.get("search_profile") or {}
            request = dict(task.get("request") or {})
            session_id = task.get("session_id")
            fingerprint = task.get("fingerprint")
            task_epoch = task.get("generation_epoch")
            task.update(
                status="running", progress=5 if profile.get("deep_exploration") else 8,
                message=profile.get("warning") or "正在选择相似历史方案", updated_at=time.time(),
            )
        try:
            def update(progress, message):
                with self.lock:
                    task = self.tasks.get(task_id)
                    if not task or task.get("invalidated") or task_epoch != self.epoch:
                        raise GenerationTaskInvalidated("生成所依赖的运行契约已变化")
                    task.update(progress=int(progress), message=str(message), updated_at=time.time())
            result = self.app._generate_sync(request, progress_callback=update)
            with self.lock:
                task = self.tasks.get(task_id)
                if not task or task.get("invalidated") or task_epoch != self.epoch:
                    raise GenerationTaskInvalidated("生成所依赖的运行契约已变化")
            empty_result = not (result.get("candidates") or [])
            result["empty_result"] = empty_result
            if empty_result:
                result["message"] = result.get("message") or "未找到任何满足条件的方案；此结果不会被缓存，再次生成会重新搜索。"
            with self.lock:
                task = self.tasks.get(task_id)
                if not task or task.get("invalidated") or task_epoch != self.epoch:
                    raise GenerationTaskInvalidated("生成所依赖的运行契约已变化")
                # Keep the epoch check, session write and public task commit in
                # one critical section. Runtime invalidation cannot interleave
                # and leave a mixed-contract batch behind.
                batch_id, prepared = self.app.sessions.add_batch(
                    session_id, result.get("candidates", []), fingerprint=fingerprint
                )
                result["batch_id"] = batch_id
                result["candidates"] = prepared
                task.update(status="completed", progress=100, message=result.get("message") or "生成完成", result=result, batch_id=batch_id, updated_at=time.time())
                if empty_result:
                    fp = task.get("fingerprint")
                    if self.by_fingerprint.get(fp) == task_id:
                        self.by_fingerprint.pop(fp, None)
        except GenerationTaskInvalidated:
            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    task.update(status="cancelled", progress=100,
                                message="运行配置已变化，本次生成已作废，请重新生成。",
                                invalidated=True, updated_at=time.time())
        except Exception as exc:
            with self.lock:
                task = self.tasks.get(task_id)
                if task and (task.get("invalidated") or task_epoch != self.epoch):
                    task.update(status="cancelled", progress=100,
                                message="运行配置已变化，本次生成已作废，请重新生成。",
                                invalidated=True, updated_at=time.time())
                elif task:
                    traceback.print_exc()
                    task.update(status="failed", progress=100, message="生成失败", error={"type":type(exc).__name__,"message":str(exc)}, updated_at=time.time())

    def get(self, task_id):
        with self.lock:
            task = self.tasks.get(str(task_id))
            return self.public(task) if task else None

    def public(self, task):
        if not task:
            return None
        payload = dict((key, task.get(key)) for key in ("task_id","status","progress","message","session_id","created_at","updated_at","error","fingerprint","batch_id","search_profile","invalidated"))
        if task.get("status") == "completed":
            result = task.get("result") or {}
            payload["result"] = dict((key,result.get(key)) for key in (
                "count", "requested_count", "evaluated_count", "all_records_count", "usable_count",
                "best_effort_candidate_count", "final_selected_count", "fallback_used",
                "strict_filter_satisfied", "strict_candidate_count", "best_effort_used", "rejection_statistics",
                "rejection_details", "generation_budget", "actual_budget_used", "max_rounds", "actual_rounds",
                "stopping_reason", "seed_agreements", "message", "batch_id", "search_profile", "search_warning",
                "search_iterations", "generation_method", "empty_result", "preflight",
                "explicit_filter_feasibility", "scenario", "scenario_policy", "applied_ranking"
            ))
            payload["candidates_count"] = len(result.get("candidates") or [])
        return payload

    def invalidate_all(self):
        with self.lock:
            self.epoch += 1
            for task_id, task in list(self.tasks.items()):
                if task.get("status") in ("queued", "running"):
                    task.update(status="cancelled", progress=100,
                                message="运行配置已变化，本次生成已作废，请重新生成。",
                                invalidated=True, updated_at=time.time())
                else:
                    self.tasks.pop(task_id, None)
            self.by_fingerprint.clear()

    def invalidate_session(self, session_id):
        session_id = str(session_id)
        with self.lock:
            targets = [key for key, value in self.tasks.items() if str(value.get("session_id")) == session_id]
            for key in targets:
                fp = self.tasks[key].get("fingerprint")
                if self.tasks[key].get("status") in ("queued", "running"):
                    self.tasks[key].update(status="cancelled", progress=100,
                                           message="当前生成会话已作废，请重新生成。",
                                           invalidated=True, updated_at=time.time())
                else:
                    self.tasks.pop(key, None)
                if self.by_fingerprint.get(fp) == key:
                    self.by_fingerprint.pop(fp, None)

    def cleanup(self):
        cutoff = time.time() - self.ttl
        with self.lock:
            stale = [key for key,value in self.tasks.items() if value.get("updated_at",0)<cutoff and value.get("status") in ("completed","failed","cancelled")]
            for key in stale:
                fp = self.tasks[key].get("fingerprint")
                self.tasks.pop(key,None)
                if self.by_fingerprint.get(fp)==key:self.by_fingerprint.pop(fp,None)
