"""Execute the shipped JS with a virtual clock/fetch; never contact Naver."""
import asyncio
import json
import shutil
import subprocess

import pytest

from engines.naver_submit import (
    BROWSER_ARMED_SUBMIT_SCRIPT, BROWSER_CANCEL_ARMED_SUBMIT_SCRIPT,
    NaverBrowserSubmitter, NaverArmUncertainError,
)


def run_timer(responses, *, cancel=False, changed_page=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to execute the browser timer offline")
    harness = r'''
const fs = require('fs');
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
let now = 100, nextId = 0;
const timers = new Map(), posts = [];
global.window = {};
global.performance = {timeOrigin: 1000000, now: () => (now += 0.1)};
global.setTimeout = (fn, delay) => { const id = ++nextId; timers.set(id, {fn, at: now + delay}); return id; };
global.clearTimeout = id => timers.delete(id);
global.fetch = async (url, request) => {
    if (!url.includes('opName=submitBooking')) throw Error('unexpected server read');
    posts.push(JSON.parse(request.body)); now += 50;
    const body = spec.responses[Math.min(posts.length - 1, spec.responses.length - 1)];
    return {status: 200, json: async () => body};
};
(async () => {
    const armed = await eval('(' + spec.script + ')')({
        armId: 'test', input: {}, query: 'mutation submitBooking',
        clockOrigin: spec.changed_page ? 999 : 1000000,
        serverOpenAtPerfMs: 500, dueAtPerfMs: 430, leadMs: 70,
        retryLeadMs: 50, targetArrivalBeforeOpenMs: 20,
        timeoutMs: 3000, maxAttempts: 3, retryWindowMs: 500,
        notOpenCodes: ['BizItem is not opened.']
    });
    if (spec.cancel) eval('(' + spec.cancelScript + ')')({armId: 'test'});
    for (let guard = 0; timers.size && guard < 20; guard++) {
        const [id, timer] = [...timers].sort((a,b) => a[1].at - b[1].at)[0];
        timers.delete(id); now = Math.max(now, timer.at); timer.fn();
        for (let i = 0; i < 20; i++) await Promise.resolve();
    }
    const state = window.__pengucroNaverArmedSubmit;
    process.stdout.write(JSON.stringify({armed, posts, status: state?.status, started: state?.startedAt}));
})().catch(error => {process.stderr.write(String(error)); process.exitCode = 1;});
'''
    result = subprocess.run([node, "-e", harness], input=json.dumps({
        "script": BROWSER_ARMED_SUBMIT_SCRIPT,
        "cancelScript": BROWSER_CANCEL_ARMED_SUBMIT_SCRIPT,
        "responses": responses, "cancel": cancel, "changed_page": changed_page,
    }), text=True, capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


SUCCESS = {"data": {"submitBooking": {"bookingId": "test-booking"}}}


@pytest.mark.parametrize("body", [SUCCESS, {"errors": [{"message": "RT47"}]}, {}])
def test_one_mutation_on_success_refusal_or_ambiguous_body(body):
    result = run_timer([body])
    assert len(result["posts"]) == 1
    assert result["status"] == "complete"
    assert result["armed"]["serverOpenAt"] == 500
    assert 430 <= result["started"] < 432


def test_only_explicit_not_open_can_retry():
    result = run_timer([{"errors": [{"message": "BizItem is not opened."}]}, SUCCESS])
    assert len(result["posts"]) == 2


@pytest.mark.parametrize("kwargs", [{"cancel": True}, {"changed_page": True}])
def test_cancel_or_changed_clock_context_never_posts(kwargs):
    assert run_timer([SUCCESS], **kwargs)["posts"] == []


class LostReplyPage:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.arm_id = None
        self.cancel_id = None

    async def evaluate(self, script, argument=None):
        if argument is None:
            return {"now": 1000, "origin": 1000000}
        if script == BROWSER_ARMED_SUBMIT_SCRIPT:
            self.arm_id = argument["armId"]
            raise TimeoutError("reply lost after timer installed")
        self.cancel_id = argument["armId"]
        return self.cancelled


@pytest.mark.parametrize("cancelled", [True, False, {"status": "complete"}])
def test_lost_arm_reply_requires_positive_cancellation_before_fallback(cancelled):
    page = LostReplyPage(cancelled)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(NaverBrowserSubmitter(page).arm_submit_at({}, 4))
    assert isinstance(error.value, NaverArmUncertainError) == (cancelled is not True)
    assert page.arm_id == page.cancel_id


def test_absolute_deadline_survives_slow_bridge_and_wall_clock_changes(monkeypatch):
    class Page:
        request = None
        async def evaluate(self, script, argument=None):
            self.request = argument
            return {"id": argument["armId"]}

    page = Page()
    submitter = NaverBrowserSubmitter(page)
    async def bridge():
        await asyncio.sleep(0.02)
        return -90000, 2.0, 1000000
    submitter._browser_clock_bridge = bridge
    # No dependency on the PC wall clock is allowed in deadline transfer.
    monkeypatch.setattr("engines.naver_submit.time.time", lambda: 9999999999)
    asyncio.run(submitter.arm_submit_at_server_time(
        {}, 4, open_at_epoch=1800000000, open_at_monotonic=100,
        lead_seconds=0.07, retry_lead_seconds=0.05,
        target_arrival_before_open_seconds=0.02, clock_precision_seconds=0.025,
    ))
    assert page.request["serverOpenAtPerfMs"] == 10000
    assert page.request["dueAtPerfMs"] == pytest.approx(9930)
    assert page.request["clockUncertaintyMs"] == 26
