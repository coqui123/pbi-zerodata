"""Measure existing discovery and TOM calls without changing fixture behavior."""
import atexit
import functools
import json
import time


def measured(fn, label, totals, accept=lambda args: True):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        if not accept(args):
            return fn(*args, **kwargs)
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            row = totals.setdefault(label, {'calls': 0, 'seconds': 0.0})
            row['calls'] += 1
            row['seconds'] += time.perf_counter() - start
    return call


def instrument():
    from pbz import desktop, validate
    totals = {'discovery': {'calls': 0, 'seconds': 0.0}, 'tom': {'calls': 0, 'seconds': 0.0}}
    desktop.processes = measured(desktop.processes, 'discovery', totals)
    validate.engine_call = measured(validate.engine_call, 'tom', totals, lambda args: args and args[0] == 'tmdl-validate')
    atexit.register(lambda: print('PBZ_TIMING ' + json.dumps(totals), flush=True))
    return totals
