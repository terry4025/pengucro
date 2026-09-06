"""September CGV failure regressions. No live bookings or network calls."""
import json
import shutil
import subprocess

import pytest

from engines.cgv_client import CgvSeatGroup
from engines.cgv_engine import CgvEngine as BaseEngine
from engines.cgv_engine_preopen_live_runtime import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as Parent


def schedule(hour, seq):
    return dict(siteNo='0013', scnYmd='20260912', scnsNo='018', scnSseq=seq,
                movNo='test-movie',
                scnsrtTm=hour, movNm='오디세이', expoProdNm='오디세이',
                expoScnsNm='IMAX관', movkndDsplEnm='IMAX LASER 2D')


def seats(*labels):
    return {'statusCode': 0, 'data': {'items': [{'seats': [
        dict(seatLocNo=label, seatRowNm=label[0], seatNo=label[1:],
             seatStusCd='00', seatSaleYn='Y') for label in labels]}]}}


def engine_for(first, second, payload):
    e = CgvEngine(lambda *_: None)
    e._priority_movie = '오디세이'
    e._priority_auditorium = 'IMAX관'
    e._priority_format = 'IMAX LASER 2D'
    e._priority_preferred_times = ['11:00', '14:30']
    e._priority_schedule_payload = {'data': [first, second]}
    e._refresh_priority_schedule_payload = lambda _: None
    e._browser_auth_data = lambda _: {}
    e._read_schedule_once = lambda p, s, n, **k: (e._choose_priority_group(payload,s,n),payload,200)
    return e


def test_conflict_refresh_skips_newly_sold_second_group(monkeypatch):
    a,b=schedule('1100','2'),schedule('1430','3')
    initial=seats('H21','H22','F21','F22','I21','I22')
    e=engine_for(a,b,initial)
    e._priority_manual_groups=tuple(CgvSeatGroup(g) for g in [('H21','H22'),('F21','F22'),('I21','I22')])
    reads=[]
    def refresh(*_):
        reads.append('fresh')
        return {'ok':True,'status':200,'data':seats('I21','I22')}
    e._fetch_priority_seat_payload=refresh
    attempts=[]
    def hold(self,p,s,groups,*_):
        attempts.append(groups[0].seats)
        if len(attempts)==1:
            self._last_fast_monitor_exit_reason='seat-conflict'
            return False,False
        return True,False
    monkeypatch.setattr(Parent,'_watch_and_hold_api',hold)
    assert e._watch_and_hold_api(object(),a,e._priority_manual_groups,2,False,{})==(True,False)
    assert attempts==[('H21','H22'),('I21','I22')]
    assert reads==['fresh']


def test_large_auto_pool_cannot_starve_next_preferred_time(monkeypatch):
    a,b=schedule('1100','2'),schedule('1430','3')
    initial=seats(*[f'{r}{i}' for r in ('H','F','I','J') for i in range(1,45)])
    e=engine_for(a,b,initial)
    e._priority_manual_groups=(CgvSeatGroup(('H21','H22','H23','H24')),CgvSeatGroup(('F21','F22','F23','F24')))
    e._priority_auto_mode='comfortable'
    e._fetch_priority_seat_payload=lambda *_:{'ok':True,'status':200,'data':initial}
    attempts=[]
    def hold(self,p,s,groups,*_):
        attempts.append((s['scnsrtTm'],groups[0].seats))
        assert len(attempts)<=6, 'first time monopolized automatic seat search'
        if s['scnsrtTm']=='1430': return True,False
        self._last_fast_monitor_exit_reason='seat-conflict'
        return False,False
    monkeypatch.setattr(Parent,'_watch_and_hold_api',hold)
    assert e._watch_and_hold_api(object(),a,e._priority_manual_groups,4,False,{})==(True,False)
    assert [t for t,g in attempts]==['1100']*5+['1430']
    assert attempts[-1][1]==e._priority_manual_groups[0].seats


def test_refresh_rate_limit_does_not_submit_other_time(monkeypatch):
    a,b=schedule('1100','2'),schedule('1430','3')
    payload=seats('H21','H22')
    e=engine_for(a,b,payload)
    e._priority_manual_groups=(CgvSeatGroup(('H21','H22')),)
    e._fetch_priority_seat_payload=lambda *_:{'ok':False,'status':429}
    attempts=[]
    def hold(self,p,s,*_):
        attempts.append(s['scnsrtTm']);self._last_fast_monitor_exit_reason='seat-conflict'
        return False,False
    monkeypatch.setattr(Parent,'_watch_and_hold_api',hold)
    assert e._watch_and_hold_api(object(),a,e._priority_manual_groups,2,False,{})==(False,True)
    assert attempts==['1100']


