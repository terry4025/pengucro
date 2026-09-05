"""Windows spawn benchmark. Only a loopback mock server is contacted."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing as mp
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def percentile(values, fraction):
    data = sorted(values)
    return round(data[min(len(data)-1, int((len(data)-1)*fraction))], 3) if data else None


def run_client(index, root, url, start, stop, output, baseline, workers):
    os.environ['PENGUCRO_DATA_DIR'] = root
    import requests
    if baseline:
        source = subprocess.check_output(['git','show','1b19ca4:engines/dpsnnn_shared.py'],
                                         cwd=Path(__file__).resolve().parents[1]).decode('utf-8')
        namespace = {}
        exec(compile(source, '<v677-governor>', 'exec'), namespace)
        cls = namespace['SharedReadGovernor']
    else:
        from engines.dpsnnn_shared import SharedReadGovernor as cls
    try:
        governor = cls('local-benchmark')
    except Exception as exc:
        output.put({'process':index, 'initialization_error':type(exc).__name__})
        return
    accesses = [0]
    original_db = getattr(governor, '_db', None)
    @contextmanager
    def counted_db():
        accesses[0] += 1
        with original_db() as db:
            yield db
    if original_db is not None:
        governor._db = counted_db
    samples, errors, polls, lost = [], [], [], []
    submitted = False
    order_lock = threading.Lock()
    order_latency = []
    cpu_start = time.process_time()
    def request(session, method, path, priority=False):
        begin = time.perf_counter()
        governor.acquire(priority=priority, stop_event=stop)
        acquired = time.perf_counter()
        response = None
        try:
            response = session.request(method, url+path, timeout=3)
            received = time.perf_counter()
            samples.append((acquired-begin, received-acquired, received-begin))
            return response
        finally:
            governor.release(response, failed=response is None)
    def worker():
        nonlocal submitted
        session = requests.Session()
        session.trust_env = False
        start.wait()
        while not stop.is_set():
            try:
                response = request(session, 'GET', '/calendar?target='+str(index))
                now = time.perf_counter()
                polls.append(now)
                if response.json()['open'] and not submitted:
                    with order_lock:
                        if not submitted:
                            submitted = True
                            result = request(session, 'POST', '/order?target='+str(index), True)
                            assert result.json()['number']
                            order_latency.append(time.perf_counter()-now)
            except Exception as exc:
                if stop.is_set():
                    break
                errors.append(type(exc).__name__ + ': ' + str(exc))
                if not baseline and isinstance(exc, requests.RequestException):
                    stop.wait(.1)
                    continue
                lost.append(1)
                break
        session.close()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads: thread.start()
    while not stop.is_set(): time.sleep(.02)
    stop_started = time.perf_counter()
    alive_at_stop = sum(t.is_alive() for t in threads)
    for thread in threads: thread.join(5)
    if hasattr(governor, 'close'):
        governor.close(timeout=3)
    polls.sort()
    output.put(dict(process=index, requests=len(samples), errors=errors,
        alive_at_stop=alive_at_stop, lost=len(lost), alive_after=sum(t.is_alive() for t in threads),
        stop_seconds=time.perf_counter()-stop_started, db_accesses=accesses[0],
        cpu_seconds=time.process_time()-cpu_start, timings=samples,
        gaps=[b-a for a,b in zip(polls,polls[1:])], order_latency=order_latency))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--processes', type=int, default=4)
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--hold', type=float, default=0)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = Path(args.output).resolve()
    root = out.with_suffix('.data')
    root.mkdir(parents=True, exist_ok=False)
    orders = {}
    order_delays = {}
    activity = dict(active=0, peak=0, requests=0)
    activity_lock = threading.Lock()
    open_at = [float('inf')]
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                # Windows clients may reset idle keep-alive sockets on stop.
                pass
        def log_message(self, *args): pass
        def do_GET(self):
            with activity_lock:
                activity['active'] += 1
                activity['requests'] += 1
                activity['peak'] = max(activity['peak'], activity['active'])
            time.sleep(.03)
            body=json.dumps({'open':time.monotonic()>=open_at[0]}).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers()
            self.wfile.write(body)
            with activity_lock:
                activity['active'] -= 1
        def do_POST(self):
            key=self.path
            orders[key]=orders.get(key,0)+1
            order_delays[key] = round((time.monotonic()-open_at[0])*1000, 3)
            body=json.dumps({'number':'LOCAL-RECEIPT'}).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers()
            self.wfile.write(body)
    class Server(ThreadingHTTPServer):
        request_queue_size = 256
    server = Server(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    ctx = mp.get_context('spawn')
    start, stop, output=ctx.Event(),ctx.Event(),ctx.Queue()
    clients=[ctx.Process(target=run_client,args=(i,str(root),f'http://127.0.0.1:{server.server_port}',
        start,stop,output,args.baseline,args.workers)) for i in range(args.processes)]
    for client in clients: client.start()
    time.sleep(2)
    open_at[0]=time.monotonic()+args.seconds*.65
    start.set()
    def hold_lock():
        time.sleep(4)
        paths=list((root/'dpsnnn-budget').glob('*.sqlite3'))
        if not paths:
            directory = root/'dpsnnn-budget'
            directory.mkdir(exist_ok=True)
            retired = directory/'retired.sqlite3'
            with sqlite3.connect(retired) as db:
                db.execute('CREATE TABLE tickets (id TEXT)')
            paths = [retired]
        if paths:
            with sqlite3.connect(paths[0],timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                time.sleep(args.hold)
    if args.hold:
        threading.Thread(target=hold_lock,daemon=True).start()
    time.sleep(args.seconds)
    stop.set()
    results=[output.get(timeout=20) for _ in clients]
    for client in clients:
        client.join(5)
        if client.is_alive(): client.terminate(); client.join()
    server.shutdown()
    remaining = 0
    for path in (root/'dpsnnn-budget').glob('*.sqlite3'):
        with sqlite3.connect(path) as db:
            remaining+=db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
    timings=[sample for item in results for sample in item.get('timings',[])]
    summary=dict(baseline=args.baseline, processes=args.processes, workers_each=args.workers,
        seconds=args.seconds, injected_lock_seconds=args.hold, remaining_tickets=remaining,
        requests=sum(item.get('requests',0) for item in results),
        errors=sum(len(item.get('errors',[])) for item in results),
        workers_lost=sum(item.get('lost',0) for item in results),
        server_activity=activity, publication_to_order_ms=order_delays,
        max_stop_seconds=max(item.get('stop_seconds',0) for item in results),
        db_accesses=sum(item.get('db_accesses',0) for item in results),
        cpu_seconds=sum(item.get('cpu_seconds',0) for item in results), orders=orders,
        timings={name:{f'p{int(p*100)}':percentile([s[i]*1000 for s in timings],p)
                      for p in (.5,.95,.99,1)} for i,name in enumerate(('wait_ms','http_ms','total_ms'))},
        per_target=[dict(process=item['process'],requests=item.get('requests',0),
            max_gap_ms=round(max(item.get('gaps',[]) or [0])*1000,3),
            order_latency_ms=[round(v*1000,3) for v in item.get('order_latency',[])]) for item in results])
    out.write_text(json.dumps(dict(summary=summary,raw=results),indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__': main()
