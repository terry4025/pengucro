from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tools.site_inspector.models import (
    ActionRecord,
    InspectionResult,
    InspectorConfig,
    NetworkRecord,
    PageState,
)
from tools.site_inspector.report import write_reports
from tools.site_inspector.explorer import SiteInspector, _INLINE_ROUTE_PATTERN, _crawl_route_signature
from tools.site_inspector.security import (
    categorize_endpoint,
    classify_action,
    classify_request,
    sanitize_body,
    sanitize_headers,
    sanitize_html_snapshot,
    sanitize_url,
    same_site,
    site_scope_key,
)


def test_config_requires_http_url(tmp_path: Path):
    with pytest.raises(ValueError, match="http"):
        InspectorConfig("camfit.co.kr", tmp_path).validated()


def test_config_rejects_unbounded_exploration(tmp_path: Path):
    with pytest.raises(ValueError, match="1~100"):
        InspectorConfig("https://example.com", tmp_path, max_pages=101).validated()

    with pytest.raises(ValueError, match="1~300"):
        InspectorConfig("https://example.com", tmp_path, max_states=301).validated()


def test_config_validates_unopened_date_probe_bounds(tmp_path: Path):
    with pytest.raises(ValueError, match="1~730"):
        InspectorConfig(
            "https://example.com", tmp_path, date_probe_offsets_days=(731,)
        ).validated()
    with pytest.raises(ValueError, match="0~30"):
        InspectorConfig("https://example.com", tmp_path, max_date_probes=31).validated()


def test_date_query_formats_are_preserved():
    target = date(2030, 4, 5)
    assert SiteInspector._date_query_value("2026-09-02", target) == "2030-04-05"
    assert SiteInspector._date_query_value("20260902", target) == "20300405"
    assert SiteInspector._date_query_value("2026-09", target) == "2030-04"
    assert SiteInspector._date_query_value("260902", target, "date") == "300405"
    assert SiteInspector._date_query_value("202609", target, "yyyymm") == "203004"
    assert SiteInspector._date_query_value("not-a-date", target) is None


def test_read_post_date_payload_updates_components_without_secrets(tmp_path: Path):
    inspector = SiteInspector(InspectorConfig("https://example.com", tmp_path))
    payload = inspector._mutate_date_payload(
        {
            "sltYear": "2026",
            "sltMonth": "09",
            "sltDay": "02",
            "yyyymm": "202609",
            "reservationDate": "2026-09-02",
            "siteId": "A",
        },
        date(2030, 4, 5),
    )
    assert payload == {
        "sltYear": "2030",
        "sltMonth": "04",
        "sltDay": "05",
        "yyyymm": "203004",
        "reservationDate": "2030-04-05",
        "siteId": "A",
    }
    assert inspector._mutate_date_payload(
        {"date": "2026-09-02", "token": "[REDACTED:abc:len=3]"},
        date(2030, 4, 5),
    ) is None


def test_pending_fetch_response_is_polled_before_page_replacement(tmp_path: Path):
    inspector = SiteInspector(InspectorConfig("https://example.com", tmp_path))
    result = InspectionResult(output_dir=tmp_path, start_url="https://example.com")
    record = NetworkRecord(
        request_key=1,
        method="POST",
        url="https://example.com/api/availability",
        resource_type="fetch",
        risk="read-post",
        category="availability",
        blocked=False,
    )

    class FakePage:
        def __init__(self):
            self.waits = 0

        def is_closed(self):
            return False

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1

    class FakeResponse:
        status = 200
        headers = {"content-type": "application/json"}

        def body(self):
            return b'{"available":true}'

    class FakeFrame:
        def __init__(self, page):
            self.page = page

    class FakeRequest:
        def __init__(self, page):
            self.frame = FakeFrame(page)
            self.calls = 0

        def response(self):
            self.calls += 1
            return FakeResponse() if self.calls >= 2 else None

    page = FakePage()
    request = FakeRequest(page)
    inspector._request_records[1] = record
    inspector._request_objects[1] = request
    result.network.append(record)

    inspector._reconcile_pending_responses(page, result, wait_ms=100)

    assert request.calls == 2
    assert page.waits == 1
    assert record.status == 200
    assert record.response_body == {"available": True}