def run_monitor(price,hold):
    node=shutil.which('node')
    if not node: pytest.skip('Node needed for executable JS regression')
    calls=[]
    class Page:
        def evaluate(self,script,arg): calls.append((script,arg));return True
    BaseEngine(lambda *_:None)._start_fast_seat_monitor(
        Page(),'https://example.test/seats',(CgvSeatGroup(('H21','H22')),),1,
        initial_payload=seats('H21','H22'),max_conflicts=1,
        direct_hold={'schedule':{},'auth':{},'people':2,'priceUrl':'price','holdUrl':'hold'})
    script,arg=calls[0]
    harness=r'''
const fs=require('fs'), spec=JSON.parse(fs.readFileSync(0,'utf8'));
global.window={};global.document={cookie:''};
global.setInterval=()=>1;global.clearInterval=()=>{};
const requests=[];
global.fetch=async url=>{
 requests.push(url);
 const result=url==='price'?spec.price:spec.hold;
 if(result.throw)throw Error('response lost');
 return {ok:true,status:200,headers:new Headers(),json:async()=>result};
};
(async()=>{
 eval('('+spec.script+')')(spec.arg);
 for(let i=0;i<50;i++)await Promise.resolve();
 const s=window.__pengucroFastSeatMonitor;
 const out={requests,hit:!!s.hit,terminal:s.terminalError,kind:s.failureKind,stage:s.lastFailureStage};
 s.stop();process.stdout.write(JSON.stringify(out));
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});
'''
    result=subprocess.run([node,'-e',harness],input=json.dumps(dict(script=script,arg=arg,price=price,hold=hold)),
                          text=True,capture_output=True,timeout=5,check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('price,hold,expected,requests',[
    ({'statusCode':-9,'statusMessage':'가격 정보 오류'}, {},'price-rejected',['price']),
    ({'throw':True}, {},'price-transport-error',['price']),
    ({'statusCode':0},{'throw':True},'hold-uncertain',['price','hold']),
    ({'statusCode':0},{'statusCode':0,'data':{}},'hold-uncertain',['price','hold']),
    ({'statusCode':0},{'statusCode':0,'data':{'resultCode':7,'resultMessage':'입력값 오류'}},'hold-rejected',['price','hold']),
])
def test_price_failure_and_unknown_hold_are_not_reported_as_seat_conflict(price,hold,expected,requests):
    result=run_monitor(price,hold)
    assert result['terminal']==expected
    assert result['kind']!='seat-conflict'
    assert result['requests']==requests


def test_explicit_seat_conflict_returns_to_priority_controller():
    result=run_monitor({'statusCode':0},{'statusCode':0,'statusMessage':'정상',
        'data':{'resultCode':1,'resultMessage':'이미 선점된 좌석입니다'}})
    assert result['kind']=='seat-conflict'
    assert result['terminal']==''
    assert result['stage']=='hold'


def test_successful_hold_is_not_reissued():
    result=run_monitor({'statusCode':0},{'statusCode':0,'data':{'resultCode':0,'movAtktNo':'test-hold'}})
    assert result['hit']
    assert result['requests']==['price','hold']


def test_automatic_order_resumes_after_later_screening_gets_a_turn(monkeypatch):
    a,b=schedule('1100','2'),schedule('1430','3')
    payload=seats(*[f'I{i}' for i in range(1,45)])
    e=engine_for(a,b,payload)
    e._priority_auto_mode='comfortable'
    e._fetch_priority_seat_payload=lambda *_:{'ok':True,'status':200,'data':payload}
    attempts=[]
    def hold(self,p,s,groups,*_):
        attempts.append((s['scnsrtTm'],groups[0].seats))
        if len(attempts)==7:return True,False
        assert len(attempts)<7
        self._last_fast_monitor_exit_reason='seat-conflict'
        return False,False
    monkeypatch.setattr(Parent,'_watch_and_hold_api',hold)
    assert e._watch_and_hold_api(object(),a,(),4,False,{})==(True,False)
    assert [t for t,g in attempts]==['1100']*3+['1430']*3+['1100']
    assert attempts[-1][1] not in [g for t,g in attempts[:3]]
