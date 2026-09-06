"""Final-engine and actual monitor JS regressions; no live CGV requests."""
from itertools import permutations
from types import SimpleNamespace

import pytest

from engines.cgv_client import CgvSeatGroup, parse_api_seats
from engines.cgv_engine import CgvEngine as Base
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_MOV_NO
from engines.cgv_preopen_matching import matching_schedule_candidates
from test_cgv_opening_regressions import schedule, seats
from test_cgv_preopen_v681 import preopen, choose, node_run
from test_cgv_recovery_v682 import registered, setup_monitor, monitor_scripts


def configure(e, a, b):
    e._priority_preopen_monitor = True
    e._priority_movie = '오디세이'
    e._priority_auditorium = 'IMAX관'
    e._priority_format = 'IMAX LASER 2D'
    e._priority_preferred_times = ['11:00', '14:30']
    e._priority_manual_groups = (CgvSeatGroup(('H21', 'H22')),)
    e._priority_schedule_payload = {'data': [a, b]}
    e._refresh_priority_schedule_payload = lambda _: None


@pytest.mark.parametrize('reverse', [False, True])
def test_partial_requested_time_survives_other_published_movie_id(reverse):
    a, b = dict(schedule('1100', '2'), movNo=''), schedule('1430', '3')
    rows = [b, a] if reverse else [a, b]
    e = registered()
    configure(e, a, b)
    e._priority_schedule_payload = {'data': rows}
    token = _PREOPEN_MOV_NO.set('test-movie')
    try:
        with preopen():
            assert choose(*rows) == dict(a, movNo='test-movie')
            assert [s['scnsrtTm'] for s in e._ordered_schedule_candidates(a)] == ['1100', '1430']
        assert a['movNo'] == ''
    finally:
        _PREOPEN_MOV_NO.reset(token)


def test_duplicate_identity_retains_one_candidate_per_screening():
    a, b = schedule('1100', '2'), dict(schedule('1430', '3'), movNo='')
    for rows in permutations([dict(a, movNo=''), a, b]):
        result = matching_schedule_candidates({'data': list(rows)}, movie='오디세이', mov_no='test-movie')
        assert len(result) == 2
        assert {s['scnSseq'] for s in result} == {'2', '3'}


@pytest.mark.parametrize('change', [dict(movNo='wrong-movie'), dict(cntlYn='Y'),
    dict(movNm='오디세이: 감독판', expoProdNm='오디세이: 감독판'),
    dict(expoScnsNm='2관', movkndDsplEnm='2D'), dict(scnSseq='')])
def test_mixed_publication_does_not_relax_candidate_validation(change):
    a = dict(schedule('1100', '2'), movNo='')
    a.update(change)
    token = _PREOPEN_MOV_NO.set('test-movie')
    try:
        with preopen():
            assert choose(a, schedule('1430', '3')) is None
    finally:
        _PREOPEN_MOV_NO.reset(token)


@pytest.mark.parametrize('change,duplicate', [({'seatStusCd': '00 ', 'seatSaleYn': ' y '}, False),
    ({'seatSaleYn': ''}, False), ({'seatSaleYn': None}, False),
    ({'seatLocNo': ''}, False), ({'seatNo': '21x'}, False),
    ({'seatNo': ' 021 '}, False), ({'seatStusCd': '01'}, True)])