@pytest.mark.parametrize(
    ("method", "url", "body", "risk", "blocked"),
    [
        ("GET", "https://x.test/api/sites", None, "read", False),
        ("POST", "https://x.test/api/availability/search", "{}", "read-post", False),
        ("POST", "https://x.test/v1/booking/calculate", "{}", "read-post", False),
        (
            "POST",
            "https://www.dpsnnn.com/booking/html_mfe_list.cm",
            "menu_code=abc123",
            "read-post",
            False,
        ),
        (
            "POST",
            "https://www.dpsnnn.com/booking/add_order.cm",
            "prod_idx=123",
            "blocked-mutation",
            True,
        ),
        ("POST", "https://x.test/v1/book", "{}", "blocked-mutation", True),
        (
            "POST",
            "https://x.test/cdn-cgi/challenge-platform/check",
            "{}",
            "safe-infrastructure-post",
            False,
        ),
        (
            "POST",
            "https://x.test/front/plugins/example/boot",
            "{}",
            "safe-infrastructure-post",
            False,
        ),
        (
            "POST",
            "https://x.test/com/notice/selectNoticeProcedure",
            "{}",
            "read-post",
            False,
        ),
        (
            "POST",
            "https://x.test/api/action",
            '{"selected":true}',
            "blocked-unknown-write",
            True,
        ),
        (
            "POST",
            "https://x.test/payment/selectMethod",
            "{}",
            "blocked-mutation",
            True,
        ),
        (
            "POST",
            "https://x.test/web/reservation/selectListReservPlaceAjax.do",
            "{}",
            "read-post",
            False,
        ),
        (
            "POST",
            "https://x.test/web/reservation/selectAndUpdate.do",
            "{}",
            "blocked-mutation",
            True,
        ),
        (
            "POST",
            "https://x.test/web/reservation/PageListReview.do",
            "{}",
            "read-post",
            False,
        ),
        ("DELETE", "https://x.test/api/user", None, "blocked-unknown-write", True),
        ("POST", "https://x.test/graphql", "query Camps { camps { id } }", "read-post", False),
        ("POST", "https://x.test/graphql", "mutation Book { book { id } }", "blocked-mutation", True),
    ],
)
def test_request_firewall(method, url, body, risk, blocked):
    assert classify_request(method, url, body) == (risk, blocked)


def test_final_ui_actions_are_not_clicked():
    assert classify_action("결제하기", "button") == "blocked-final-action"
    assert classify_action("예약 일정 선택하기", "button") == "explorable"
    assert classify_action("날짜 선택", "button") == "explorable"
    assert classify_action("예약하기", "button") == "blocked-final-action"
    assert classify_action("예매", "button") == "blocked-final-action"
    assert classify_action("영화별 예매", "button") == "explorable"
    assert classify_action("예약하기", "button", "fnDetailSvc selectReservView.do") == "explorable"
    assert classify_action("예약하기", "button", "https://example.com/ct/shop/demo") == "explorable"
    assert classify_action("예약하기", "button", "https://example.com/ct/shop/demo submit") == "blocked-final-action"
    assert classify_action("예약하기", "button", "POST rev.act.php") == "blocked-final-action"
    assert classify_action(
        "예약",
        "button",
        "https://www.dpsnnn.com/reserve_g",
        "예약가능 행복 / 22:20",
    ) == "explorable"
    assert classify_action(
        "예약",
        "button",
        "POST /booking/add_order.cm",
        "예약가능 행복 / 22:20",
    ) == "blocked-final-action"
    assert classify_action("뒤로가기", "button") == "skipped-navigation-control"
    assert classify_action("", "button") == "skipped-unlabelled-control"
    assert classify_action("", "link", "javascript:openUnknownDialog()") == "skipped-unlabelled-control"
    assert classify_action("Next slide", "button") == "skipped-navigation-control"
    assert classify_action("2", "button") == "skipped-pagination-control"
    assert classify_action("예약불가 10:40", "button") == "skipped-unavailable-control"
    assert classify_action("오늘은 그만 보기", "button") == "skipped-preference-control"
    assert classify_action("첫번째", "button") == "skipped-carousel-control"
    assert classify_action("닫기", "button") == "explorable"
    assert classify_action("전체메뉴버튼", "button") == "skipped-navigation-control"
    assert classify_action("TOP", "button") == "skipped-navigation-control"
    assert classify_action("popupCheck", "checkbox") == "skipped-preference-control"


