from engines import yescaptcha_client
from engines.yescaptcha_client import POLL_INTERVAL_SECONDS, YesCaptchaClient


class FakeResponse:
    def __init__(self, payload, status_error=None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error

    def json(self):
        return self.payload


def test_create_task_sends_integer_soft_id(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return FakeResponse({"errorId": 0, "taskId": "task-1"})

    monkeypatch.setattr(yescaptcha_client.requests, "post", fake_post)
    client = YesCaptchaClient("client-key", "26273")

    assert client.create_recaptcha_v2_task("https://example.com/form", "site-key") == (
        True,
        "task-1",
        "",
    )
    assert captured["payload"]["clientKey"] == "client-key"
    assert captured["payload"]["softID"] == 26273
    assert isinstance(captured["payload"]["softID"], int)


def test_create_task_omits_invalid_soft_id(monkeypatch):
    captured = {}

    def fake_post(_url, json, timeout):
        del timeout
        captured["payload"] = json
        return FakeResponse({"errorId": 0, "taskId": "task-1"})

    monkeypatch.setattr(yescaptcha_client.requests, "post", fake_post)
    client = YesCaptchaClient("client-key", "not-a-number")

    assert client.create_recaptcha_v2_task("https://example.com/form", "site-key")[0] is True
    assert "softID" not in captured["payload"]


def test_create_task_rejects_success_without_task_id(monkeypatch):
    monkeypatch.setattr(
        yescaptcha_client.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse({"errorId": 0}),
    )
    ok, task_id, message = YesCaptchaClient("client-key").create_recaptcha_v2_task(
        "https://example.com/form",
        "site-key",
    )
    assert ok is False
    assert task_id == ""
    assert "Task ID" in message


def test_poll_uses_official_three_second_interval(monkeypatch):
    responses = iter(
        [
            FakeResponse({"errorId": 0, "status": "processing"}),
            FakeResponse(
                {
                    "errorId": 0,
                    "status": "ready",
                    "solution": {"gRecaptchaResponse": "token"},
                }
            ),
        ]
    )
    sleeps = []
    monkeypatch.setattr(
        yescaptcha_client.requests,
        "post",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(yescaptcha_client.time, "sleep", sleeps.append)

    assert YesCaptchaClient("client-key").poll_result("task-1") == (
        True,
        "token",
        "",
    )
    assert sleeps == [POLL_INTERVAL_SECONDS]
    assert POLL_INTERVAL_SECONDS == 3.0


def test_blank_network_exception_still_returns_useful_type(monkeypatch):
    monkeypatch.setattr(
        yescaptcha_client.requests,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )

    ok, _balance, message = YesCaptchaClient("client-key").get_balance()

    assert ok is False
    assert "TimeoutError" in message