def test_actual_monitor_and_python_agree_on_seat_availability(change, duplicate):
    script, arg = monitor_scripts(False)[0]
    row = arg['initialPayload']['data']['items'][0]['seats'][0]
    row.update(change)
    if duplicate:
        arg['initialPayload']['data']['items'][0]['seats'].append(
            seats('H21')['data']['items'][0]['seats'][0])
    available = {s.label for s in parse_api_seats(arg['initialPayload']) if s.available}
    expected = {'H21', 'H22'} <= available
    result = node_run({'script': script, 'arg': arg}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setInterval=()=>1;global.clearInterval=()=>{};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
const requests=[];
global.fetch=async(url,opts)=>{requests.push(url);return {ok:true,status:200,headers:new Headers(),
json:async()=>({statusCode:0,data:{resultCode:0,movAtktNo:'owned'}})}};
(async()=>{eval('('+spec.script+')')(spec.arg);for(let n=0;n<40;n++)await Promise.resolve();
console.log(JSON.stringify({hit:!!window.__pengucroFastSeatMonitor.hit,requests}));})();
''')
    assert result == {'hit': expected, 'requests': ['price', 'hold'] if expected else []}


@pytest.mark.parametrize('mode,limit', [('fast', 3), ('strict', 8)])
@pytest.mark.parametrize('claiming', [False, True])
def test_final_engine_bounds_idle_or_price_wait_and_reaches_next_time(monkeypatch, mode, limit, claiming):
    e = registered()
    setup_monitor(e)
    a, b = schedule('1100', '2'), schedule('1430', '3')
    configure(e, a, b)
    e._priority_rotation_mode = mode
    payload = seats('H21', 'H22')
    e._read_schedule_once = lambda *a, **k: (e._priority_manual_groups[0], payload, 200)
    now = [100.0]
    class Stop:
        stopped = False
        def set(self): self.stopped = True
        def is_set(self): return self.stopped or now[0] >= 120
        def wait(self, delay): now[0] += delay; return self.is_set()
    e.stop_event = Stop()
    monkeypatch.setattr('engines.cgv_engine.time.monotonic', lambda: now[0])
    monkeypatch.setattr(Base, '_read_fast_seat_monitor', staticmethod(lambda _: {
        'attemptId': 'current', 'running': True, 'claiming': claiming, 'holdSent': False,
        'inflight': int(claiming), 'completed': int((now[0]-100)/.35)}))
    starts, decisions = [], []
    def start(*args, **kwargs):
        seq = kwargs['direct_hold']['schedule']['scnSseq']
        starts.append((seq, now[0]-100))
        if seq == '3': e.stop_event.set()
        return True
    e._start_fast_seat_monitor = start
    def evaluate(script, arg):
        decisions.append(arg)
        return {'terminalError': arg['reason'], 'holdSent': False}
    with preopen():
        assert e._watch_and_hold_api(SimpleNamespace(evaluate=evaluate), a, e._priority_manual_groups, 2, False, {}) == (False, False)
    assert [seq for seq, _ in starts] == ['2', '3']
    assert limit <= starts[1][1] <= limit + .1
    assert decisions == [{'id': 'current', 'onlyBeforeHold': True, 'reason': 'candidate-timeout'}]
    assert e._priority_claim_deadline is None


@pytest.mark.parametrize('changed', ['removed', 'controlled', 'time', 'movie'])
def test_final_engine_revalidates_after_pending_seat_read(monkeypatch, changed):
    e = registered()
    setup_monitor(e)
    a, b = schedule('1100', '2'), schedule('1430', '3')
    configure(e, a, b)
    e.PRIORITY_SEAT_REQUEST_INTERVAL_SECONDS = 0
    reads, starts = [], []
    def read(*args, **kwargs):
        reads.append(1)
        modified = {'controlled': dict(a, cntlYn='Y'), 'time': dict(a, scnsrtTm='1200'),
                    'movie': dict(a, movNo='other', movNm='다른 영화', expoProdNm='다른 영화')}
        e._priority_schedule_payload = {'data': [b] if changed == 'removed' else [modified[changed], b]}
        return {'ok': True, 'status': 200, 'data': seats('H21', 'H22')}
    monkeypatch.setattr('engines.cgv_engine_priority_ladder.run_schedule_wave', read)
    def start(*args, **kwargs):
        starts.append(kwargs['direct_hold']['schedule']['scnSseq'])
        e.stop_event.set()
        return True
    e._start_fast_seat_monitor = start
    with preopen():
        assert e._watch_and_hold_api(object(), a, e._priority_manual_groups, 2, False, {}) == (False, False)
    assert starts == ['3'] and len(reads) == 2


@pytest.mark.parametrize('reason', ['candidate-timeout', 'candidate-invalidated'])
@pytest.mark.parametrize('phase', ['price', 'hold'])
def test_actual_atomic_rotation_never_cancels_sent_hold_or_dispatches_after_stop(reason, phase):
    scripts = monitor_scripts(True)
    scripts[1][1]['reason'] = reason
    result = node_run({'scripts': scripts, 'phase': phase}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setTimeout=()=>1;global.clearTimeout=()=>{};global.setInterval=()=>1;global.clearInterval=()=>{};
let finish,signal;const requests=[];
const response=data=>({ok:true,status:200,headers:new Headers(),json:async()=>data});
global.fetch=(url,opts)=>{requests.push(url);if(url===spec.phase){signal=opts.signal;return new Promise(resolve=>{finish=resolve})};
return Promise.resolve(response({statusCode:0}));};
(async()=>{eval('('+spec.scripts[0][0]+')')(spec.scripts[0][1]);
for(let i=0;i<30;i++)await Promise.resolve();
const decision=eval('('+spec.scripts[1][0]+')')(spec.scripts[1][1]);
finish(response({statusCode:0,data:{resultCode:0,movAtktNo:'owned'}}));
for(let i=0;i<30;i++)await Promise.resolve();
console.log(JSON.stringify({decision,aborted:signal.aborted,requests,hit:!!window.__pengucroFastSeatMonitor.hit}));})();
''')
    if phase == 'price':
        assert result['decision']['terminalError'] == reason
        assert result['aborted'] and result['requests'] == ['price'] and not result['hit']
    else:
        assert result == {'decision': {}, 'aborted': False, 'requests': ['price', 'hold'], 'hit': True}


def test_housekeeping_invalidates_active_candidate_before_deadline():
    e = registered()
    a, b = schedule('1100', '2'), schedule('1430', '3')
    configure(e, a, b)
    e._priority_active_schedule = a
    e._priority_claim_returns_on_conflict = True
    e._priority_claim_deadline = float('inf')
    e._priority_schedule_payload = {'data': [b]}
    calls = []
    e._interrupt_fast_monitor = lambda p, **kw: calls.append(kw) or {'terminalError': kw['reason']}
    with preopen():
        assert e._monitor_housekeeping(object()) == {'terminalError': 'candidate-invalidated'}
    assert calls == [{'only_before_hold': True, 'reason': 'candidate-invalidated'}]


def test_v683_release_contract():
    from pengucro import __version__, __release_sequence__
    from pengucro.patch_notes import PATCH_NOTES
    assert __version__ == '6.83' and __release_sequence__ == 6830001
    assert PATCH_NOTES[0].version == __version__
