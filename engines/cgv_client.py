from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

import requests


CGV_HOME_URL = "https://cgv.co.kr"
CGV_API_URL = "https://api.cgv.co.kr"
CGV_BFF_CONTENT_URL = f"{CGV_HOME_URL}/api/v1/content"
CGV_BFF_BOOKING_URL = f"{CGV_HOME_URL}/api/v1/booking"
CGV_COMPANY_CODE = "A420"
CGV_MANUAL_SITE_VALUE = "manual"
CGV_MAX_WORKERS = 4
CGV_DEFAULT_WORKERS = 3

CGV_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": CGV_HOME_URL,
    "Referer": f"{CGV_HOME_URL}/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
    ),
}


class CgvError(RuntimeError):
    pass


class CgvAccessBlocked(CgvError):
    pass


@dataclass(frozen=True)
class CgvSeatGroup:
    seats: tuple[str, ...]


@dataclass(frozen=True)
class CgvRegion:
    code: str
    name: str
    count: int = 0


@dataclass(frozen=True)
class CgvSite:
    site_no: str
    name: str
    region_code: str

    @property
    def label(self) -> str:
        return self.name if self.name.startswith("CGV") else f"CGV {self.name}"


@dataclass(frozen=True)
class CgvSeat:
    seat_id: str
    label: str
    row: str
    number: int
    available: bool
    sale_enabled: bool = True
    sbord_no: str = ""
    seat_area_no: str = ""
    szone_no: str = ""
    szone_kind_cd: str = ""
    stknd_cd: str = ""
    seat_salfrm_cd: str = ""
    seat_status_cd: str = ""
    seat_sale_yn: str = ""
    xcoord_start: str = ""
    ycoord_start: str = ""
    xcoord_end: str = ""
    ycoord_end: str = ""
    left_passage: bool = False
    right_passage: bool = False


@dataclass(frozen=True)
class CgvSeatRecommendation:
    tier: str
    label: str
    reason: str


@dataclass(frozen=True)
class CgvSeatGuide:
    title: str
    summary: str
    details: tuple[str, ...]
    sources: tuple[tuple[str, str], ...]
    dedicated: bool = False


_GENERAL_SEAT_SOURCES = (
    ("THX 화면 배치 기준", "https://www.thx.com/questions/thx-certified-screen-placement/"),
    (
        "용산 IMAX 실제 관람 가이드",
        "https://presenttraveler.tistory.com/71",
    ),
)

_YONGSAN_IMAX_SOURCES = (
    (
        "용산 IMAX 좌석 구성",
        "https://cinespot.co.kr/cgv/cgv-yongsanaipakeumol/IDEqjcBbyuO0i4j9u4Wq",
    ),
    (
        "용산 IMAX H열 관람 가이드",
        "https://presenttraveler.tistory.com/71",
    ),
    (
        "용산 IMAX 중앙 좌석 후기",
        "https://nuut.tistory.com/60",
    ),
)


def _is_imax(auditorium: str, format_name: str) -> bool:
    return "IMAX" in f"{auditorium} {format_name}".upper()


def _is_yongsan_imax(site_no: str, auditorium: str, format_name: str) -> bool:
    return str(site_no).strip() == "0013" and _is_imax(auditorium, format_name)


