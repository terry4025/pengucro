"""Opening speed and bounded priority recovery; no live requests or bookings."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from engines.cgv_client import CgvSeatGroup, parse_api_seats
from engines.cgv_engine import CgvEngine as BaseEngine
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as Parent
from test_cgv_opening_regressions import engine_for, schedule, seats


def test_auto_quota_switch_does_not_read_previous_screening_again(monkeypatch):
    a, b = schedule('1100', '2'), schedule('1430', '3')
    payload = seats(*[f'I{i}' for i in range(1,45)])
    e = engine_for(a, b, payload)
    e._priority_auto_mode = 'comfortable'
    reads, attempts = [], []
    def refresh(page, screening):
        reads.append(screening['scnsrtTm'])
        return {'ok': True, 'status': 200, 'data': payload}
    def hold(self, page, screening, groups, *args):
        attempts.append(screening['scnsrtTm'])
        if screening == b:
            return True, False
        self._last_fast_monitor_exit_reason = 'seat-conflict'
        return False, False
    e._fetch_priority_seat_payload = refresh
    monkeypatch.setattr(Parent, '_watch_and_hold_api', hold)
    assert e._watch_and_hold_api(object(), a, (), 4, False, {}) == (True, False)
    assert attempts == ['1100'] * 3 + ['1430']
    assert reads == ['1100'] * 2  # No third refresh before switching.


def test_unrelated_sales_do_not_restart_failed_auto_shortlist(monkeypatch):
    a, b = schedule('1100', '2'), schedule('1430', '3')
    payload = seats(*[f'I{i}' for i in range(1,45)], 'A1')
    changed = seats(*[f'I{i}' for i in range(1,45)])
    e = engine_for(a, b, payload)
    e._priority_auto_mode = 'comfortable'
    attempts = []
    def read(page, screening, people, **kwargs):
        current = changed if len(attempts) >= 6 else payload
        return e._choose_priority_group(current, screening, people), current, 200
    e._read_schedule_once = read
    e._fetch_priority_seat_payload = lambda *_: {'ok': True, 'status': 200, 'data': payload}
    def hold(self, page, screening, groups, *args):
        attempts.append((screening, groups[0].seats))
        if len(attempts) == 7:
            return True, False
        self._last_fast_monitor_exit_reason = 'seat-conflict'
        return False, False
    monkeypatch.setattr(Parent, '_watch_and_hold_api', hold)
    assert e._watch_and_hold_api(object(), a, (), 4, False, {}) == (True, False)
    assert attempts[-1][1] not in [group for _, group in attempts[:3]]


def test_failed_group_reenabled_only_after_own_reopening_or_ttl(monkeypatch):
    e = CgvEngine(lambda *_: None)
    now = [100.0]
    monkeypatch.setattr('engines.cgv_engine_priority_ladder.time.monotonic', lambda: now[0])
    key, group = ('screening',), ('H21', 'H22')
    e._priority_failures[key] = {group: (100, False)}
    assert group in e._priority_failed_groups(key, seats('H21', 'H22'))
    assert group in e._priority_failed_groups(key, seats('H21'))
    assert group not in e._priority_failed_groups(key, seats('H21', 'H22'))
    e._priority_failures[key] = {group: (100, False)}
    now[0] = 131
    assert group not in e._priority_failed_groups(key, seats('H21', 'H22'))


@pytest.mark.parametrize('mode,expected', [
    ('fast', [('1100','H1'),('1100','H3'),('1430','H1'),('1430','H3'),('1100','H5')]),
    ('strict', [('1100','H1'),('1100','H3'),('1100','H5'),('1430','H1')]),
])
def test_rotation_budget_preserves_manual_order_and_resumes(monkeypatch, mode, expected):
    now = [100.0]
    monkeypatch.setattr('engines.cgv_engine_priority_ladder.time.monotonic', lambda: now[0])
    a, b = schedule('1100','2'), schedule('1430','3')
    payload = seats(*[f'H{i}' for i in range(1,7)])
    e = engine_for(a, b, payload)
    e._priority_rotation_mode = mode
    e._priority_manual_groups = tuple(CgvSeatGroup((f'H{i}',f'H{i+1}')) for i in (1,3,5))
    e._fetch_priority_seat_payload = lambda *_: {'ok':True,'status':200,'data':payload}
    calls=[]
    def hold(self,page,screening,groups,*args):
        calls.append((screening['scnsrtTm'],groups[0].seats[0]))
        now[0] += 1.6
        if len(calls) == len(expected): return True,False
        assert len(calls) < 8
        self._last_fast_monitor_exit_reason='seat-conflict'
        return False,False
    monkeypatch.setattr(Parent,'_watch_and_hold_api',hold)
    assert e._watch_and_hold_api(object(),a,e._priority_manual_groups,2,False,{}) == (True,False)
    assert calls == expected


def test_rank_cache_reuses_geometry_but_uses_current_availability(monkeypatch):
    import engines.cgv_engine_priority_ladder as module
    e = CgvEngine(lambda *_: None)
    e._priority_auto_mode = 'comfortable'
    a = schedule('1100','2')
    original = tuple(parse_api_seats(seats(*[f'I{i}' for i in range(1,45)])))
    calls=[]
    rank = module.rank_recommended_seat_groups
    def count(*args,**kwargs):
        calls.append(1)
        return rank(*args,**kwargs)
    monkeypatch.setattr(module,'rank_recommended_seat_groups',count)
    first=e._auto_available_group(original,a,4)
    sold=tuple(replace(s,available=False,seat_status_cd='01') if s.label in first.seats else s for s in original)
    second=e._auto_available_group(sold,a,4)
    assert first != second
    assert not set(first.seats) & set(second.seats)
    assert len(calls) == 1
    modified=tuple(replace(s,left_passage=True) if s.label=='I22' else s for s in sold)
    e._auto_available_group(modified,a,4)
    assert len(calls) == 2  # A physical layout change invalidates the cached rank.


@pytest.mark.parametrize('elapsed,expected',[(0.3,0.7),(1.5,0.05)])
def test_poll_period_accounts_for_response_time_without_catchup(monkeypatch,elapsed,expected):
    e=BaseEngine(lambda *_:None)
    waits=[]
    e.stop_event=SimpleNamespace(wait=waits.append)
    monkeypatch.setattr('engines.cgv_engine.time.monotonic',lambda:100+elapsed)
    e._wait_schedule_cycle(100,1)
    assert waits == [pytest.approx(expected)]


def receipt_for(screening):
    return {'group':['H21','H22'],'data':seats('H21','H22'),'transaction':{
        'holdPayload':dict(screening), 'priceResponse':{'statusCode':0},
        'holdResponse':{'statusCode':0,'data':{'resultCode':0,'movAtktNo':'owned-hold'}},
        'timing':{'started':0,'holdStarted':100}}}


@pytest.mark.parametrize('change', ['none','wrong-time','wrong-seat','no-id','rejected','bad-price'])
def test_recovery_requires_matching_confirmed_receipt(change):
    e=BaseEngine(lambda *_:None)
    e._fast_monitor_attempt_id='current-attempt'
    e.FAST_MONITOR_RECONCILE_SECONDS=0
    a=schedule('1100','2')
    receipt=receipt_for(a)
    if change=='wrong-time':receipt['transaction']['holdPayload']['scnSseq']='other'
    if change=='wrong-seat':receipt['group']=['A1','A2']
    if change=='no-id':receipt['transaction']['holdResponse']['data'].pop('movAtktNo')
    if change=='rejected':receipt['transaction']['holdResponse']['data']['resultCode']=7
    if change=='bad-price':receipt['transaction']['priceResponse']['statusCode']=-1
    class Page:
        def evaluate(self,script,arg=None):
            assert 'fetch(' not in script  # Recovery performs no HTTP call or mutation.
            return receipt if arg=='current-attempt' else {}
    result=e._recover_fast_monitor_snapshot(Page(),a,(CgvSeatGroup(('H21','H22')),))
    assert bool(result.get('hit')) == (change=='none')


def test_missing_browser_ack_never_authorizes_another_hold(monkeypatch):
    e=BaseEngine(lambda *_:None)
    e._browser_auth_data=lambda _:{}
    e._consume_initial_seat_response=lambda _:{}
    e._direct_hold_config=lambda *_:{}
    e._start_fast_seat_monitor=lambda *args,**kwargs:False
    e._recover_fast_monitor_snapshot=lambda *_:{}
    e._stop_fast_seat_monitor=lambda _:None
    assert e._watch_and_hold_api(object(),schedule('1100','2'),(CgvSeatGroup(('H21','H22')),),2,False,{}) == (False,False)
    assert e._last_fast_monitor_exit_reason=='hold-uncertain'


def test_form_preserves_rotation_choice(monkeypatch):
    import ui.reservation_form_runtime as module
    from pengucro.models import ReservationRequest
    request=ReservationRequest(site='CGV',branch='0013',reservation_date='2026-09-12',
        reservation_time='11:00:00',name='',phone='',people=2,theme_pk='오디세이',
        engine_metadata={'cgv':{'seats':'H21,H22'}})
    monkeypatch.setattr(module.BaseReservationForm,'get_reservation_data',lambda _:(request,'',1,False))
    form=module.ReservationForm.__new__(module.ReservationForm)
    form.cgv_selection={'priority_rotation_mode':'fast'}
    form._site_uses_cgv=lambda:True
    result,*_=module.ReservationForm.get_reservation_data(form)
    assert result.to_engine_payload()['engine_metadata']['cgv']['priority_rotation_mode']=='fast'


def test_failure_logs_distinguish_no_hold_sent_from_hold_response_loss():
    logs=[]
    e=BaseEngine(lambda msg,*_:logs.append(msg))
    e._log_fast_monitor_timing({'timing':{'started':0,'priceStarted':2,'priceFinished':22}})
    assert '가격 왕복 20ms' in logs[-1]
    assert '감시 설치→선점 발송 미확인' in logs[-1]


def test_browser_receipt_survives_monitor_cleanup_and_rejects_stale_ids():
    import json
    import shutil
    import subprocess
    node=shutil.which('node')
    if not node: pytest.skip('Node needed for shipped JavaScript tests')
    e=BaseEngine(lambda *_:None)
    captured=[]
    class Page:
        def evaluate(self,script,arg=None):
            captured.append((script,arg))
            return True
    a=schedule('1100','2')
    e._start_fast_seat_monitor(Page(),'seats',(CgvSeatGroup(('H21','H22')),),1,
        initial_payload=seats('H21','H22'),max_conflicts=1,
        direct_hold={'schedule':a,'auth':{},'people':2,'priceUrl':'price','holdUrl':'hold'})
    start,arg=captured.pop()
    e._stop_fast_seat_monitor(Page())
    stop,_=captured.pop()
    e.FAST_MONITOR_RECONCILE_SECONDS=0
    e._recover_fast_monitor_snapshot(Page(),a,(CgvSeatGroup(('H21','H22')),))
    read_receipt=captured[0][0]
    script=r'''
const fs=require('fs'),s=JSON.parse(fs.readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setInterval=()=>1;global.clearInterval=()=>{};
const posts=[];
global.fetch=async url=>{posts.push(url);return {ok:true,status:200,headers:new Headers(),
 json:async()=>url==='price'?{statusCode:0}:{statusCode:0,data:{resultCode:0,movAtktNo:'owned'}}}};
(async()=>{
 eval('('+s.start+')')(s.arg);
 for(let i=0;i<60;i++)await Promise.resolve();
 const timing=window.__pengucroFastSeatMonitor.timing;
 eval('('+s.stop+')')();
 const read=eval('('+s.read+')');
 const hit=!!read(s.arg.attemptId),wrong=read('previous-attempt');
 window.__pengucroCgvHoldReceipt.confirmedAt=performance.now()-16000;
 const stale=read(s.arg.attemptId);
 process.stdout.write(JSON.stringify({posts,hit,wrong,stale,timing,removed:!window.__pengucroFastSeatMonitor}));
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
'''
    proc=subprocess.run([node,'-e',script],input=json.dumps({'start':start,'arg':arg,'stop':stop,'read':read_receipt}),
        text=True,capture_output=True,check=True,timeout=5)
    result=json.loads(proc.stdout)
    assert result['posts']==['price','hold']
    assert result['hit'] and result['removed']
    assert result['wrong'] is None and result['stale'] is None
    t=result['timing']
    assert t['started'] <= t['seatReady'] <= t['candidateReady'] <= t['priceStarted'] <= t['priceFinished'] <= t['holdStarted'] <= t['holdFinished']


def test_installed_monitor_ack_loss_recovers_checkout_without_new_monitor():
    e=BaseEngine(lambda *_:None)
    a=schedule('1100','2')
    receipt=receipt_for(a)
    e._browser_auth_data=lambda _:{}
    e._consume_initial_seat_response=lambda _:{}
    e._direct_hold_config=lambda *_:{}
    starts=[]
    e._start_fast_seat_monitor=lambda *args,**kwargs: starts.append(1) or False
    e._recover_fast_monitor_snapshot=lambda *_:{'hit':receipt}
    e._stop_fast_seat_monitor=lambda _:None
    e._prepare_api_hold_ui=lambda *_:True
    e._sync_held_seats_for_checkout=lambda *_:True
    e._install_cached_hold_responses=lambda *_:None
    e._submit_seat_selection=lambda _:True
    e._restore_fetch=lambda _:None
    e.FAST_MONITOR_READ_INTERVAL=0
    assert e._watch_and_hold_api(object(),a,(CgvSeatGroup(('H21','H22')),),2,False,{}) == (True,False)
    assert starts==[1]


def test_v680_patch_notes_remain_available():
    from pengucro.patch_notes import PATCH_NOTES
    assert any(note.version == '6.80' for note in PATCH_NOTES)