def test_sensitive_values_are_redacted_without_losing_shape():
    headers = sanitize_headers({"Authorization": "Bearer secret", "Accept": "application/json"})
    assert headers["Authorization"].startswith("[REDACTED:")
    assert headers["Accept"] == "application/json"

    body = sanitize_body('{"user":{"password":"pw","people":2},"token":"abc"}')
    assert body["user"]["password"].startswith("[REDACTED:")
    assert body["user"]["people"] == 2
    assert body["token"].startswith("[REDACTED:")

    url = sanitize_url("https://x.test/result?token=secret&id=123#fragment")
    assert "secret" not in url
    assert "id=123" in url
    assert "#" not in url

    pii = sanitize_body('{"name":"홍길동","mobile":"01012345678","email":"a@b.test"}')
    assert "홍길동" not in json.dumps(pii, ensure_ascii=False)
    assert "01012345678" not in json.dumps(pii, ensure_ascii=False)
    assert "a@b.test" not in json.dumps(pii, ensure_ascii=False)

    pii_url = sanitize_url("https://x.test/result?mobile=01012345678&name=user&id=123")
    assert "01012345678" not in pii_url
    assert "name=user" not in pii_url
    assert "id=123" in pii_url


def test_html_snapshot_keeps_structure_but_removes_values_and_inline_scripts():
    html = sanitize_html_snapshot(
        '<html><head><script>window.token="secret"</script>'
        '<script src="/app.js"></script></head><body>'
        '<input name="csrfToken" value="abc"><textarea>private</textarea></body></html>'
    )
    assert "secret" not in html
    assert "private" not in html
    assert 'src="/app.js"' in html
    assert 'name="csrfToken"' in html
    assert "abc" not in html


def test_endpoint_categories_cover_reservation_flow():
    assert categorize_endpoint("https://x.test/availability/calendar") == "availability"
    assert categorize_endpoint("https://x.test/v1/booking/calculate") == "calculate"
    assert categorize_endpoint("https://x.test/payments/confirm") == "payment"
    assert categorize_endpoint("https://x.test/auth/refresh") == "auth"
    assert categorize_endpoint("https://x.test/home.php?go=rev.make") == "reservation"
    assert categorize_endpoint("https://x.test/home.php?go=rev.login") == "auth"
    assert categorize_endpoint("https://x.test/home.php?go=theme.list") == "catalog"
    assert categorize_endpoint("https://x.test/home.php?go=rev.main") == "reservation"


