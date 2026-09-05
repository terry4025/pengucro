from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from .models import InspectionResult
from .security import same_site, site_scope_key


_ROUTE_QUERY_KEYS = {"go", "action", "mode", "route", "cmd", "op", "view", "type"}
_STATIC_ASSET_SUFFIXES = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip",
    ".mp4", ".mp3",
)


def _endpoint_signature(url: str) -> str:
    """Preserve server-rendered route selectors without leaking user values."""
    parsed = urlsplit(url)
    path = parsed.path or "/"
    if not parsed.query:
        return path
    parts = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        safe_key = quote(key, safe="._-")
        safe_value = quote(value, safe="._-") if key.lower() in _ROUTE_QUERY_KEYS else "{value}"
        parts.append(f"{safe_key}={safe_value}")
    return f"{path}?{'&'.join(parts)}" if parts else path


def _json_dump(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _value_schema(value, *, depth: int = 0):
    if depth > 8:
        return {"type": "max-depth"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {
                str(key): _value_schema(item, depth=depth + 1)
                for key, item in list(value.items())[:200]
            },
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": _value_schema(value[0], depth=depth + 1)
            if value
            else {"type": "unknown"},
        }
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    return {"type": "string"}


def _request_schemas(result: InspectionResult) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for item in result.network:
        if item.resource_type not in {"xhr", "fetch", "document"}:
            continue
        key = (item.method, _endpoint_signature(item.url))
        row = grouped.setdefault(
            key,
            {
                "method": item.method,
                "path": key[1],
                "category": item.category,
                "risk": item.risk,
                "blocked": item.blocked,
                "statuses": set(),
                "request_schema": None,
                "response_schema": None,
                "content_types": set(),
            },
        )
        row["blocked"] = bool(row["blocked"] or item.blocked)
        if item.status is not None:
            row["statuses"].add(item.status)
        if item.request_body is not None and row["request_schema"] is None:
            row["request_schema"] = _value_schema(item.request_body)
        if item.response_body is not None and row["response_schema"] is None:
            row["response_schema"] = _value_schema(item.response_body)
        content_type = item.response_headers.get("content-type", "")
        if content_type:
            row["content_types"].add(content_type.split(";", 1)[0].strip())
    rows = []
    for row in grouped.values():
        row["statuses"] = sorted(row["statuses"])
        row["content_types"] = sorted(row["content_types"])
        rows.append(row)
    return sorted(rows, key=lambda row: (row["category"], row["path"], row["method"]))


def _semantic_selector_groups(result: InspectionResult) -> dict[str, list[dict]]:
    terms = {
        "branch": ("branch", "zizum", "region", "location", "지점", "지역", "장소"),
        "theme_or_product": ("theme", "product", "service", "movie", "camp", "테마", "상품", "서비스", "영화"),
        "date": ("date", "day", "month", "calendar", "rev_days", "날짜", "일자", "달력"),
        "time_or_slot": ("time", "slot", "round", "회차", "시간"),
        "people": ("people", "person", "adult", "teen", "child", "인원", "성인", "아동"),
        "submit": ("예약하기", "예매하기", "결제하기", "book now", "reserve now", "submit"),
        "result_or_code": ("result", "status", "code", "number", "완료", "결과", "예약번호"),
    }
    groups: dict[str, list[dict]] = {name: [] for name in terms}
    seen: dict[str, set[tuple[str, str]]] = {name: set() for name in terms}
    for inventory in result.dom_inventory:
        state_id = str(inventory.get("state_id", ""))
        for control in inventory.get("controls", []):
            haystack = " ".join(
                str(control.get(key, ""))
                for key in ("name", "id", "label", "type", "role")
            ).lower()
            for group, needles in terms.items():
                if not any(term.lower() in haystack for term in needles):
                    continue
                key = (state_id, str(control.get("selector", "")))
                if key in seen[group]:
                    continue
                seen[group].add(key)
                groups[group].append(
                    {
                        "state_id": state_id,
                        "selector": control.get("selector", ""),
                        "tag": control.get("tag", ""),
                        "type": control.get("type", ""),
                        "name": control.get("name", ""),
                        "label": control.get("label", ""),
                    }
                )
    return groups


def _site_structure(result: InspectionResult, endpoints: list[dict]) -> dict:
    route_rows: dict[str, dict] = {}
    start = urlsplit(result.start_url)
    start_origin = (start.scheme.lower(), start.netloc.lower())

    def add_route(url: str, source: str, *, visited: bool = False, method: str = "GET") -> None:
        if not url:
            return
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        if parsed.path.lower().endswith(_STATIC_ASSET_SUFFIXES):
            return
        signature = f"{parsed.scheme}://{parsed.netloc}{_endpoint_signature(url)}"
        row = route_rows.setdefault(
            signature,
            {
                "route": signature,
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "path": _endpoint_signature(url),
                "methods": set(),
                "sources": set(),
                "visited": False,
                "same_origin": (parsed.scheme.lower(), parsed.netloc.lower()) == start_origin,
                "same_site": same_site(result.start_url, url),
            },
        )
        row["methods"].add(method)
        row["sources"].add(source)
        row["visited"] = bool(row["visited"] or visited)

    for state in result.states:
        add_route(state.url, "page-state", visited=True)
    for item in result.discovered_routes:
        add_route(str(item.get("url", "")), "crawl-discovery")
    for inventory in result.dom_inventory:
        for value in inventory.get("route_targets", []):
            add_route(str(value), f"dom:{inventory.get('state_id', '')}")
        for form in inventory.get("forms", []):
            add_route(
                str(form.get("action", "")),
                f"form:{inventory.get('state_id', '')}",
                method=str(form.get("method", "GET")),
            )
        for frame in inventory.get("iframes", []):
            add_route(str(frame.get("src", "")), f"iframe:{inventory.get('state_id', '')}")
    for item in result.network:
        add_route(item.url, "network", method=item.method)
    for item in result.script_endpoints:
        add_route(str(item.get("url", "")), "script")

    routes = []
    for row in route_rows.values():
        row["methods"] = sorted(row["methods"])
        row["sources"] = sorted(row["sources"])
        routes.append(row)
    routes.sort(key=lambda row: (row["origin"], row["path"]))
    transitions = [
        {
            "from_state": action.state_id,
            "action_kind": action.kind,
            "action_label": action.label,
            "selector": action.selector,
            "outcome": action.outcome,
            "to_state": action.resulting_state_id,
            "resulting_url": action.resulting_url,
        }
        for action in result.actions
    ]
    return {
        "start_url": result.start_url,
        "coverage": {
            "captured_states": len(result.states),
            "discovered_routes": len(routes),
            "visited_routes": sum(1 for row in routes if row["visited"]),
            "unvisited_routes": sum(1 for row in routes if not row["visited"]),
            "dom_inventories": len(result.dom_inventory),
            "date_probes": len(result.date_probes),
            "bounded_exploration": True,
        },
        "routes": routes,
        "states": [
            {
                "state_id": state.state_id,
                "url": state.url,
                "title": state.title,
                "depth": state.depth,
                "interactive_count": state.interactive_count,
                "html_file": state.html_file,
                "screenshot_file": state.screenshot_file,
            }
            for state in result.states
        ],
        "transitions": transitions,
        "dom_inventory": result.dom_inventory,
        "selector_candidates": _semantic_selector_groups(result),
        "date_probes": result.date_probes,
        "endpoint_count": len(endpoints),
    }


def _endpoint_rows(result: InspectionResult) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for item in result.network:
        if item.category == "telemetry":
            continue
        if item.resource_type not in {"xhr", "fetch", "document", "websocket"}:
            continue
        suffix = urlsplit(item.url).path.lower()
        if suffix.endswith(
            (
                ".css",
                ".js",
                ".mjs",
                ".map",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".svg",
                ".webp",
                ".ico",
                ".woff",
                ".woff2",
                ".ttf",
                ".mp4",
                ".mp3",
            )
        ):
            continue
        path = _endpoint_signature(item.url)
        key = (item.method, path)
        row = grouped.setdefault(
            key,
            {
                "method": item.method,
                "path": path,
                "category": item.category,
                "risk": item.risk,
                "blocked": item.blocked,
                "statuses": set(),
                "observations": 0,
                "request_body_example": item.request_body,
                "response_body_example": item.response_body,
            },
        )
        row["observations"] += 1
        row["blocked"] = bool(row["blocked"] or item.blocked)
        if item.status is not None:
            row["statuses"].add(item.status)
        if row["request_body_example"] is None and item.request_body is not None:
            row["request_body_example"] = item.request_body
        if row["response_body_example"] is None and item.response_body is not None:
            row["response_body_example"] = item.response_body
    for item in result.script_endpoints:
        url = str(item.get("url", ""))
        if not url:
            continue
        if urlsplit(url).path.lower().endswith(
            (".css", ".js", ".mjs", ".map", ".png", ".jpg", ".svg", ".webp", ".woff", ".woff2")
        ):
            continue
        path = _endpoint_signature(url)
        key = ("UNKNOWN", path)
        grouped.setdefault(
            key,
            {
                "method": "UNKNOWN",
                "path": path,
                "category": item.get("category", "other"),
                "risk": "observation-only",
                "blocked": False,
                "statuses": set(),
                "observations": 0,
                "request_body_example": None,
                "response_body_example": None,
                "source": item.get("source_script", "script"),
            },
        )
    rows = []
    for row in grouped.values():
        row["statuses"] = sorted(row["statuses"])
        rows.append(row)
    return sorted(rows, key=lambda row: (row["category"], row["path"], row["method"]))


def _write_engine_draft(
    output_dir: Path,
    result: InspectionResult,
    endpoints: list[dict],
    structure: dict,
    request_schemas: list[dict],
) -> None:
    candidates: dict[str, list[dict]] = defaultdict(list)
    for item in endpoints:
        candidates[item["category"]].append(
            {
                "method": item["method"],
                "path": item["path"],
                "blocked_during_analysis": item["blocked"],
            }
        )
    spec = {
        "site": urlsplit(result.start_url).netloc,
        "start_url": result.start_url,
        "generated_from_observation_only": True,
        "standard_pengucro_payload": [
            "reservationDate",
            "reservationTime",
            "themePK",
            "people",
        ],
        "site_specific_fields_to_model": [
            "checkInDate",
            "checkoutDate",
            "zoneId",
            "siteId",
            "numOfAdults",
            "numOfTeens",
            "numOfChildren",
            "numOfCars",
            "pets",
            "services",
        ],
        "endpoint_candidates": dict(candidates),
        "selector_candidates": structure["selector_candidates"],
        "state_transitions": structure["transitions"],
        "request_schema_file": "request_schemas.json",
        "site_structure_file": "site_structure.json",
        "date_probe_summary": [
            {
                "kind": item.get("kind"),
                "target_date": item.get("target_date"),
                "status": item.get("status"),
                "structural_signature": item.get("structural_signature"),
                "slot_like_control_count": item.get("slot_like_control_count"),
                "html_file": item.get("html_file"),
            }
            for item in result.date_probes
        ],
        "required_manual_review": [
            "최종 예약 요청의 성공 판정 필드",
            "불명확 응답 뒤 예약번호 복구 경로",
            "로그인·토큰 갱신 방식",
            "결제 준비와 결제 확정 경계",
        ],
    }
    _json_dump(output_dir / "engine_spec.json", spec)

    read_candidates = [
        item
        for item in endpoints
        if not item["blocked"]
        and item["category"] in {"availability", "reservation", "catalog", "calculate"}
    ]
    submit_candidates = [
        item
        for item in endpoints
        if item["blocked"] or item["category"] in {"payment", "result"}
    ]
    blueprint = {
        "generated_from_observation_only": True,
        "site": spec["site"],
        "start_url": result.start_url,
        "coverage": structure["coverage"],
        "catalog_and_availability_candidates": read_candidates,
        "submission_and_result_candidates": submit_candidates,
        "selector_candidates": structure["selector_candidates"],
        "request_schemas": request_schemas,
        "date_probes": result.date_probes,
        "implementation_order": [
            "카탈로그 및 지점·상품 식별자 파싱",
            "미오픈 날짜를 포함한 가용시간 조회",
            "로그인·CSRF·세션 갱신",
            "최종 제출 직전까지의 준비 흐름",
            "단일 최종 제출과 성공 판정",
            "불명확 응답의 예약번호 복구",
        ],
        "not_proven": [
            "실제 예약 성공",
            "최종 제출 원자성",
            "예약번호 또는 주문번호 복구",
            "결제 준비와 결제 확정 경계",
        ],
    }
    _json_dump(output_dir / "engine_blueprint.json", blueprint)

    source = f'''"""자동 분석 결과로 만든 검토용 엔진 골격입니다.

실제 예약 제출 코드는 의도적으로 비어 있습니다. report.md와 engine_spec.json을
검토하고 성공 판정·중복 방지·결과 복구 테스트를 추가한 뒤에만 연결하세요.
"""

from engines.base_engine import BaseEngine


class GeneratedSiteEngine(BaseEngine):
    SITE_URL = {result.start_url!r}

    def discover_catalog(self):
        """site_structure.json의 지점·상품 선택자와 catalog API를 연결하세요."""
        raise NotImplementedError("카탈로그 파서를 구현해야 합니다.")

    def fetch_availability(self, reservation_data):
        """미오픈 날짜 probe와 request_schemas.json을 기준으로 구현하세요."""
        raise NotImplementedError("예약 가능 시간 조회를 구현해야 합니다.")

    def prepare_submission(self, reservation_data):
        """인증·CSRF와 제출 직전 payload만 준비하고 최종 POST는 보내지 않습니다."""
        raise NotImplementedError("제출 준비 흐름을 구현해야 합니다.")

    def submit_once(self, prepared):
        """검증 후 단 한 번만 호출할 최종 제출 경계입니다."""
        raise NotImplementedError("실제 예약 제출은 아직 구현되지 않았습니다.")

    def reconcile_result(self, prepared):
        """불명확 응답 뒤 본인 예약내역에서 예약번호를 복구하세요."""
        raise NotImplementedError("예약 결과 복구를 구현해야 합니다.")

    def make_reservation_thread(self, reservation_data):
        self.log("[분석 초안] 실제 예약 제출은 아직 구현되지 않았습니다.", "warning")
        self.is_running = False
'''
    (output_dir / "generated_engine_draft.py").write_text(source, encoding="utf-8")


def write_reports(result: InspectionResult) -> Path:
    output_dir = result.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    _json_dump(output_dir / "inspection.json", result.as_dict())
    endpoints = _endpoint_rows(result)
    _json_dump(output_dir / "endpoints.json", endpoints)
    request_schemas = _request_schemas(result)
    _json_dump(output_dir / "request_schemas.json", request_schemas)
    structure = _site_structure(result, endpoints)
    _json_dump(output_dir / "site_structure.json", structure)
    origins: dict[str, dict] = {}
    for row in structure["routes"]:
        origin = row["origin"]
        item = origins.setdefault(
            origin,
            {
                "origin": origin,
                "site_scope_key": site_scope_key(origin),
                "same_origin": row["same_origin"],
                "same_site": row["same_site"],
                "route_count": 0,
                "visited_route_count": 0,
            },
        )
        item["route_count"] += 1
        item["visited_route_count"] += int(bool(row["visited"]))
    _json_dump(
        output_dir / "related_origins.json",
        {
            "start_site_scope_key": site_scope_key(result.start_url),
            "origins": sorted(origins.values(), key=lambda item: (-item["route_count"], item["origin"])),
        },
    )
    frontier = [
        row
        for row in structure["routes"]
        if row["same_site"] and not row["visited"] and "GET" in row["methods"]
    ]
    _json_dump(
        output_dir / "crawl_frontier.json",
        {
            "description": "설정 한도 때문에 아직 방문하지 않은 동일 사이트 GET 경로",
            "routes": frontier,
        },
    )
    _write_engine_draft(output_dir, result, endpoints, structure, request_schemas)

    categories = Counter(item["category"] for item in endpoints)
    lines = [
        "# 사이트 자동 분석 보고서",
        "",
        f"- 시작 URL: `{result.start_url}`",
        f"- 화면 상태: {len(result.states)}개",
        f"- 자동 동작: {len(result.actions)}개",
        f"- 네트워크 관측: {len(result.network)}개",
        f"- API 후보: {len(endpoints)}개",
        f"- 정적 보조 분석: {len(result.static_observations)}개",
        f"- DOM 구조 인벤토리: {len(result.dom_inventory)}개",
        f"- 미오픈 날짜 탐색: {len(result.date_probes)}개",
        f"- 발견 경로: {structure['coverage']['discovered_routes']}개 "
        f"(방문 {structure['coverage']['visited_routes']} / 미방문 {structure['coverage']['unvisited_routes']})",
        f"- 차단된 쓰기 요청: {sum(1 for item in result.network if item.blocked)}개",
        "",
        "## API 분류",
        "",
    ]
    if categories:
        lines.extend(f"- {name}: {count}개" for name, count in sorted(categories.items()))
    else:
        lines.append("- 관측된 API 후보가 없습니다.")
    lines.extend(["", "## 관측된 엔드포인트", ""])
    for item in endpoints:
        status = ",".join(str(value) for value in item["statuses"]) or "-"
        guard = "차단" if item["blocked"] else "허용"
        lines.append(
            f"- `{item['method']} {item['path']}` · {item['category']} · {guard} · HTTP {status}"
        )
    lines.extend(["", "## 자동 동작 결과", ""])
    for action in result.actions:
        label = action.label.replace("\n", " ").strip()[:80]
        lines.append(f"- {action.kind} `{label}` · {action.risk} · {action.outcome}")
    if result.static_observations:
        lines.extend(["", "## 정적 보조 분석", ""])
        for observation in result.static_observations:
            select_names = [
                str(item.get("name", "")) or "(이름 없음)"
                for item in observation.get("selects", [])
            ]
            lines.append(
                f"- `{observation.get('url', '')}` · {observation.get('reason', '')} · "
                f"select {len(observation.get('selects', []))}개 "
                f"({', '.join(select_names[:10]) or '-'}) · "
                f"form {len(observation.get('forms', []))}개 · "
                f"명령 후보 {', '.join(observation.get('command_candidates', [])[:10]) or '-'}"
            )
    if result.date_probes:
        lines.extend(["", "## 미오픈 날짜 DOM 탐색", ""])
        for probe in result.date_probes:
            lines.append(
                f"- {probe.get('kind', '')} · `{probe.get('target_date', '-')}` · "
                f"상태 {probe.get('status', '-')} · 컨트롤 {probe.get('control_count', '-')} · "
                f"슬롯형 {probe.get('slot_like_control_count', '-')} · "
                f"DOM `{probe.get('structural_signature', '-')}`"
            )
    if result.warnings:
        lines.extend(["", "## 미확인·주의사항", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
    lines.extend(
        [
            "",
            "## 안전 경계",
            "",
            "- 이 결과는 탐색·관측 자료이며 실제 예약 성공을 증명하지 않습니다.",
            "- 예약·결제·취소·삭제 요청은 분석 중 차단됩니다.",
            "- 인증정보와 토큰 값은 보고서에서 마스킹됩니다.",
            "- 생성된 엔진 골격은 성공 판정과 결과 복구 검증 전에는 배포하지 마세요.",
            "",
        ]
    )
    report_path = output_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
