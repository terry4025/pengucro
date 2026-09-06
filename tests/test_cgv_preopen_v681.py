"""Sequential opening, zero aggregate, confirmed holds and timer starvation."""
import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from engines.cgv_client import CgvSeatGroup
from engines.cgv_engine import CgvEngine as BaseEngine
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as Parent
from engines.cgv_engine_priority_ladder_runtime import CgvEngine as PriorityRuntime
from engines.cgv_engine_movie_identity_runtime import (
    CgvEngine as IdentityEngine, _PREOPEN_SELECTION_ACTIVE, _PREOPEN_TIME_DRIFT,
    _PREOPEN_ZERO_PROBE, select_schedule)
from engines.cgv_preopen_matching import normalize_preopen_time_drift
from engines.cgv_schedule_observer import ScheduleObserver, run_schedule_wave
from test_cgv_opening_regressions import engine_for, schedule, seats


@contextmanager
def preopen(drift=0):
    variables = [(_PREOPEN_SELECTION_ACTIVE, True), (_PREOPEN_TIME_DRIFT, drift),
                 (_PREOPEN_ZERO_PROBE, True)]
    tokens = [(var, var.set(value)) for var, value in variables]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def choose(*rows):
    return select_schedule({'data': list(rows)}, movie='오디세이',
                           auditorium='IMAX관', format_name='IMAX LASER 2D',
                           preferred_times=['11:00'])


def test_zero_aggregate_uses_real_seats_and_does_not_hold_an_empty_map(monkeypatch):
    row = dict(schedule('1100', '2'), frSeatCnt=0, stcnt=624)
    e = engine_for(row, row, seats())
    del e._read_schedule_once  # Exercise the actual preflight and selection.
    e._priority_preferred_times = ['11:00']
    e._priority_preopen_monitor = True
    e._priority_manual_groups = (CgvSeatGroup(('H21', 'H22')),)
    reads, holds = [], []
    def fetch(*args):
        reads.append(1)
        assert len(reads) <= 2
        return {'ok': True, 'status': 200,
                'data': seats() if len(reads) == 1 else seats('H21', 'H22')}
    e._fetch_priority_seat_payload = fetch
    def hold(self, page, actual, groups, *args):
        holds.append((actual, groups[0].seats, len(reads)))
        return True, False
    monkeypatch.setattr(Parent, '_watch_and_hold_api', hold)
    with preopen():
        assert choose(row) == row
        assert e._watch_and_hold_api(object(), row, e._priority_manual_groups, 2, False, {}) == (True, False)
    assert holds == [(row, ('H21', 'H22'), 2)]


@pytest.mark.parametrize('change', [{'cntlYn': 'Y'}, {'scnSseq': ''},
                                   {'movNm': '다른 영화', 'expoProdNm': '다른 영화'},
                                   {'expoScnsNm': '2관', 'movkndDsplEnm': '2D'}])
def test_zero_aggregate_does_not_relax_identity_or_control_guards(change):
    row = dict(schedule('1100', '2'), frSeatCnt=0, stcnt=624, **change)
    with preopen():
        assert choose(row) is None


@pytest.mark.parametrize('drift,minute,allowed', [(0, '1110', False), (15, '1110', True),
                                                (15, '1130', False), (90, '1230', True)])
def test_drift_is_bounded_and_explicit(drift, minute, allowed):
    row = schedule(minute, '2')
    with preopen(drift):
        assert (choose(row) is not None) is allowed


@pytest.mark.parametrize('raw,expected', [(None, 0), ('bad', 0), (-1, 0), (999, 0),
                                       (True, 0), (15.5, 0), ('30', 30), (90, 90)])
def test_untrusted_time_choice_cannot_widen_window(raw, expected):
    assert normalize_preopen_time_drift(raw) == expected