def build_seat_guide(
    *, site_no: str, auditorium: str, format_name: str
) -> CgvSeatGuide:
    """Return preference-aware guidance; it is never presented as an objective guarantee."""

    if _is_yongsan_imax(site_no, auditorium, format_name):
        return CgvSeatGuide(
            title="용산 IMAX 전용 명당 가이드",
            summary="균형 H열 중앙 · 몰입 F–G열 중앙 · 편안 I–J열 중앙",
            details=(
                "균형형: H열 중앙은 화면 크기, 자막 가독성, 목의 편안함을 고르게 노리는 자리입니다.",
                "몰입형: F–G열 중앙은 화면이 시야를 더 가득 채우는 대신 개인에 따라 가깝게 느껴질 수 있습니다.",
                "편안형: I–J열 중앙은 전체 구도를 보기 쉽고 장시간 관람 피로를 줄이는 쪽입니다.",
                "좌우 위치는 실제 좌석도의 가운데를 계산하며, 용산 IMAX에서는 22·23번 주변이 중심입니다.",
            ),
            sources=_YONGSAN_IMAX_SOURCES,
            dedicated=True,
        )
    if _is_imax(auditorium, format_name):
        return CgvSeatGuide(
            title="IMAX 명당 가이드",
            summary="좌우 중앙 · 전체 깊이의 중간에서 약간 뒤쪽을 우선 추천",
            details=(
                "화면과 음향의 좌우 균형을 위해 실제 좌석도의 중앙을 먼저 계산합니다.",
                "몰입감을 원하면 추천 구역보다 한두 열 앞, 편안함을 원하면 한두 열 뒤를 선택해보세요.",
                "상영관 구조와 개인 취향에 따라 체감은 달라질 수 있습니다.",
            ),
            sources=_GENERAL_SEAT_SOURCES,
        )
    return CgvSeatGuide(
        title="상영관 명당 가이드",
        summary="좌우 중앙 · 전체 깊이의 약 60~70% 구역을 우선 추천",
        details=(
            "실제 좌석 배치에서 좌우 중앙과 중간보다 약간 뒤쪽인 좌석을 추천합니다.",
            "통로, 출입구, 스크린 크기에 따라 체감이 달라질 수 있으므로 취향에 맞게 앞뒤로 조정하세요.",
        ),
        sources=_GENERAL_SEAT_SOURCES,
    )


def recommend_cgv_seats(
    seats: Iterable[CgvSeat],
    *,
    site_no: str,
    auditorium: str,
    format_name: str,
) -> dict[str, CgvSeatRecommendation]:
    """Grade real CGV seats by horizontal center and auditorium depth."""

    seat_items = tuple(seats)
    rows: dict[str, list[CgvSeat]] = {}
    for seat in seat_items:
        rows.setdefault(seat.row, []).append(seat)
    if not rows:
        return {}

    row_names = tuple(sorted(rows, key=seat_row_sort_key))
    row_count = len(row_names)
    yongsan_imax = _is_yongsan_imax(site_no, auditorium, format_name)
    imax = _is_imax(auditorium, format_name)
    result: dict[str, CgvSeatRecommendation] = {}

    for row_index, row_name in enumerate(row_names):
        row_seats = rows[row_name]
        center = (min(seat.number for seat in row_seats) + max(seat.number for seat in row_seats)) / 2
        depth = row_index / max(1, row_count - 1)
        for seat in row_seats:
            center_distance = abs(seat.number - center)
            recommendation: CgvSeatRecommendation | None = None
            if yongsan_imax:
                if row_name == "H" and center_distance <= 2.5:
                    recommendation = CgvSeatRecommendation(
                        "best", "균형 최우선", "용산 IMAX H열 중앙의 균형형 명당"
                    )
                elif row_name in {"G", "I"} and center_distance <= 3.5:
                    mode = "몰입형" if row_name == "G" else "편안형"
                    recommendation = CgvSeatRecommendation(
                        "recommended", mode, f"용산 IMAX {row_name}열 중앙 {mode} 추천"
                    )
                elif row_name in {"F", "J"} and center_distance <= 4.5:
                    mode = "강한 몰입" if row_name == "F" else "편안한 전체화면"
                    recommendation = CgvSeatRecommendation(
                        "preference", "취향 추천", f"용산 IMAX {mode} 취향 추천"
                    )
            else:
                target_depth = 0.58 if imax else 0.65
                depth_gap = abs(depth - target_depth)
                if center_distance <= 1.5 and depth_gap <= 0.10:
                    recommendation = CgvSeatRecommendation(
                        "best", "중앙 최우선", "화면·음향 균형이 좋은 중앙 추천 구역"
                    )
                elif center_distance <= 3.5 and depth_gap <= 0.20:
                    recommendation = CgvSeatRecommendation(
                        "recommended", "추천", "중앙과 관람 거리를 함께 고려한 추천 구역"
                    )
                elif center_distance <= 4.5 and depth_gap <= 0.28:
                    recommendation = CgvSeatRecommendation(
                        "preference", "취향 추천", "앞뒤 취향에 따라 선택하기 좋은 중앙 구역"
                    )
            if recommendation:
                result[seat.label] = recommendation
    return result