def test_report_generates_reviewable_engine_draft(tmp_path: Path):
    output = tmp_path / "capture"
    output.mkdir()
    result = InspectionResult(output_dir=output, start_url="https://example.com/camp/1")
    result.states.append(
        PageState(
            state_id="state-001",
            url="https://example.com/camp/1",
            title="Camp",
            depth=0,
            signature="abc",
            html_file="pages/state-001.html",
            screenshot_file="screenshots/state-001.png",
            interactive_count=3,
        )
    )
    result.actions.append(
        ActionRecord(
            state_id="state-001",
            kind="button",
            label="예약하기",
            selector="#book",
            risk="blocked-final-action",
            outcome="안전 정책에 따라 클릭하지 않음",
        )
    )
    result.network.extend(
        [
            NetworkRecord(
                request_key=1,
                method="GET",
                url="https://example.com/availability/calendar",
                resource_type="fetch",
                risk="read",
                category="availability",
                blocked=False,
                status=200,
            ),
            NetworkRecord(
                request_key=2,
                method="POST",
                url="https://example.com/v1/book",
                resource_type="fetch",
                risk="blocked-mutation",
                category="reservation",
                blocked=True,
                request_body={"siteId": "A"},
            ),
        ]
    )
    result.script_endpoints.append(
        {
            "url": "https://example.com/controller/run_proc.php",
            "category": "other",
            "source_script": "static-html:https://example.com/camp/1",
        }
    )
    result.static_observations.append(
        {
            "state_id": "state-001",
            "url": "https://example.com/camp/1",
            "reason": "표시된 대화형 컨트롤 없음",
            "forms": [],
            "selects": [{"name": "branch", "options": [{"value": "1", "label": "A"}]}],
            "external_scripts": [],
            "endpoint_candidates": ["https://example.com/controller/run_proc.php"],
            "command_candidates": ["get_theme_info_list"],
        }
    )
    result.discovered_routes.append(
        {"url": "https://tickets.example.com/product/123", "depth": 1}
    )

    report = write_reports(result)

    assert report.exists()
    assert (output / "inspection.json").exists()
    assert (output / "endpoints.json").exists()
    assert (output / "engine_spec.json").exists()
    assert (output / "engine_blueprint.json").exists()
    assert (output / "site_structure.json").exists()
    assert (output / "crawl_frontier.json").exists()
    assert (output / "related_origins.json").exists()
    assert (output / "request_schemas.json").exists()
    assert (output / "generated_engine_draft.py").exists()
    spec = json.loads((output / "engine_spec.json").read_text(encoding="utf-8"))
    assert spec["generated_from_observation_only"] is True
    assert spec["endpoint_candidates"]["reservation"][0]["blocked_during_analysis"] is True
    assert any(item["method"] == "UNKNOWN" for item in json.loads(
        (output / "endpoints.json").read_text(encoding="utf-8")
    ))
    assert "정적 보조 분석" in report.read_text(encoding="utf-8")
    structure = json.loads((output / "site_structure.json").read_text(encoding="utf-8"))
    assert structure["coverage"]["captured_states"] == 1
    assert structure["coverage"]["discovered_routes"] >= 2
    blueprint = json.loads((output / "engine_blueprint.json").read_text(encoding="utf-8"))
    assert blueprint["generated_from_observation_only"] is True
    frontier = json.loads((output / "crawl_frontier.json").read_text(encoding="utf-8"))
    assert any(row["origin"] == "https://tickets.example.com" for row in frontier["routes"])
    assert "실제 예약 제출은 아직 구현되지 않았습니다" in (
        output / "generated_engine_draft.py"
    ).read_text(encoding="utf-8")


def test_report_keeps_server_route_query_but_masks_dynamic_values(tmp_path: Path):
    output = tmp_path / "capture"
    output.mkdir()
    result = InspectionResult(output_dir=output, start_url="https://example.com/home.php")
    result.network.append(
        NetworkRecord(
            request_key=1,
            method="GET",
            url="https://example.com/home.php?go=rev.make&rev_days=2030-01-01&s_zizum=1",
            resource_type="document",
            risk="read",
            category="other",
            blocked=False,
            status=200,
        )
    )

    write_reports(result)

    endpoints = json.loads((output / "endpoints.json").read_text(encoding="utf-8"))
    assert endpoints[0]["path"] == (
        "/home.php?go=rev.make&rev_days={value}&s_zizum={value}"
    )
    assert "2030-01-01" not in json.dumps(endpoints, ensure_ascii=False)


def test_report_excludes_static_assets_from_site_routes(tmp_path: Path):
    output = tmp_path / "capture"
    output.mkdir()
    result = InspectionResult(output_dir=output, start_url="https://example.com/start")
    result.network.extend(
        [
            NetworkRecord(
                request_key=1,
                method="GET",
                url="https://example.com/assets/app.css",
                resource_type="stylesheet",
                risk="read",
                category="other",
                blocked=False,
                status=200,
            ),
            NetworkRecord(
                request_key=2,
                method="GET",
                url="https://example.com/reservation/calendar",
                resource_type="fetch",
                risk="read",
                category="availability",
                blocked=False,
                status=200,
            ),
        ]
    )

    write_reports(result)

    structure = json.loads((output / "site_structure.json").read_text(encoding="utf-8"))
    paths = [row["path"] for row in structure["routes"]]
    assert "/assets/app.css" not in paths
    assert "/reservation/calendar" in paths


