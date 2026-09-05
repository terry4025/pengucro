"""Bounded live read-only benchmark; never creates a reservation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import threading
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.dpsnnn_engine import DPSNNN_BRANCHES, fetch_exact_dpsnnn_slots, create_dpsnnn_session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', default='1,2,4,6,8')
    parser.add_argument('--rate', type=float, default=8)
    parser.add_argument('--branch', choices=tuple(DPSNNN_BRANCHES), default='gangnam')
    parser.add_argument('--production', action='store_true')
    args = parser.parse_args()
    branch = DPSNNN_BRANCHES[args.branch]
    seed = requests.Session()
    seed.headers.update(create_dpsnnn_session().headers)
    seed.get(branch['base_url'] + branch['reserve_path'], timeout=10).raise_for_status()
    local = threading.local()
    rate_lock = threading.Lock()
    next_start = [0.0]
    def read(_):
        if not hasattr(local, 'session'):
            local.session = create_dpsnnn_session() if args.production else requests.Session()
            local.session.headers.update(seed.headers)
            local.session.cookies.update(seed.cookies)
        # Finite observation with an explicit request-start ceiling.
        with rate_lock:
            delay = next_start[0] - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_start[0] = time.monotonic() + 1 / args.rate
        start = time.perf_counter()
        diagnostics = []
        try:
            slots = fetch_exact_dpsnnn_slots(local.session, branch, next(iter(branch['themes'])), args.date,
                timeout=8, diagnostics=lambda *data: diagnostics.append(data))
            status = diagnostics[-1][2]
            error = ''
        except Exception as exc:
            slots = []
            status = diagnostics[-1][2] if diagnostics else None
            error = type(exc).__name__
        return dict(ms=round((time.perf_counter()-start)*1000, 2), status=status,
                    slots=len(slots), error=error)
    results = []
    for workers in map(int, args.workers.split(',')):
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            samples = list(pool.map(read, range(max(6, workers*2))))
        elapsed = time.perf_counter()-start
        times = sorted(x['ms'] for x in samples)
        row = dict(workers=workers, requests=len(samples), seconds=round(elapsed,3),
                   rps=round(len(samples)/elapsed,3), median_ms=statistics.median(times),
                   p95_ms=times[min(len(times)-1, int(len(times)*.95))], samples=samples)
        results.append(row)
        print(json.dumps({k:v for k,v in row.items() if k!='samples'}), flush=True)
        if any(x['error'] or x['status'] != 200 for x in samples):
            break
        if len(results)>1 and row['p95_ms']>results[0]['p95_ms']*2:
            break
        time.sleep(1)
    Path(args.output).write_text(json.dumps(dict(date=args.date, branch=args.branch,
        request_start_limit_per_second=args.rate, production=args.production, results=results), indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
