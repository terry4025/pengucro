import asyncio
from types import SimpleNamespace

import pytest
import requests

from engines.doomescape_engine import DoomEscapeEngine, DoomSubmissionUncertain


TARGET = dict(reservationDate="2026-09-07", reservationTime="20:00:00",
              themePK="36", themeLabel="데이투어")


def receipt(state="신청"):
    return (f"<table><tr><th>예약자</th><td>테스트 ({state})</td></tr>"
            "<tr><th>예약번호</th><td>12345</td></tr>"
            "<tr><th>예약일시</th><td>2026-09-07 20:00</td></tr>"
            "<tr><th>테마</th><td>데이투어</td></tr></table>")


@pytest.mark.parametrize("state,expected", [
    ("신청", True), ("입금대기", True), ("환불", False), ("환불완료", False),
    ("취소", False), ("취소신청", False), ("미확인", False), ("신청실패", False)])
def test_receipt_state(state, expected):
    assert DoomEscapeEngine._receipt_evidence(receipt(state), TARGET)["valid"] is expected


@pytest.mark.parametrize("old,new", [("2026-09-07", "2026-09-08"),
                                     ("20:00", "21:00"), ("데이투어", "옵스큐라"),
                                     ("예약번호", "내부코드")])
def test_wrong_identity_or_only_internal_code_fails(old, new):
    body = receipt().replace(old, new) + '<a href="?ck_code=12345">완료</a>'
    assert not DoomEscapeEngine._receipt_evidence(body, TARGET)["valid"]


def test_policy_and_script_do_not_override_receipt():
    body = receipt() + '<p>취소/환불 규정에 동의합니다.</p><script>예약자 테스트 (환불)</script>'
    assert DoomEscapeEngine._receipt_evidence(body, TARGET)["valid"]
    assert not DoomEscapeEngine._receipt_evidence(
        '<script>예약이 완료되었습니다</script><a href="?ck_code=12345">완료</a>', TARGET)["valid"]
    assert not DoomEscapeEngine._receipt_evidence(
        receipt().replace("20:00", "21:00") + "<footer>영업시간 20:00</footer>", TARGET)["valid"]


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def read(self):
        return self.body.encode()


@pytest.mark.parametrize("body,status", [(receipt("환불"), 200),
                                        (receipt(), 500), ("로그인이 필요합니다", 200)])
def test_verification_never_submits_and_fails_closed(body, status):
    engine = DoomEscapeEngine("https://doomescape.com", lambda *a: None)
    urls = []
    def get(url, **kwargs):
        urls.append(url)
        assert kwargs["allow_redirects"] is False
        return Response(body, status)
    session = SimpleNamespace(get=get)
    with pytest.raises(DoomSubmissionUncertain):
        asyncio.run(engine._verify_receipt_async(session, "ORDER", "SECRET", TARGET, {}))
    assert 1 <= len(urls) <= 3
    assert all("go=rev.make.end" in url and "num=ORDER" in url for url in urls)
    assert not engine._success_fired


def test_pending_receipt_is_rechecked_without_reconfirmation():
    engine = DoomEscapeEngine("https://doomescape.com", lambda *a: None)
    replies = iter([Response("처리 중"), Response(receipt())])
    session = SimpleNamespace(get=lambda *a, **k: next(replies))
    assert asyncio.run(engine._verify_receipt_async(session, "ORDER", "SECRET", TARGET, {})) == "12345"


def test_sync_timeout_is_uncertain_not_retryable():
    engine = DoomEscapeEngine("https://doomescape.com", lambda *a: None)
    def get(*a, **k):
        raise requests.Timeout()
    with pytest.raises(DoomSubmissionUncertain):
        engine._verify_receipt_sync(SimpleNamespace(get=get), "ORDER", "SECRET", TARGET)


def test_evidence_log_does_not_disclose_name_phone_or_code():
    logs = []
    engine = DoomEscapeEngine("https://doomescape.com", lambda message, *a: logs.append(message))
    assert engine._require_receipt(receipt()+"010-1234-5678", 200, TARGET, "ORDER") == "12345"
    assert not any(value in " ".join(logs) for value in ("테스트", "010-1234-5678", "12345"))