def test_unsafe_get_actions_are_blocked():
    risk, blocked = classify_request("GET", "https://example.com/member/logout.do")
    assert blocked is True
    assert risk == "blocked-unsafe-get"

    risk, blocked = classify_request(
        "GET", "https://example.com/assets/type=refund.svg"
    )
    assert (risk, blocked) == ("read", False)

    risk, blocked = classify_request(
        "GET", "https://example.com/assets/loadShare_remove_scroll.js"
    )
    assert (risk, blocked) == ("read", False)


def test_inline_route_pattern_finds_legacy_relative_handlers():
    source = "const check = 'selectServiceCheck.do?mode=list';"
    matches = [match.group(1) for match in _INLINE_ROUTE_PATTERN.finditer(source)]
    assert matches == ["selectServiceCheck.do?mode=list"]


def test_same_site_scope_supports_related_subdomains():
    assert site_scope_key("https://nol.interpark.com") == "interpark.com"
    assert site_scope_key("https://app.catchtable.co.kr") == "catchtable.co.kr"
    assert same_site("https://nol.interpark.com", "https://tickets.interpark.com")
    assert not same_site("https://nol.interpark.com", "https://nol.com")


def test_request_firewall_ignores_pages_not_owned_by_the_run(tmp_path: Path):
    inspector = SiteInspector(InspectorConfig("https://example.com", tmp_path))
    result = InspectionResult(output_dir=tmp_path, start_url="https://example.com")

    class ForeignRequest:
        frame = type("Frame", (), {"page": object()})()

    class ForeignRoute:
        request = ForeignRequest()
        continued = False

        def continue_(self):
            self.continued = True

    route = ForeignRoute()
    inspector._route_request(route, result)

    assert route.continued is True
    assert result.network == []


@pytest.mark.parametrize(
    ("hint", "input_type", "expected"),
    [
        ("2026-09-02", "text", "2030-01-03"),
        ("YYYY.MM.DD", "text", "2030.01.03"),
        ("YYYY/MM/DD", "text", "2030/01/03"),
        ("20260902", "text", "20300103"),
        ("", "date", "2030-01-03"),
    ],
)
def test_browser_date_value_formats(hint: str, input_type: str, expected: str):
    assert SiteInspector._format_browser_date_value(date(2030, 1, 3), hint, input_type) == expected


def test_date_range_roles_cover_travel_query_typo():
    assert SiteInspector._date_key_role("departureDateForm") == "start"
    assert SiteInspector._date_key_role("departureDateTo") == "end"
    assert SiteInspector._parse_date_value("20260902") == date(2026, 9, 2)


def test_telemetry_endpoint_is_separated_from_engine_candidates():
    assert categorize_endpoint("https://sentry.example/api/1/envelope/") == "telemetry"


def test_high_entropy_encrypted_values_are_redacted():
    secret = "A" * 128 + "/+=="
    sanitized = sanitize_body(
        json.dumps({"encryptedParamString": secret, "description": "normal"})
    )
    assert sanitized["encryptedParamString"].startswith("[REDACTED:")
    assert sanitized["description"] == "normal"


def test_crawl_signature_collapses_query_values_but_keeps_route_selectors():
    first = _crawl_route_signature("https://example.com/map?keyword=steak&go=rev.make")
    second = _crawl_route_signature("https://example.com/map?keyword=pasta&go=rev.make")
    different_route = _crawl_route_signature("https://example.com/map?keyword=pasta&go=rev.login")
    assert first == second
    assert first != different_route


def test_display_post_is_read_only_unless_handler_is_mutating():
    assert classify_request("POST", "https://example.com/api/display/v2/main/banners") == (
        "read-post",
        False,
    )
    assert classify_request("POST", "https://example.com/api/display/v2/main/update") == (
        "blocked-unknown-write",
        True,
    )
    assert classify_request(
        "POST", "https://example.com/api/reservation/v1/dining/time-slots"
    ) == ("read-post", False)
    assert classify_request(
        "POST", "https://example.com/api/reservation/v1/dining/create"
    ) == ("blocked-mutation", True)