def seat_row_sort_key(value: Any) -> tuple[tuple[int, Any], ...]:
    """Sort CGV rows naturally even when zone payloads arrive out of order."""

    text = str(value or "").strip().upper()
    parts = re.findall(r"[A-Z]+|\d+|[^A-Z\d]+", text)
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in parts
    ) or ((1, ""),)


def _coordinate(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def seat_layout_columns(seats: Iterable[CgvSeat]) -> dict[str, int]:
    """Map seats onto shared visual columns using CGV's physical coordinates.

    The official response contains pixel-like start coordinates.  Normalising
    those values keeps blocks and aisles aligned across rows.  Older/fallback
    DOM responses have no coordinates, so seat numbers remain the safe basis.
    """

    items = tuple(seats)
    if not items:
        return {}
    coordinates = {
        seat.label: coordinate
        for seat in items
        if (coordinate := _coordinate(seat.xcoord_start)) is not None
    }
    if len(coordinates) >= max(2, len(items) // 2):
        unique = sorted(set(coordinates.values()))
        diffs = [right - left for left, right in zip(unique, unique[1:]) if right > left]
        if diffs:
            lower_diffs = sorted(diffs)[: max(1, (len(diffs) + 1) // 2)]
            pitch = max(1.0, float(statistics.median(lower_diffs)))
        else:
            pitch = 1.0
        minimum = min(unique)
        return {
            seat.label: max(0, round((coordinates[seat.label] - minimum) / pitch))
            for seat in items
            if seat.label in coordinates
        } | {
            seat.label: max(0, seat.number - min(item.number for item in items))
            for seat in items
            if seat.label not in coordinates
        }

    minimum_number = min(seat.number for seat in items)
    return {seat.label: max(0, seat.number - minimum_number) for seat in items}


def is_physical_seat_group(group: Iterable[CgvSeat], people: int) -> bool:
    seats = tuple(sorted(group, key=lambda item: item.number))
    if len(seats) != max(1, int(people)) or len({seat.row for seat in seats}) != 1:
        return False
    for left, right in zip(seats, seats[1:]):
        if right.number != left.number + 1:
            return False
        if left.right_passage or right.left_passage:
            return False
    return True


def can_extend_physical_seat_group(
    all_seats: Iterable[CgvSeat],
    selected: Iterable[str],
    candidate: str,
    people: int,
) -> bool:
    """Validate a partial block against missing seats and official aisle flags."""

    labels = tuple(dict.fromkeys((*selected, normalize_seat_name(candidate))))
    if not can_extend_contiguous_seat_group(labels[:-1], labels[-1], people):
        return False
    parts = tuple(_seat_parts(label) for label in labels)
    if any(part is None for part in parts):
        return False
    row = parts[0][0]  # type: ignore[index]
    numbers = [part[1] for part in parts if part is not None]
    row_by_number = {
        seat.number: seat for seat in all_seats if normalize_seat_name(seat.row) == row
    }
    for number in range(min(numbers), max(numbers) + 1):
        if number not in row_by_number:
            return False
    for number in range(min(numbers), max(numbers)):
        left = row_by_number[number]
        right = row_by_number[number + 1]
        if left.right_passage or right.left_passage:
            return False
    return True


def choose_recommended_seat_group(
    seats: Iterable[CgvSeat],
    recommendations: Mapping[str, CgvSeatRecommendation],
    people: int,
    *,
    mode: str = "best",
    excluded: Iterable[Iterable[str]] = (),
) -> tuple[str, ...] | None:
    """Choose one physical, same-row adjacent block for a recommendation mode.

    Availability is deliberately not a filter: sold seats are valid priorities
    for future openings and cancellation-ticket monitoring.
    """

    people = max(1, int(people))
    excluded_groups = {
        tuple(sorted((normalize_seat_name(label) for label in group), key=_seat_parts))
        for group in excluded
    }
    rows: dict[str, list[CgvSeat]] = {}
    for seat in seats:
        rows.setdefault(seat.row, []).append(seat)
    if not rows:
        return None

    mode = str(mode or "best").strip().lower()
    preferred_rows = {
        "balanced": ("H",),
        "immersive": ("F", "G"),
        "comfortable": ("I", "J"),
    }.get(mode, ())
    target_tier = {
        "recommended": "recommended",
        "preference": "preference",
    }.get(mode, "")
    tier_score = {"best": 0, "recommended": 1, "preference": 2}
    candidates: list[tuple[tuple[Any, ...], tuple[str, ...]]] = []

    for row_name in sorted(rows, key=seat_row_sort_key):
        row_seats = sorted(rows[row_name], key=lambda item: item.number)
        if preferred_rows and row_name not in preferred_rows:
            continue
        center = (min(item.number for item in row_seats) + max(item.number for item in row_seats)) / 2
        for start in range(0, len(row_seats) - people + 1):
            group = tuple(row_seats[start : start + people])
            if not is_physical_seat_group(group, people):
                continue
            labels = tuple(item.label for item in group)
            canonical = tuple(sorted(labels, key=_seat_parts))
            if canonical in excluded_groups:
                continue
            tiers = tuple(
                recommendations[item.label].tier
                if item.label in recommendations else ""
                for item in group
            )
            if target_tier and target_tier not in tiers:
                continue
            if mode == "best" and not any(tiers):
                continue
            row_preference = preferred_rows.index(row_name) if preferred_rows else 0
            worst_tier = max((tier_score.get(tier, 4) for tier in tiers), default=4)
            average_tier = sum(tier_score.get(tier, 4) for tier in tiers) / people
            group_center = (group[0].number + group[-1].number) / 2
            candidates.append(
                ((row_preference, worst_tier, average_tier, abs(group_center - center), group[0].number), labels)
            )

    if not candidates and mode != "best":
        return choose_recommended_seat_group(
            tuple(seat for row in rows.values() for seat in row),
            recommendations,
            people,
            mode="best",
            excluded=excluded,
        )
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def normalize_time(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 3:
        digits = f"0{digits}"
    return digits[:4]


def normalize_seat_name(value: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "")).upper()


def _seat_parts(value: Any) -> tuple[str, int] | None:
    match = re.fullmatch(r"([^0-9]+)([0-9]+)", normalize_seat_name(value))
    if not match:
        return None
    row, raw_number = match.groups()
    return row, int(raw_number)


def can_extend_contiguous_seat_group(
    seats: Iterable[str], candidate: str, people: int
) -> bool:
    """Return whether a partial selection can still become one adjacent block."""

    labels = tuple(dict.fromkeys((*seats, normalize_seat_name(candidate))))
    people = max(1, int(people))
    if not labels or len(labels) > people:
        return False
    parts = tuple(_seat_parts(label) for label in labels)
    if any(part is None for part in parts):
        return False
    if people == 1:
        return len(labels) == 1
    rows = {part[0] for part in parts if part is not None}
    numbers = [part[1] for part in parts if part is not None]
    return len(rows) == 1 and max(numbers) - min(numbers) < people


def is_contiguous_seat_group(seats: Iterable[str], people: int | None = None) -> bool:
    """Require one same-row, duplicate-free, consecutive seat block."""

    labels = tuple(normalize_seat_name(seat) for seat in seats)
    required = len(labels) if people is None else max(1, int(people))
    if len(labels) != required or len(set(labels)) != len(labels):
        return False
    parts = tuple(_seat_parts(label) for label in labels)
    if any(part is None for part in parts):
        return False
    if required == 1:
        return True
    rows = {part[0] for part in parts if part is not None}
    numbers = sorted(part[1] for part in parts if part is not None)
    return len(rows) == 1 and numbers == list(range(numbers[0], numbers[0] + required))


def parse_seat_groups(value: str, people: int) -> tuple[CgvSeatGroup, ...]:
    """Parse ordered seat alternatives.

    ``H10,H11 | G10,G11`` means H10/H11 first and G10/G11 second.  When a
    single anchor is supplied for a multi-person booking, adjacent seats to the
    right and then to the left are generated as safe fallbacks.
    """

    groups: list[CgvSeatGroup] = []
    for raw_group in re.split(r"[|/]", value or ""):
        seats = tuple(
            seat
            for seat in (normalize_seat_name(item) for item in re.split(r"[,;\s]+", raw_group))
            if seat
        )
        if seats:
            groups.append(CgvSeatGroup(seats))
    if not groups:
        return ()

    people = max(1, int(people))
    expanded: list[CgvSeatGroup] = []
    for group in groups:
        if len(group.seats) == people:
            if is_contiguous_seat_group(group.seats, people):
                expanded.append(group)
            continue
        if len(group.seats) != 1 or people == 1:
            continue
        match = re.fullmatch(r"([^0-9]+)([0-9]+)", group.seats[0])
        if not match:
            continue
        row, raw_number = match.groups()
        number = int(raw_number)
        right = tuple(f"{row}{number + offset}" for offset in range(people))
        left_start = number - people + 1
        left = tuple(f"{row}{left_start + offset}" for offset in range(people))
        expanded.append(CgvSeatGroup(right))
        if left_start > 0 and left != right:
            expanded.append(CgvSeatGroup(left))
    return tuple(expanded)


def _walk_site_nodes(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if value.get("siteNo") and (value.get("siteNm") or value.get("siteName")):
            yield value
        for child in value.values():
            yield from _walk_site_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_site_nodes(child)


def parse_site_list(payload: Mapping[str, Any]) -> dict[str, str]:
    sites: dict[str, str] = {}
    for item in _walk_site_nodes(payload.get("data", payload)):
        site_no = str(item.get("siteNo", "")).strip()
        site_name = str(item.get("siteNm") or item.get("siteName") or "").strip()
        if not site_no or not site_name:
            continue
        label = site_name if site_name.startswith("CGV") else f"CGV {site_name}"
        sites[label] = site_no
    return dict(sorted(sites.items(), key=lambda pair: pair[0]))


# CGV theaters listed by the official IMAX special-cinema page on 2026-08-20.
# This is a fallback only.  The browser client refreshes the authoritative list
# from CGV's display-content APIs before filtering the catalog whenever possible.
CGV_IMAX_SITE_NOS: frozenset[str] = frozenset({
    "0079",  # 창원더시티
    "0005",  # 서면
    "0089",  # 센텀시티
    "0040",  # 압구정
    "0059",  # 영등포타임스퀘어
    "0074",  # 왕십리
    "0013",  # 용산아이파크몰
    "0199",  # 천호
    "0179",  # 전주효자
    "0268",  # 순천신대
    "0127",  # 대전터미널
    "0228",  # 청주(서문)
    "0128",  # 울산삼산
    "0293",  # 천안터미널
    "0110",  # 천안펜타포트
    "0002",  # 인천
    "0070",  # 춘천
    "0257",  # 광교
    "0106",  # 동탄
    "0143",  # 소풍
    "0274",  # 스타필드시티위례
    "0113",  # 의정부
    "0054",  # 일산
    "0181",  # 판교
    "0052",  # 평택
})


def parse_imax_unit_content_relation_no(payload: Mapping[str, Any]) -> str:
    """Return the official IMAX theater-guide component relation number."""

    data = payload.get("data", payload)
    if not isinstance(data, list):
        return ""

    def components(values: list[Any]) -> Iterator[Mapping[str, Any]]:
        for component in values:
            if not isinstance(component, Mapping):
                continue
            yield component
            nested = component.get("scrDspCpotLstSearchResLst", ()) or ()
            if isinstance(nested, list):
                yield from components(nested)

    for component in components(data):
        if not isinstance(component, Mapping):
            continue
        title = re.sub(r"\s+", "", str(component.get("title", "")))
        is_theater_guide = title == "IMAX극장안내" or (
            str(component.get("screnDispCpotTypCd", "")).strip() == "07"
            and str(component.get("screnDispCpotCd", "")).strip() == "14"
        )
        if not is_theater_guide:
            continue
        relation_no = str(component.get("unitCpotRelNo", "")).strip()
        if relation_no:
            return relation_no
        candidates = component.get("scrDspCpotLstSearchResLst", ()) or ()
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            relation_no = str(item.get("unitCpotRelNo", "")).strip()
            if relation_no:
                return relation_no
    return ""


def parse_imax_site_nos(payload: Mapping[str, Any]) -> frozenset[str]:
    """Return site numbers from CGV's official IMAX theater-guide detail."""

    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return frozenset()
    items = data.get("dspScrdispSiteList", ()) or ()
    if not isinstance(items, list):
        return frozenset()
    return frozenset(
        site_no
        for item in items
        if isinstance(item, Mapping)
        and (site_no := str(item.get("siteNo", "")).strip())
    )


def is_imax_site(item: Mapping[str, Any] | CgvSite) -> bool:
    """Return whether a CGV site operates an IMAX auditorium."""
    if isinstance(item, CgvSite):
        site_no = str(item.site_no).strip()
    else:
        site_no = str(item.get("siteNo", "")).strip()
    if site_no in CGV_IMAX_SITE_NOS:
        return True
    if isinstance(item, Mapping):
        text = " ".join(str(v) for v in item.values()).upper()
        if "IMAX" in text:
            return True
    return False


def filter_imax_catalog(
    regions: Iterable[CgvRegion],
    sites: Iterable[CgvSite],
    *,
    imax_site_nos: frozenset[str] | None = None,
) -> tuple[tuple[CgvRegion, ...], tuple[CgvSite, ...]]:
    """Filter sites to IMAX theaters and recompute regional counts accordingly."""
    allowed = imax_site_nos if imax_site_nos is not None else CGV_IMAX_SITE_NOS
    filtered_sites = tuple(site for site in sites if site.site_no in allowed)
    counts_by_region: dict[str, int] = {}
    for site in filtered_sites:
        counts_by_region[site.region_code] = counts_by_region.get(site.region_code, 0) + 1

    filtered_regions = tuple(
        CgvRegion(code=region.code, name=region.name, count=counts_by_region[region.code])
        for region in regions
        if region.code in counts_by_region and counts_by_region[region.code] > 0
    )
    return filtered_regions, filtered_sites


def parse_site_catalog(
    payload: Mapping[str, Any],
    *,
    imax_only: bool = False,
    imax_site_nos: frozenset[str] | None = None,
) -> tuple[tuple[CgvRegion, ...], tuple[CgvSite, ...]]:
    """Parse CGV's current region/site BFF response without static site data."""

    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        return (), ()
    regions: list[CgvRegion] = []
    for item in data.get("regionInfo", ()) or ():
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("comCdval", "")).strip()
        name = str(item.get("comCdvalNm", "")).strip()
        if not code or not name:
            continue
        try:
            count = int(item.get("cnt", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        regions.append(CgvRegion(code, name, count))

    sites: list[CgvSite] = []
    for item in data.get("siteInfo", ()) or ():
        if not isinstance(item, Mapping):
            continue
        site_no = str(item.get("siteNo", "")).strip()
        name = str(item.get("siteNm", "")).strip()
        region_code = str(item.get("regnGrpCd", "")).strip()
        if site_no and name:
            sites.append(CgvSite(site_no, name, region_code))
    if imax_only:
        return filter_imax_catalog(regions, sites, imax_site_nos=imax_site_nos)
    return tuple(regions), tuple(sites)


def parse_dom_seats(items: Iterable[Mapping[str, Any]]) -> tuple[CgvSeat, ...]:
    seats: list[CgvSeat] = []
    seen: set[str] = set()
    for item in items:
        label = normalize_seat_name(item.get("label", ""))
        seat_id = str(item.get("id", "")).strip()
        match = re.fullmatch(r"([^0-9]+)([0-9]+)", label)
        if not seat_id or not match or label in seen:
            continue
        seen.add(label)
        row, number = match.groups()
        disabled = bool(item.get("disabled", False))
        sale_enabled = bool(item.get("saleEnabled", not disabled))
        seats.append(
            CgvSeat(
                seat_id=seat_id,
                label=label,
                row=row,
                number=int(number),
                available=not disabled,
                sale_enabled=sale_enabled,
            )
        )
    return tuple(seats)


def parse_api_seats(payload: Mapping[str, Any]) -> tuple[CgvSeat, ...]:
    """Parse every physical seat returned by CGV's official seat BFF.

    Sold and temporarily-held seats deliberately remain in the result so the
    setup dialog can save them as cancellation-ticket priorities.
    """

    data = payload.get("data", payload)
    roots = data.get("items", ()) if isinstance(data, Mapping) else ()
    if isinstance(roots, Mapping):
        roots = (roots,)
    seats: list[CgvSeat] = []
    seen: set[str] = set()
    for root in roots or ():
        if not isinstance(root, Mapping):
            continue
        raw_seats = root.get("seats", ()) or ()
        for item in raw_seats:
            if not isinstance(item, Mapping):
                continue
            seat_id = str(item.get("seatLocNo", "")).strip()
            row = str(item.get("seatRowNm", "")).strip().upper()
            raw_number = str(item.get("seatNo", "")).strip()
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                continue
            label = normalize_seat_name(f"{row}{number}")
            if not seat_id or not row or label in seen:
                continue
            seen.add(label)
            status = str(item.get("seatStusCd", "")).strip()
            sale_yn = str(item.get("seatSaleYn", "Y")).strip().upper()
            seats.append(
                CgvSeat(
                    seat_id=seat_id,
                    label=label,
                    row=row,
                    number=number,
                    available=status == "00" and sale_yn == "Y",
                    sale_enabled=sale_yn == "Y",
                    sbord_no=str(item.get("sbordNo", "")).strip(),
                    seat_area_no=str(item.get("seatAreaNo", "")).strip(),
                    szone_no=str(item.get("szoneNo", "")).strip(),
                    szone_kind_cd=str(item.get("szoneKindCd", "")).strip(),
                    stknd_cd=str(item.get("stkndCd", "")).strip(),
                    seat_salfrm_cd=str(item.get("seatSalfrmCd", "")).strip(),
                    seat_status_cd=status,
                    seat_sale_yn=sale_yn,
                    xcoord_start=str(item.get("xcoordStartVal", "")).strip(),
                    ycoord_start=str(item.get("ycoordStartVal", "")).strip(),
                    xcoord_end=str(item.get("xcoordEndVal", "")).strip(),
                    ycoord_end=str(item.get("ycoordEndVal", "")).strip(),
                    left_passage=str(item.get("leftPwayYn", "")).strip().upper() == "Y",
                    right_passage=str(item.get("rghtPwayYn", "")).strip().upper() == "Y",
                )
            )
    return tuple(seats)


def build_seat_price_payload(
    schedule: Mapping[str, Any], seats: Iterable[CgvSeat], people: int
) -> dict[str, Any]:
    selected = tuple(seats)
    return {
        "coCd": str(schedule.get("coCd") or CGV_COMPANY_CODE),
        "siteNo": str(schedule.get("siteNo", "")),
        "scnsNo": str(schedule.get("scnsNo", "")),
        "scnYmd": str(schedule.get("scnYmd", "")),
        "scnSseq": str(schedule.get("scnSseq", "")),
        "movNo": str(schedule.get("movNo", "")),
        "rtctlScopCd": str(schedule.get("rtctlScopCd") or "08"),
        "prcrulDivCd": str(schedule.get("prcrulDivCd") or "01"),
        "sachlTypCd": str(schedule.get("sachlTypCd") or "01"),
        "prodBnduList": [{"prodBnduCd": "01", "prodBnduQty": max(1, int(people))}],
        "seatList": [
            {
                "seatLocNo": seat.seat_id,
                "szoneKindCd": seat.szone_kind_cd,
                "stkndCd": seat.stknd_cd,
                "seatSalfrmCd": seat.seat_salfrm_cd,
                "prodBnduCd": "01",
            }
            for seat in selected
        ],
        "zoneGroupYn": "N",
    }


def build_seat_hold_payload(
    schedule: Mapping[str, Any],
    seats: Iterable[CgvSeat],
    *,
    cust_no: str = "",
    customer_grade_code: str = "01",
    birth: str = "",
    phone: str = "",
    certification_no: str = "",
) -> dict[str, Any]:
    return {
        "coCd": str(schedule.get("coCd") or CGV_COMPANY_CODE),
        "bymd": re.sub(r"\D", "", birth),
        "mbltNo": re.sub(r"\D", "", phone),
        "siteNo": str(schedule.get("siteNo", "")),
        "scnYmd": str(schedule.get("scnYmd", "")),
        "scnsNo": str(schedule.get("scnsNo", "")),
        "scnSseq": str(schedule.get("scnSseq", "")),
        "movAtktNo": "",
        "custNo": str(cust_no or ""),
        "cusgdCd": str(customer_grade_code or "01"),
        "nmbrCrtfNo": str(certification_no or ""),
        "sachlCd": "10",
        "atktChnlCd": "01",
        "sachlTypCd": str(schedule.get("sachlTypCd") or "01"),
        "rtctlScopCd": str(schedule.get("rtctlScopCd") or "08"),
        "seatPrmpDataList": [
            {
                "seatRowNm": seat.row,
                "seatNo": str(seat.number),
                "seatLocNo": seat.seat_id,
                "sbordNo": seat.sbord_no,
                "seatAreaNo": seat.seat_area_no,
                "szoneNo": seat.szone_no,
            }
            for seat in seats
        ],
    }


def schedule_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("scnSseq") is not None and value.get("scnsrtTm"):
                item = dict(value)
                identity = tuple(
                    str(item.get(key, ""))
                    for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq")
                )
                if identity not in seen:
                    seen.add(identity)
                    items.append(item)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload.get("data", payload))
    return items


def select_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
) -> dict[str, Any] | None:
    movie_key = re.sub(r"\s+", "", movie).casefold()
    auditorium_key = re.sub(r"\s+", "", auditorium).casefold()
    format_key = re.sub(r"\s+", "", format_name).casefold()
    raw_preferred = [normalize_time(t) for t in preferred_times if normalize_time(t)]
    if not raw_preferred and show_time:
        raw_preferred = [normalize_time(show_time)]

    candidates: list[dict[str, Any]] = []
    for item in schedule_items(payload):
        movie_text = " ".join(
            str(item.get(key, ""))
            for key in ("movNm", "expoProdNm", "prodNm", "movNo")
        )
        screen_text = " ".join(
            str(item.get(key, ""))
            for key in (
                "expoScnsNm",
                "scnsNm",
            )
        )
        format_text = " ".join(
            str(item.get(key, ""))
            for key in (
                "movkndDsplEnm",
                "movkndDsplNm",
                "sbtdivNm",
                "videoAddexpCdNm",
                "expoScnsNm",
                "scnsNm",
            )
        )
        if movie_key and movie_key not in re.sub(r"\s+", "", movie_text).casefold():
            continue
        if auditorium_key and auditorium_key not in re.sub(r"\s+", "", screen_text).casefold() and auditorium_key not in re.sub(r"\s+", "", format_text).casefold():
            continue
        if format_key and format_key not in re.sub(r"\s+", "", format_text).casefold():
            continue
        if str(item.get("cntlYn", "N")).upper() == "Y":
            continue
        candidates.append(item)

    if not candidates:
        return None

    if raw_preferred:
        for pref in raw_preferred:
            for item in candidates:
                if normalize_time(item.get("scnsrtTm")) == pref:
                    return item
        return None

    return candidates[0]


class CgvClient:
    def __init__(self, timeout: float = 10.0, session: requests.Session | None = None) -> None:
        self.timeout = max(2.0, float(timeout))
        self.session = session or requests.Session()
        self.session.headers.update(CGV_HEADERS)

    def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        base_url = CGV_HOME_URL if path.startswith("/api/") else CGV_API_URL
        response = self.session.get(
            f"{base_url}{path}", params=dict(params), timeout=self.timeout
        )
        if response.status_code in {403, 429}:
            raise CgvAccessBlocked(
                "CGV가 현재 연결을 제한했습니다. 잠시 뒤 로그인된 Chrome에서 다시 시도해주세요."
            )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise CgvError("CGV 응답을 JSON으로 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise CgvError("CGV 응답 형식이 올바르지 않습니다.")
        return payload

    def fetch_sites(self) -> dict[str, str]:
        payload = self._get(
            "/api/v1/content/site/searchAllRegionAndSite",
            {"coCd": CGV_COMPANY_CODE, "custNo": "", "lntd": "", "lttd": "", "srchKwrd": ""},
        )
        sites = parse_site_list(payload)
        if not sites:
            raise CgvError("CGV 지점 목록을 찾지 못했습니다.")
        return sites

    def fetch_schedule(self, site_no: str, screening_date: str) -> dict[str, Any]:
        return self._get(
            "/api/v1/booking/searchMovScnInfo",
            {
                "coCd": CGV_COMPANY_CODE,
                "siteNo": site_no,
                "scnYmd": re.sub(r"\D", "", screening_date),
                "scnsNo": "",
                "scnSseq": "",
                "rtctlScopCd": "08",
                "custNo": "",
            },
        )