@pytest.mark.parametrize('metadata,expected', [({}, False), ({'preopen_time_drift_minutes': 15}, True)])
def test_actual_thread_installs_and_restores_time_and_zero_options(monkeypatch, metadata, expected):
    observed = []
    def run(self, data):
        observed.append((choose(dict(schedule('1110', '2'), frSeatCnt=0, stcnt=624)) is not None,
                         self._priority_preopen_monitor))
        raise RuntimeError('scope test')
    monkeypatch.setattr(PriorityRuntime, 'make_reservation_thread', run)
    e = IdentityEngine(lambda *_: None)
    with pytest.raises(RuntimeError, match='scope test'):
        e.make_reservation_thread({'engine_metadata': {'cgv': {'is_preopen': True, **metadata}}})
    assert observed == [(expected, True)]
    assert not _PREOPEN_SELECTION_ACTIVE.get() and not _PREOPEN_ZERO_PROBE.get()
    assert _PREOPEN_TIME_DRIFT.get() == 0 and not e._priority_preopen_monitor


def test_later_publication_joins_current_pass_before_first_time_repeats(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_engine_priority_ladder.time.monotonic', lambda: now[0])
    a, b = schedule('1100', '2'), schedule('1430', '3')
    payload = seats(*[f'I{i}' for i in range(1, 45)])
    e = engine_for(a, b, payload)
    e._priority_auto_mode = 'comfortable'
    def refresh(page):
        e._priority_schedule_payload = {'data': [a, b] if now[0] >= 101 else [a]}
    e._refresh_priority_schedule_payload = refresh
    e._fetch_priority_seat_payload = lambda *_: {'ok': True, 'status': 200, 'data': payload}
    attempts = []
    def hold(self, page, row, *args):
        attempts.append((row['scnsrtTm'], now[0] - 100))
        assert len(attempts) <= 4
        if row == b:
            return True, False
        now[0] += 1
        self._last_fast_monitor_exit_reason = 'seat-conflict'
        return False, False
    monkeypatch.setattr(Parent, '_watch_and_hold_api', hold)
    with preopen():
        assert e._watch_and_hold_api(object(), a, (), 4, False, {}) == (True, False)
    assert attempts == [('1100', 0), ('1100', 1), ('1100', 2), ('1430', 3)]


def test_fresh_schedule_removes_controlled_primary_and_foreign_date():
    a, b = schedule('1100', '2'), schedule('1430', '3')
    e = engine_for(a, b, seats())
    e._priority_schedule_payload = {'data': [dict(a, cntlYn='Y'), b, dict(a, scnYmd='20260913')]}
    with preopen():
        assert e._ordered_schedule_candidates(a) == [b]


@pytest.mark.parametrize('failure', ['prepare', 'sync', 'submit', 'exception'])
def test_confirmed_hold_ui_failure_never_cancels_or_enables_rebooking(failure):
    e, actions = BaseEngine(lambda *_: None), []
    def prepare(*args):
        actions.append('prepare')
        return failure != 'prepare'
    def sync(*args):
        actions.append('sync')
        if failure == 'exception':
            raise RuntimeError('CDP disconnected')
        return failure != 'sync'
    e._prepare_api_hold_ui = prepare
    e._sync_held_seats_for_checkout = sync
    e._install_cached_hold_responses = lambda *_: actions.append('cache')
    e._submit_seat_selection = lambda *_: actions.append('submit') or False
    e._restore_fetch = lambda *_: actions.append('restore')
    e._cancel_api_hold = lambda *_: pytest.fail('won hold cancelled')
    assert e._connect_confirmed_hold(object(), {}, 2, {}, (), {}, {'id': 'held'}, {}, False) == (False, False)
    assert e._confirmed_hold_for_recovery == ({}, {'id': 'held'})
    assert actions.count('prepare') == (2 if failure == 'prepare' else 1)
    assert actions.count('submit') <= 1 and actions[-1] == 'restore'


def test_same_hold_connects_after_one_prepare_retry():
    e, actions = BaseEngine(lambda *_: None), []
    ready = iter([False, True])
    e._prepare_api_hold_ui = lambda *_: next(ready)
    e._sync_held_seats_for_checkout = lambda *_: True
    e._install_cached_hold_responses = lambda *args: actions.append(args[-2:])
    e._submit_seat_selection = lambda *_: True
    e._restore_fetch = lambda *_: None
    e._cancel_api_hold = lambda *_: pytest.fail('cancel')
    price, hold = {'price': 'existing'}, {'hold': 'existing'}
    assert e._connect_confirmed_hold(object(), {}, 2, {}, (), price, hold, {}, False) == (True, False)
    assert actions == [(price, hold)] and e._confirmed_hold_for_recovery is None


def test_developer_mode_still_cleans_up_failed_ui():
    e, cleaned = BaseEngine(lambda *_: None), []
    e._prepare_api_hold_ui = lambda *_: False
    e._restore_fetch = lambda *_: None
    e._release_developer_api_hold = lambda *_: cleaned.append(e._developer_hold_cleanup) or True
    assert e._connect_confirmed_hold(object(), {}, 2, {}, (), {}, {'id': 1}, {}, True) == (False, False)
    assert cleaned == [({}, {'id': 1})] and e._confirmed_hold_for_recovery is None


def test_observer_deadline_uses_host_clock_and_cleans_pending_get(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr('engines.cgv_schedule_observer.time.monotonic', lambda: now[0])
    page = SimpleNamespace(evaluate=lambda script, arg: calls.append(arg['action']) or {'state': 'started'})
    observer = ScheduleObserver('schedule')
    assert observer.step(page) is None
    now[0] = 106
    assert observer.step(page)['timedOut']
    assert calls == ['step', 'cancel']
    assert observer.step(page) is None  # Backoff; no immediate replacement.


@pytest.mark.parametrize('status', [401, 403, 429])
def test_observer_restrictions_stop_later_schedule_and_hold_requests(status):
    calls = []
    page = SimpleNamespace(evaluate=lambda script, arg: calls.append(arg) or {
        'state': 'done', 'result': {'ok': False, 'status': status}})
    e = CgvEngine(lambda *_: None)
    e._priority_schedule_url = 'schedule'
    e._refresh_priority_schedule_payload(page)
    assert e._priority_schedule_blocked
    e._refresh_priority_schedule_payload(page)
    assert len(calls) == 1


def test_form_keeps_time_opt_in_in_engine_payload(monkeypatch):
    import ui.reservation_form_runtime as module
    from pengucro.models import ReservationRequest
    request = ReservationRequest(site='CGV', branch='0013', reservation_date='2026-09-12',
        reservation_time='11:00:00', name='', phone='', people=2, theme_pk='오디세이',
        engine_metadata={'cgv': {'seats': 'H21,H22'}})
    monkeypatch.setattr(module.BaseReservationForm, 'get_reservation_data', lambda _: (request, '', 1, False))
    form = SimpleNamespace(_site_uses_cgv=lambda: True, cgv_selection={'preopen_time_drift_minutes': 30})
    result, *_ = module.ReservationForm.get_reservation_data(form)
    assert result.to_engine_payload()['engine_metadata']['cgv']['preopen_time_drift_minutes'] == 30


def node_run(spec, harness):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for browser JavaScript regression')
    result = subprocess.run([node, '-e', harness], input=json.dumps(spec),
                            text=True, capture_output=True, timeout=5, check=True)
    return json.loads(result.stdout)


def test_observer_javascript_is_single_flight_and_cancellable_without_timers():
    result = node_run({'script': ScheduleObserver.STEP_SCRIPT}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={}; global.document={cookie:'',visibilityState:'hidden'};
global.setTimeout=()=>{throw Error('timer not allowed')};
let requests=0, signal;
global.fetch=(url,opts)=>{requests++;signal=opts.signal;return new Promise(()=>{});};
const step=eval('('+spec.script+')'), arg={key:'k',url:'schedule',action:'step'};
const first=step(arg),second=step(arg); step({...arg,action:'cancel'});
process.stdout.write(JSON.stringify({requests,first,second,aborted:signal.aborted,
                                    entries:Object.keys(window.__pengucroScheduleObservers).length}));
''')
    assert result == {'requests': 1, 'first': {'state': 'started'}, 'second': {'state': 'running'},
                      'aborted': True, 'entries': 0}


def capture_wave():
    calls = []
    def evaluate(script, arg):
        calls.append((script, arg))
        return {'present': True, 'result': {'ok': True}}
    e = CgvEngine(lambda *_: None)
    e._run_schedule_race_once(SimpleNamespace(evaluate=evaluate), 'schedule', 2)
    return calls


@pytest.mark.parametrize('status', [200, 401, 403, 429])
def test_first_wave_fetch_runs_even_if_all_chrome_timers_are_delayed(status):
    calls = capture_wave()
    result = node_run({'start': calls[0], 'read': calls[1], 'close': calls[2], 'status': status}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={};global.document={cookie:'',visibilityState:'hidden'};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
let requests=0;
global.fetch=async()=>{requests++;return {ok:spec.status===200,status:spec.status,json:async()=>({data:[]})};};
(async()=>{
 eval('('+spec.start[0]+')')(spec.start[1]); const immediate=requests;
 for(let i=0;i<30;i++)await Promise.resolve();
 const state=eval('('+spec.read[0]+')')(spec.read[1]);
 eval('('+spec.close[0]+')')(spec.close[1]);
 process.stdout.write(JSON.stringify({immediate,requests,state,entries:Object.keys(window.__pengucroScheduleWaves).length}));
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
''')
    assert result['immediate'] == result['requests'] == 1
    assert result['state']['result']['ok'] is (status == 200)
    assert result['state']['result']['status'] == status and result['entries'] == 0
    if status == 200:
        assert result['state']['result']['dispatches'][0]['visibility'] == 'hidden'


def test_pending_wave_get_is_aborted_when_host_cleans_up():
    calls = capture_wave()
    result = node_run({'start': calls[0], 'close': calls[2]}, r'''
const spec=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setTimeout=()=>1;global.clearTimeout=()=>{};
let signal,requests=0;
global.fetch=(url,opts)=>{requests++;signal=opts.signal;return new Promise(()=>{});};
eval('('+spec.start[0]+')')(spec.start[1]);
eval('('+spec.close[0]+')')(spec.close[1]);
process.stdout.write(JSON.stringify({requests,aborted:signal.aborted,
                                    entries:Object.keys(window.__pengucroScheduleWaves).length}));
''')
    assert result == {'requests': 1, 'aborted': True, 'entries': 0}


def test_schedule_restriction_in_priority_pass_stops_before_new_hold(monkeypatch):
    a, b = schedule('1100', '2'), schedule('1430', '3')
    e = engine_for(a, b, seats('H21', 'H22'))
    del e._refresh_priority_schedule_payload
    e._priority_schedule_url = 'schedule'
    e._priority_manual_groups = (CgvSeatGroup(('H21', 'H22')),)
    calls = []
    def evaluate(script, arg):
        calls.append(arg['action'])
        return {'state': 'done', 'result': {'ok': False, 'status': 429}}
    monkeypatch.setattr(Parent, '_watch_and_hold_api', lambda *_: pytest.fail('new hold after 429'))
    assert e._watch_and_hold_api(SimpleNamespace(evaluate=evaluate), a, (), 2, False, {}) == (False, False)
    assert calls == ['step', 'cancel']


def test_host_wave_timeout_cleans_get_without_retrying_submission(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr('engines.cgv_schedule_observer.time.monotonic', lambda: now[0])
    class Stop:
        def is_set(self): return False
        def wait(self, delay): now[0] += 3
    page = SimpleNamespace(evaluate=lambda script, arg: calls.append((script, arg)) or {'present': True})
    result = run_schedule_wave(page, 'async () => ({})', {}, Stop(), 6)
    assert result['error'] == 'schedule-host-timeout'
    assert len([arg for _, arg in calls if isinstance(arg, dict)]) == 1
    assert 'delete entries[key]' in calls[-1][0]


def test_v681_version_sequence_and_executable_match():
    from pengucro import __version__, __release_sequence__
    from pengucro.patch_notes import PATCH_NOTES
    assert __version__ == '6.81' and __release_sequence__ == 6810001
    assert PATCH_NOTES[0].version == __version__
    spec = (Path(__file__).resolve().parents[1] / '방탈출펭크로.spec').read_text()
    assert f'방탈출펭크로{__version__}_yescaptcha' in spec
