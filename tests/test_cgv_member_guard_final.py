"""Final registered CGV auth gate; local fake responses, never live bookings."""
from types import SimpleNamespace

import pytest

from engines.cgv_engine_runtime import CgvEngine as Runtime, _MEMBER_SESSION_GUARD_ACTIVE
from engines.registry import EngineRegistry
from test_cgv_preopen_v681 import node_run


@pytest.fixture
def guarded(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("engines.cgv_engine_runtime.time.monotonic", lambda: now[0])
    token = _MEMBER_SESSION_GUARD_ACTIVE.set(True)
    logs, reads, waves, cancellations = [], [], [], []
    engine = EngineRegistry.create(site_name="CGV", mode="", payload={}, custom_sites={},
        log_callback=lambda *args: logs.append(args), success_callback=None)
    page = SimpleNamespace(url="https://cgv.co.kr/cnm/movieBook", context=object())
    state = {"version": 2, "requestId": 1, "completedId": 1, "valid": True,
             "unauthorized": False, "inFlight": False, "completedAgeMs": 0}
    engine._install_member_session_guard = lambda p, **kw: reads.append(kw) or dict(state)
    engine._cancel_member_session_probe = lambda p, **kw: cancellations.append(kw)
    engine._run_schedule_race_once = lambda *args: waves.append(args) or {
        "ok": True, "status": 200, "data": {"statusCode": 0, "data": []}}
    engine._recover_member_session = lambda *args: False
    try:
        yield engine, page, state, now, reads, waves, cancellations, logs
    finally:
        _MEMBER_SESSION_GUARD_ACTIVE.reset(token)


def race(engine, page):
    return engine._race_schedule(page, "https://cgv.co.kr/local-test-only", 2)


def test_final_public_200_cannot_hide_member_expiry(guarded):
    e, p, state, _, reads, waves, *_ = guarded
    state["unauthorized"] = True
    result = race(e, p)
    assert result["status"] == 401 and result["error"] == "member-session-expired"
    assert reads and not waves


def test_final_recovers_after_eighteen_hours_before_next_schedule(guarded):
    e, p, state, now, _, waves, *_ = guarded
    for _ in range(18 * 60):
        assert race(e, p)["ok"]
        now[0] += 60
        state["completedId"] += 1
    state["unauthorized"] = True
    events = []
    def recover(page, context):
        events.append("official-login")
        state.update(unauthorized=False, valid=True)
        e._mark_member_session_confirmed(page)
        return True
    e._recover_member_session = recover
    before = len(waves)
    assert race(e, p)["ok"]
    assert events == ["official-login"] and len(waves) == before + 1


def test_auth_gate_keeps_low_frequency_reads_and_minute_launches(guarded):
    e, p, _, now, reads, waves, *_ = guarded
    for second in range(61):
        now[0] = 1000 + second
        assert race(e, p)["ok"]
    assert len(waves) == 61
    assert len(reads) == 13  # Local CDP snapshots, not 13 HTTP probes.
    assert sum(item["start"] for item in reads) == 2


def test_hung_probe_is_cancelled_without_immediate_retry(guarded):
    e, p, state, now, reads, _, cancellations, _ = guarded
    state.update(completedId=0, valid=False, inFlight=True)
    assert race(e, p)["ok"]
    now[0] += 5
    assert race(e, p)["ok"]
    assert cancellations == [{}]
    state["inFlight"] = False
    now[0] += 5
    assert race(e, p)["ok"]
    assert [item["start"] for item in reads] == [True, False, False]
    now[0] = 1060
    assert race(e, p)["ok"]
    assert reads[-1]["start"] is True


@pytest.mark.parametrize("invalid", [
    {}, {"version": 2, "completedId": "1", "valid": True, "unauthorized": False},
    {"version": 2, "completedId": 1, "valid": "true", "unauthorized": False},
    {"version": 99, "completedId": 1, "valid": True, "unauthorized": False},
])
def test_invalid_or_stale_member_proof_blocks_then_fresh_proof_recovers(guarded, invalid):
    e, p, state, now, _, waves, *_ = guarded
    state.clear()
    state.update(invalid)
    assert race(e, p)["ok"]  # Startup/login proof has a bounded grace.
    now[0] += 95
    before = len(waves)
    assert race(e, p)["error"] == "member-session-probe-stale"
    assert len(waves) == before
    state.update(version=2, completedId=2, valid=True, unauthorized=False, inFlight=False,
                 completedAgeMs=0)
    now[0] += 5
    assert race(e, p)["ok"] and len(waves) == before + 1


def test_same_completed_response_never_refreshes_host_proof_age(guarded):
    e, p, _, now, *_ = guarded
    assert race(e, p)["ok"]
    for _ in range(17):
        now[0] += 5
        assert race(e, p)["ok"]
    now[0] += 5
    assert race(e, p)["error"] == "member-session-probe-stale"


def test_unseen_old_completion_after_long_pause_is_not_new_auth_proof(guarded):
    e, p, state, now, _, waves, *_ = guarded
    e._mark_member_session_confirmed(p)
    now[0] += 18 * 3600
    state.update(completedAgeMs=18 * 3600 * 1000, inFlight=True, requestId=2,
                 startedAgeMs=0)
    assert race(e, p)["error"] == "member-session-probe-stale"
    assert not waves


def test_completed_age_is_accounted_for_not_reset_on_collection(guarded):
    e, p, state, now, *_ = guarded
    e._mark_member_session_confirmed(p)
    now[0] += 100
    state["completedAgeMs"] = 80_000
    assert race(e, p)["ok"]
    now[0] += 10
    assert race(e, p)["error"] == "member-session-probe-stale"


def test_page_change_does_not_renew_stale_auth_proof(guarded):
    e, p, state, now, *_ = guarded
    assert race(e, p)["ok"]
    now[0] += 95
    state.clear()
    new_page = SimpleNamespace(url=p.url, context=p.context)
    assert race(e, new_page)["error"] == "member-session-probe-stale"


def test_host_pause_discards_probe_even_when_browser_clock_did_not_advance(guarded):
    e, p, state, now, _, waves, cancellations, _ = guarded
    assert race(e, p)["ok"]
    now[0] += 18 * 3600
    # The response was not collected before sleep, and performance.now froze.
    state.update(completedId=2, completedAgeMs=0)
    def cancel(page, **kwargs):
        cancellations.append(kwargs)
        if kwargs.get("dispose"):
            state.update(completedId=0, completedAgeMs=None, valid=False, inFlight=True)
    e._cancel_member_session_probe = cancel
    before = len(waves)
    assert race(e, p)["error"] == "member-session-probe-stale"
    assert cancellations == [{"dispose": True}] and len(waves) == before


def test_already_stuck_request_is_cancelled_on_first_snapshot(guarded):
    e, p, state, _, _, _, cancellations, _ = guarded
    state.update(inFlight=True, startedAgeMs=20_000)
    assert race(e, p)["ok"]
    assert cancellations == [{}]


def test_nonmember_schedule_never_starts_auth_probe(guarded):
    e, p, _, _, reads, waves, *_ = guarded
    token = _MEMBER_SESSION_GUARD_ACTIVE.set(False)
    try:
        assert race(e, p)["ok"] and waves and not reads
    finally:
        _MEMBER_SESSION_GUARD_ACTIVE.reset(token)


@pytest.mark.parametrize("during_recovery", [False, True])
def test_stop_does_not_launch_schedule_or_repeat_login(guarded, during_recovery):
    e, p, state, _, _, waves, cancellations, _ = guarded
    if during_recovery:
        state["unauthorized"] = True
        def recover(*args):
            e.stop_event.set()
            return False
        e._recover_member_session = recover
    else:
        e.stop_event.set()
    assert race(e, p)["error"] == "stopped"
    assert not waves
    assert not during_recovery or race(e, p)["error"] == "stopped"
    assert cancellations


def test_failed_recovery_has_cooldown_not_every_schedule(guarded):
    e, p, state, now, *_ = guarded
    state["unauthorized"] = True
    recoveries = []
    e._recover_member_session = lambda *args: recoveries.append(1) or False
    for seconds in (0, 5, 10, 20, 30):
        now[0] = 1000 + seconds
        assert race(e, p)["status"] == 401
    assert len(recoveries) == 2


def test_stop_during_guard_snapshot_prevents_next_schedule(guarded):
    e, p, state, _, _, waves, cancellations, _ = guarded
    def read(*args, **kwargs):
        e.stop_event.set()
        return dict(state)
    e._install_member_session_guard = read
    assert race(e, p)["error"] == "stopped"
    assert not waves and cancellations == [{"dispose": True}]


def test_final_schedule_401_login_page_reuses_official_recovery(guarded):
    e, p, _, _, _, _, *_ = guarded
    p.reload = lambda **kwargs: setattr(p, "url", "https://cgv.co.kr/mem/login")
    p.wait_for_timeout = lambda *_: None
    replies = iter([{"ok": False, "status": 401},
                    {"ok": True, "status": 200, "data": {"statusCode": 0}}])
    e._run_schedule_race_once = lambda *args: next(replies)
    recoveries = []
    e._recover_member_session = lambda *args: recoveries.append(1) or True
    assert race(e, p)["ok"] and recoveries == [1]


def _browser_scripts():
    captured = {}
    page = SimpleNamespace(evaluate=lambda script, arg: captured.update(script=script, arg=arg) or {})
    Runtime._install_member_session_guard(page)
    probe = dict(captured)
    Runtime._cancel_member_session_probe(page)
    return {"probe": probe["script"], "arg": probe["arg"], "cancel": captured["script"]}


def test_real_probe_js_single_flight_cancel_late_response_and_no_browser_timers():
    result = node_run(_browser_scripts(), r'''
const s=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={}; global.document={cookie:''};
global.setTimeout=global.setInterval=()=>{throw Error('browser timer used')};
let requests=[];
global.fetch=(url,opts)=>new Promise(resolve=>requests.push({opts,resolve}));
const probe=eval('('+s.probe+')'), cancel=eval('('+s.cancel+')');
const first=probe(s.arg); probe(s.arg); cancel(false);
const second=probe(s.arg);
requests[0].resolve({status:401,json:async()=>({statusCode:401})});
(async()=>{
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  const late=probe({...s.arg,start:false});
  requests[1].resolve({status:400,json:async()=>({statusCode:400})});
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  const done=probe({...s.arg,start:false}); cancel(true);
  process.stdout.write(JSON.stringify({requests:requests.length,
    aborted:requests[0].opts.signal.aborted, first:first.inFlight,
    second:second.requestId, lateUnauthorized:late.unauthorized,
    valid:done.valid, inFlight:done.inFlight, removed:!window.__pengucroMemberSessionProbe}));
})();
''')
    assert result == {"requests": 2, "aborted": True, "first": True, "second": 2,
                      "lateUnauthorized": False, "valid": True, "inFlight": False, "removed": True}


@pytest.mark.parametrize("status,data,valid,unauthorized", [
    (200, {"statusCode": 0}, True, False),
    (200, {"data": {"statusCode": -1002}}, False, True),
    (429, {"statusCode": 429}, False, False),
    (503, {"statusCode": 503}, False, False),
    (200, None, False, False),
])
def test_real_probe_js_does_not_call_network_failure_auth_expiry(status, data, valid, unauthorized):
    spec = dict(_browser_scripts(), status=status, data=data)
    result = node_run(spec, r'''
const s=JSON.parse(require('fs').readFileSync(0,'utf8'));
global.window={}; global.document={cookie:''};
global.fetch=async()=>({status:s.status,json:async()=>s.data});
const probe=eval('('+s.probe+')'); probe(s.arg);
(async()=>{
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  const r=probe({...s.arg,start:false});
  process.stdout.write(JSON.stringify({valid:r.valid,unauthorized:r.unauthorized,inFlight:r.inFlight}));
})();
''')
    assert result == {"valid": valid, "unauthorized": unauthorized, "inFlight": False}
