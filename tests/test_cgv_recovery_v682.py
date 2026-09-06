"""Final registered engine regressions; no live CGV bookings."""
import json
from types import SimpleNamespace

import pytest

from engines.registry import EngineRegistry
from pengucro.models import STANDARD_MODE
from engines.cgv_engine import CgvEngine as Base
from engines.cgv_client import CgvSeatGroup
from engines.cgv_schedule_observer import ScheduleObserver, run_schedule_wave
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_MOV_NO
from test_cgv_opening_regressions import schedule, seats, engine_for, Parent
from test_cgv_preopen_v681 import preopen, choose, node_run


def registered():
    return EngineRegistry.create(site_name='CGV', mode=STANDARD_MODE, payload={},
        custom_sites={}, log_callback=lambda *_: None, success_callback=None)


def setup_monitor(e):
    starts = []
    e._current_page = lambda p: p
    e._sync_runtime_handles_from_page = lambda _: None
    e._browser_auth_data = lambda _: {}
    e._consume_initial_seat_response = lambda _: {}
    e._start_fast_seat_monitor = lambda *a, **k: starts.append(1) or True
    e._stop_fast_seat_monitor = lambda _: None
    e._fast_monitor_attempt_id = 'current'
    e.FAST_MONITOR_RECONCILE_SECONDS = 0
    return starts


@pytest.mark.parametrize('valid', [True, False])
@pytest.mark.parametrize('priority', [True, False])
def test_registered_guard_recovers_receipt_before_any_fallback(monkeypatch, valid, priority):
    e = registered()
    starts = setup_monitor(e)
    row = schedule('1100', '2')
    group = CgvSeatGroup(('H21', 'H22'))
    if priority:
        e._priority_preopen_monitor = True
        e._priority_preferred_times = ['11:00']
        e._priority_movie = '오디세이'
        e._priority_auditorium = 'IMAX관'
        e._priority_format = 'IMAX LASER 2D'
        e._priority_schedule_payload = {'data': [row]}
        e._priority_manual_groups = (group,)
        e._read_schedule_once = lambda *a, **k: (group, seats(*group.seats), 200)
    receipt = {'group': list(group.seats), 'data': seats(*group.seats), 'transaction': {
        'holdPayload': dict(row), 'priceResponse': {'statusCode': 0},
        'holdResponse': {'statusCode': 0, 'data': {'movAtktNo': 'existing', 'resultCode': 0 if valid else 9}}}}
    reads, submitted = [], []
    page = SimpleNamespace(evaluate=lambda script, arg: reads.append(arg) or receipt)
    monkeypatch.setattr(Base, '_read_fast_seat_monitor', staticmethod(lambda _: {}))
    e._prepare_api_hold_ui = lambda *_: True
    e._sync_held_seats_for_checkout = lambda *_: True
    e._install_cached_hold_responses = lambda *_: None
    e._restore_fetch = lambda _: None
    e._submit_seat_selection = lambda _: submitted.append(1) or True
    e._cancel_api_hold = lambda *_: pytest.fail('cancelled won hold')
    assert e._watch_and_hold_api(page, row, (group,), 2, False, {}) == (valid, False)
    assert starts == [1] and reads == ['current']
    assert submitted == ([1] if valid else [])
    if not valid:
        assert e._last_fast_monitor_exit_reason == 'hold-uncertain'


def test_uncertain_snapshot_does_not_end_late_receipt_reconciliation(monkeypatch):
    e = registered()
    row = schedule('1100', '2')
    group = CgvSeatGroup(('H21', 'H22'))
    e._fast_monitor_attempt_id = 'current'
    e.FAST_MONITOR_RECONCILE_SECONDS = 0.2
    now, reads = [100.0], []
    monkeypatch.setattr('engines.cgv_engine.time.monotonic', lambda: now[0])
    class Stop:
        def is_set(self): return False
        def wait(self, delay): now[0] += delay; return False
    e.stop_event = Stop()
    receipt = {'group': list(group.seats), 'transaction': {'holdPayload': row,
        'priceResponse': {'statusCode': 0}, 'holdResponse': {'statusCode': 0,
        'data': {'movAtktNo': 'existing'}}}}
    def evaluate(script, arg):
        reads.append(1)
        return receipt if len(reads) == 3 else None
    monkeypatch.setattr(Base, '_read_fast_seat_monitor', staticmethod(lambda _: {
        'attemptId': 'current', 'terminalError': 'hold-uncertain'}))
    assert e._recover_fast_monitor_snapshot(SimpleNamespace(evaluate=evaluate), row, (group,))['hit'] == receipt
    assert len(reads) == 3


