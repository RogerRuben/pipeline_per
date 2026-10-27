# -*- coding: utf-8 -*-
"""Flushed, structured training events and progress during slow model fits."""
import contextlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return plain(value.tolist())
    if isinstance(value, np.generic):
        return plain(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def score_text(score):
    if not score:
        return '未评估'
    r2 = score.get('r2')
    return 'MAPE=%.10g%% R²=%s n=%s' % (
        score['mape_percent'], '不可定义' if r2 is None else '%.12g' % r2,
        score.get('rows', '?'))


class TrainingTrace(object):
    def __init__(self, directory, progress=None, heartbeat_seconds=10.):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.progress = progress or (lambda message: print(message, flush=True))
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.sequence = 0

    def emit(self, event, message=None, **details):
        with self.lock:
            self.sequence += 1
            record = plain(dict(event=event, sequence=self.sequence,
                                utc=datetime.utcnow().isoformat() + 'Z',
                                elapsed_seconds=time.monotonic() - self.started,
                                **details))
            with (self.directory / 'training_events.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                handle.flush()
            text = '[%s #%d] %s' % (event, self.sequence, message or json.dumps(plain(details), ensure_ascii=False))
            with (self.directory / 'training_verbose.log').open('a', encoding='utf-8') as handle:
                handle.write(text + '\n')
                handle.flush()
            self.progress(text)
        return record

    def message(self, text):
        self.emit('progress', str(text))

    @contextlib.contextmanager
    def span(self, stage, **details):
        start = time.monotonic()
        done = threading.Event()
        self.emit(stage + '_start', **details)

        def heartbeat():
            while not done.wait(self.heartbeat_seconds):
                self.emit('heartbeat', '%s仍在运行，耗时%.1f秒' % (stage, time.monotonic() - start),
                          stage=stage, seconds=time.monotonic() - start, **details)

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield
        except BaseException as exc:
            self.emit(stage + '_failed', error=repr(exc), seconds=time.monotonic() - start, **details)
            raise
        else:
            self.emit(stage + '_end', seconds=time.monotonic() - start, **details)
        finally:
            done.set()
            worker.join(timeout=1.)


def fit_scope(trace, stage, **details):
    return trace.span(stage, **details) if trace is not None else contextlib.nullcontext()
