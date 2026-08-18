from __future__ import annotations

from datetime import datetime, timedelta

from engines.cgv_browser_client import CgvBrowserClient


class _ScheduleClient(CgvBrowserClient):
    def __init__(self, schedules):
        super().__init__()
        self.schedules = schedules
        self.requested_dates = []

    def _with_page(self, operation):
        return operation(object())

    def _fetch_schedule_on_page(self, _page, _site_no, date_digits):
        self.requested_dates.append(date_digits)
        return tuple(self.schedules.get(date_digits, ()))


def test_target_date_is_probed_before_historical_references():
    target = datetime.now().date() + timedelta(days=5)
    target_digits = target.strftime("%Y%m%d")
    reference = target - timedelta(days=1)
    reference_digits = reference.strftime("%Y%m%d")

    real_target = {
        "siteNo": "0013",
        "scnYmd": target_digits,
        "scnsNo": "target-real",
        "scnSseq": "1",
        "scnsrtTm": "1930",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
    }
    historical = {
        "siteNo": "0013",
        "scnYmd": reference_digits,
        "scnsNo": "reference",
        "scnSseq": "1",
        "scnsrtTm": "1900",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
    }
    client = _ScheduleClient(
        {
            target_digits: (real_target,),
            reference_digits: (historical,),
        }
    )

    schedules, reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert client.requested_dates[0] == target_digits
    assert reference_only is False
    assert reference_date == target.isoformat()
    assert schedules[0]["scnYmd"] == target_digits
    assert schedules[0]["scnsNo"] == "target-real"


def test_empty_target_date_falls_back_to_reference_templates_only_after_probe():
    target = datetime.now().date() + timedelta(days=5)
    target_digits = target.strftime("%Y%m%d")
    reference = target - timedelta(days=1)
    reference_digits = reference.strftime("%Y%m%d")
    historical = {
        "siteNo": "0013",
        "scnYmd": reference_digits,
        "scnsNo": "reference",
        "scnSseq": "1",
        "scnsrtTm": "1900",
        "expoProdNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
    }
    client = _ScheduleClient({reference_digits: (historical,)})

    schedules, _reference_date, reference_only = client.fetch_schedule_with_reference(
        "0013", target.isoformat()
    )

    assert client.requested_dates[0] == target_digits
    assert reference_only is True
    assert schedules[0]["_pengucroPreopen"] is True
    assert schedules[0]["scnYmd"] == target_digits