@pytest.mark.parametrize('status', [200, 401, 403, 429])
def test_late_completed_schedule_response_is_not_discarded(monkeypatch, status):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_schedule_observer.time.monotonic', lambda: now[0])
    calls = []
    def evaluate(script, arg):
        calls.append(arg['action'])
        if len(calls) == 1: return {'state': 'started'}
        return {'state': 'done', 'result': {'ok': status == 200, 'status': status,
            'data': {'statusCode': 0, 'data': [schedule('1430', '3')]}, 'elapsedMs': 100}}
    e = registered()
    e._priority_schedule_url = 'schedule'
    page = SimpleNamespace(evaluate=evaluate)
    e._refresh_priority_schedule_payload(page)
    now[0] = 106.1
    e._refresh_priority_schedule_payload(page)
    assert calls == ['step', 'step']
    assert e._priority_schedule_blocked is (status != 200)
    if status == 200:
        assert e._priority_schedule_payload['data'][0]['scnsrtTm'] == '1430'


def test_wave_collects_completed_result_even_after_host_pause(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_schedule_observer.time.monotonic', lambda: now[0])
    def evaluate(script, arg):
        if isinstance(arg, dict): now[0] = 107
        return {'present': True, 'result': {'ok': True, 'status': 200}}
    assert run_schedule_wave(SimpleNamespace(evaluate=evaluate), 'async () => ({})', {},
                             SimpleNamespace(is_set=lambda: False), 6)['ok']


@pytest.mark.parametrize('phase,terminal,limit', [('pricing', 'prehold-timeout', 9),
                                               ('holding', 'hold-uncertain', 9),
                                               ('monitoring', 'prehold-timeout', 3)])
def test_registered_engine_enforces_host_deadline(monkeypatch, phase, terminal, limit):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_engine.time.monotonic', lambda: now[0])
    e = registered()
    starts = setup_monitor(e)
    e._priority_claim_returns_on_conflict = True
    class Stop:
        def is_set(self): return False
        def wait(self, delay):
            now[0] += max(0.25, delay)
            assert now[0] < 120, 'host deadline did not terminate waiting'
            return False
    e.stop_event = Stop()
    e._recover_fast_monitor_snapshot = lambda *_: {}
    monkeypatch.setattr(Base, '_read_fast_seat_monitor', staticmethod(lambda _: {
        'running': True, 'claiming': phase != 'monitoring', 'phase': phase,
        'completed': 0, 'inflight': 1}))
    controls = []
    page = SimpleNamespace(evaluate=lambda script, arg: controls.append(arg) or {'terminalError': terminal})
    result = e._watch_and_hold_api(page, schedule('1100', '2'), (CgvSeatGroup(('H21','H22')),), 2, False, {})
    assert result == (False, False) and starts == [1]
    assert e._last_fast_monitor_exit_reason == terminal
    assert now[0] - 100 <= limit and len(controls) == 1


def test_single_published_screening_retries_only_proven_unsubmitted_timeout(monkeypatch):
    e = registered()
    starts = setup_monitor(e)
    waited = []
    e.stop_event = SimpleNamespace(is_set=lambda: False,
        wait=lambda delay: waited.append(delay) or False)
    e._recover_fast_monitor_snapshot = lambda *_: {}
    snapshots = iter([{'running': False, 'terminalError': 'prehold-timeout'},
                      {'running': False, 'terminalError': 'hold-uncertain'}])
    monkeypatch.setattr(Base, '_read_fast_seat_monitor', staticmethod(lambda _: next(snapshots)))
    assert e._watch_and_hold_api(object(), schedule('1100','2'), (), 2, False, {}) == (False, False)
    assert starts == [1, 1] and 1.0 in waited
    assert e._last_fast_monitor_exit_reason == 'hold-uncertain'


def test_prehold_timeout_reaches_next_time_without_old_seat_refresh(monkeypatch):
    a, b = schedule('1100','2'), schedule('1430','3')
    e = engine_for(a, b, seats('H21','H22'))
    e._priority_manual_groups = (CgvSeatGroup(('H21','H22')),)
    attempts = []
    def hold(self, page, row, *args):
        attempts.append(row['scnsrtTm'])
        self._last_fast_monitor_exit_reason = 'prehold-timeout'
        return row == b, False
    monkeypatch.setattr(Parent, '_watch_and_hold_api', hold)
    e._fetch_priority_seat_payload = lambda *_: pytest.fail('unnecessary refresh of timed-out candidate')
    assert e._watch_and_hold_api(object(), a, (), 2, False, {}) == (True, False)
    assert attempts == ['1100','1430']


@pytest.mark.parametrize('known,actual,expected', [('', '', None), ('known','', 'known'),
                                               ('known','other', None), ('known','known','known')])
def test_partial_movie_id_uses_only_verified_identity(known, actual, expected):
    row = dict(schedule('1100','2'), movNo=actual)
    token = _PREOPEN_MOV_NO.set(known)
    try:
        with preopen():
            chosen = choose(row)
            assert (chosen.get('movNo') if chosen else None) == expected
            if chosen:
                assert Base._direct_hold_config(chosen, 2, {}, {})['schedule']['movNo'] == expected
            assert row['movNo'] == actual  # Never mutate the server payload.
    finally:
        _PREOPEN_MOV_NO.reset(token)


def test_candidate_auth_reuse_is_one_shot_scoped_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_engine_priority_ladder_runtime.time.monotonic', lambda: now[0])
    e, page, row = registered(), object(), schedule('1100','2')
    reads = []
    e._browser_auth_data = lambda _: reads.append(1) or {'custNo': str(len(reads))}
    e._priority_seed_page = page
    e._priority_auth_snapshot = (page, e._schedule_key(row), {'custNo': 'preflight'}, 100)
    e._seed_initial_payload(row, seats('H21','H22'))
    assert e._auth_for_hold(page, row) == {'custNo': 'preflight'}
    assert reads == [] and e._priority_auth_snapshot is None
    assert e._auth_for_hold(page, row) == {'custNo': '1'}
    e._priority_auth_snapshot = (page, e._schedule_key(row), {'custNo': 'old'}, 100)
    now[0] = 102
    assert e._auth_for_hold(page, row) == {'custNo': '2'}
    e._priority_auth_snapshot = (object(), e._schedule_key(row), {'custNo': 'other-page'}, 102)
    assert e._auth_for_hold(page, row) == {'custNo': '3'}


@pytest.mark.parametrize('limited', [False, True])
def test_first_candidate_reuses_auth_and_obeys_limit_during_preflight(monkeypatch, limited):
    e = registered()
    a, b = schedule('1100', '2'), schedule('1430', '3')
    group = CgvSeatGroup(('H21', 'H22'))
    e._priority_preferred_times = ['11:00', '14:30']
    e._priority_movie = '오디세이'
    e._priority_auditorium = 'IMAX관'
    e._priority_format = 'IMAX LASER 2D'
    e._priority_schedule_payload = {'data': [a, b]}
    e._priority_manual_groups = (group,)
    e._refresh_priority_schedule_payload = lambda _: None
    e._current_page = lambda p: p
    e._sync_runtime_handles_from_page = lambda _: None
    calls = []
    e._browser_auth_data = lambda _: calls.append('auth') or {'custNo': 'member'}
    def read(*args, **kwargs):
        calls.append('seat')
        if limited:
            e._priority_schedule_blocked = True
        return {'ok': True, 'status': 200, 'data': seats(*group.seats)}
    monkeypatch.setattr('engines.cgv_engine_priority_ladder.run_schedule_wave', read)
    def start(*args, **kwargs):
        calls.append('install')
        e.stop_event.set()
        return True
    e._start_fast_seat_monitor = start
    e._stop_fast_seat_monitor = lambda _: None
    assert e._watch_and_hold_api(object(), a, (group,), 2, False, {}) == (False, False)
    assert calls == (['auth', 'seat'] if limited else ['auth', 'seat', 'install'])


def test_housekeeping_collects_schedule_during_claim_and_defers_hold_cancellation(monkeypatch):
    e = registered()
    e._priority_monitor_service_at = 0
    reads = []
    def refresh(page):
        reads.append(1)
        e._priority_schedule_blocked = True
    e._refresh_priority_schedule_payload = refresh
    actions = []
    e._interrupt_fast_monitor = lambda page, **kw: actions.append(kw) or {}
    assert e._monitor_housekeeping(object()) == {}
    assert reads == [1] and actions == [{'only_before_hold': True}]


def monitor_scripts(only_before_hold):
    captured = []
    page = SimpleNamespace(evaluate=lambda script, arg: captured.append((script,arg)) or True)
    e = registered()
    e._start_fast_seat_monitor(page, 'seat', (CgvSeatGroup(('H21','H22')),), 1,
        initial_payload=seats('H21','H22'), max_conflicts=1,
        direct_hold={'schedule':schedule('1100','2'),'auth':{},'people':2,'priceUrl':'price','holdUrl':'hold'})
    e._interrupt_fast_monitor(page, only_before_hold=only_before_hold)
    return captured


@pytest.mark.parametrize('phase,restriction,expected', [('price',False,'prehold-timeout'),
    ('hold',False,'hold-uncertain'), ('price',True,'schedule-observer-blocked'), ('hold',True,None)])
def test_actual_js_atomic_stop_prevents_late_price_from_sending_hold(phase, restriction, expected):
    scripts = monitor_scripts(restriction)
    result = node_run({'scripts':scripts, 'phase':phase}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setTimeout=()=>1;global.clearTimeout=()=>{};global.setInterval=()=>1;global.clearInterval=()=>{};
let finish;const requests=[];
const response=data=>({ok:true,status:200,headers:new Headers(),json:async()=>data});
global.fetch=(url,opts)=>{requests.push(url);if(url===spec.phase)return new Promise(resolve=>{finish=resolve});
return Promise.resolve(response({statusCode:0}));};
(async()=>{
 eval('('+spec.scripts[0][0]+')')(spec.scripts[0][1]);
 for(let i=0;i<30;i++)await Promise.resolve();
 const decision=eval('('+spec.scripts[1][0]+')')(spec.scripts[1][1]);
 // Even a transport ignoring AbortSignal must not turn a late price into a hold.
 finish(response({statusCode:0,data:{resultCode:0,movAtktNo:'held'}}));
 for(let i=0;i<30;i++)await Promise.resolve();
 const s=window.__pengucroFastSeatMonitor;
 process.stdout.write(JSON.stringify({decision,requests,receipt:!!window.__pengucroCgvHoldReceipt}));s.stop();
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
''')
    assert result['decision'].get('terminalError') == expected
    assert result['requests'] == (['price'] if phase == 'price' else ['price','hold'])
    assert result['receipt'] is (phase == 'hold')


def test_seat_preflight_uses_host_wave_deadline_and_cleanup(monkeypatch):
    e = registered()
    e._browser_auth_data = lambda _: {}
    now, actions = [100.0], []
    monkeypatch.setattr('engines.cgv_schedule_observer.time.monotonic', lambda: now[0])
    class Stop:
        def is_set(self): return False
        def wait(self, delay): now[0] += 0.5; return False
    e.stop_event = Stop()
    def evaluate(script, arg):
        actions.append(arg)
        return {'present': True}
    result = e._fetch_priority_seat_payload(SimpleNamespace(evaluate=evaluate), schedule('1100','2'))
    assert result['timedOut'] and now[0] == 102
    assert sum(isinstance(arg, dict) for arg in actions) == 1
    assert isinstance(actions[-1], str)


def test_v682_release_contract():
    from pathlib import Path
    from pengucro import __version__, __release_sequence__
    from pengucro.patch_notes import PATCH_NOTES
    assert __version__ == '6.82' and __release_sequence__ == 6820001
    assert PATCH_NOTES[0].version == __version__
    assert f'방탈출펭크로{__version__}_yescaptcha' in (Path(__file__).resolve().parents[1]/'방탈출펭크로.spec').read_text()
