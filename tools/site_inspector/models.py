from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class InspectorConfig:
    start_url: str
    output_root: Path
    max_pages: int = 12
    max_states: int = 36
    max_actions_per_page: int = 6
    max_depth: int = 3
    navigation_timeout_ms: int = 25_000
    response_body_limit: int = 256 * 1024
    script_body_limit: int = 2 * 1024 * 1024
    manual_intervention_timeout_seconds: int = 90
    date_probe_offsets_days: tuple[int, ...] = (30, 90, 180)
    max_date_probes: int = 12
    follow_related_subdomains: bool = True

    def validated(self) -> "InspectorConfig":
        parsed = urlsplit(self.start_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("분석 URL은 http:// 또는 https://로 시작해야 합니다.")
        if not 1 <= int(self.max_pages) <= 100:
            raise ValueError("최대 페이지 수는 1~100 사이여야 합니다.")
        if not 1 <= int(self.max_states) <= 300:
            raise ValueError("최대 화면 상태 수는 1~300 사이여야 합니다.")
        if not 0 <= int(self.max_depth) <= 8:
            raise ValueError("탐색 깊이는 0~8 사이여야 합니다.")
        if not 0 <= int(self.max_actions_per_page) <= 50:
            raise ValueError("페이지별 동작 수는 0~50 사이여야 합니다.")
        if not 0 <= int(self.manual_intervention_timeout_seconds) <= 300:
            raise ValueError("사람 확인 대기시간은 0~300초 사이여야 합니다.")
        if not 0 <= int(self.max_date_probes) <= 30:
            raise ValueError("미오픈 날짜 탐색 수는 0~30 사이여야 합니다.")
        if any(not 1 <= int(value) <= 730 for value in self.date_probe_offsets_days):
            raise ValueError("미오픈 날짜 간격은 1~730일 사이여야 합니다.")
        return self


@dataclass
class NetworkRecord:
    request_key: int
    method: str
    url: str
    resource_type: str
    risk: str
    category: str
    blocked: bool
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    error: str = ""


@dataclass
class ActionRecord:
    state_id: str
    kind: str
    label: str
    selector: str
    risk: str
    outcome: str
    resulting_url: str = ""
    resulting_state_id: str = ""


@dataclass
class PageState:
    state_id: str
    url: str
    title: str
    depth: int
    signature: str
    html_file: str
    screenshot_file: str
    interactive_count: int


@dataclass
class InspectionResult:
    output_dir: Path
    start_url: str
    states: list[PageState] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)
    network: list[NetworkRecord] = field(default_factory=list)
    script_endpoints: list[dict[str, str]] = field(default_factory=list)
    static_observations: list[dict[str, Any]] = field(default_factory=list)
    dom_inventory: list[dict[str, Any]] = field(default_factory=list)
    date_probes: list[dict[str, Any]] = field(default_factory=list)
    discovered_routes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_url": self.start_url,
            "output_dir": str(self.output_dir),
            "states": [asdict(item) for item in self.states],
            "actions": [asdict(item) for item in self.actions],
            "network": [asdict(item) for item in self.network],
            "script_endpoints": list(self.script_endpoints),
            "static_observations": list(self.static_observations),
            "dom_inventory": list(self.dom_inventory),
            "date_probes": list(self.date_probes),
            "discovered_routes": list(self.discovered_routes),
            "warnings": list(self.warnings),
        }
