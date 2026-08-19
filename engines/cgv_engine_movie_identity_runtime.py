from __future__ import annotations

from contextvars import ContextVar
import re
from typing import Any, Iterable, Mapping

import engines.cgv_engine as _base_engine_module
import engines.cgv_engine_priority_ladder as _priority_ladder_module
from engines.cgv_client import (
    normalize_time,
    schedule_items,
    select_schedule as _legacy_select_schedule,
)
from engines.cgv_engine_priority_ladder_runtime import CgvEngine as PriorityLadderRuntimeCgvEngine
from engines.cgv_movie_identity import schedule_matches_movie
from engines.cgv_preopen_matching import (
    context_matches as _resilient_context_matches,
    has_booking_identity,
    matching_schedule_candidates,
    rank_preopen_schedules,
    select_preopen_schedule,
)


_PREOPEN_SELECTION_ACTIVE: ContextVar[bool] = ContextVar(
    "pengucro_cgv_preopen_selection_active",
    default=False,
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _context_matches(
    item: Mapping[str, Any], auditorium: str, format_name: str
) -> bool:
    return _resilient_context_matches(item, auditorium, format_name)


def _canonical_select_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
) -> dict[str, Any] | None:
    preferred = [normalize_time(value) for value in preferred_times if normalize_time(value)]
    if not preferred and show_time:
        preferred = [normalize_time(show_time)]

    candidates = matching_schedule_candidates(
        payload,
        movie=movie,
        auditorium=auditorium,
        format_name=format_name,
    )
    if not candidates:
        return None
    if preferred:
        for target_time in preferred:
            for item in candidates:
                if normalize_time(item.get("scnsrtTm")) == target_time:
                    return item
        return None
    return candidates[0]


def select_schedule(
    payload: Mapping[str, Any],
    *,
    movie: str,
    show_time: str = "",
    auditorium: str = "",
    preferred_times: Iterable[str] = (),
    format_name: str = "",
) -> dict[str, Any] | None:
    """Select a real CGV screening while surviving pre-open publication drift.

    Open-date bookings preserve the historical exact-time behavior.  Pre-open
    bookings are different: their displayed times came from a reference date,
    so those values are priorities rather than immutable screening IDs.  During
    the pre-open -> real transition we therefore map each saved time to the
    nearest real matching screening, while still requiring the same movie and
    premium-format family and still rejecting ``cntlYn=Y`` controlled rows.
    """

    preferred_values = tuple(preferred_times)

    if _PREOPEN_SELECTION_ACTIVE.get():
        # Do not fall through to the historical selector here. During partial
        # publication it can accept an exact-time row before CGV has populated
        # the real screening IDs, which causes a premature visitor-page jump.
        return select_preopen_schedule(
            payload,
            movie=movie,
            show_time=show_time,
            auditorium=auditorium,
            preferred_times=preferred_values,
            format_name=format_name,
        )

    legacy = _legacy_select_schedule(
        payload,
        movie=movie,
        show_time=show_time,
        auditorium=auditorium,
        preferred_times=preferred_values,
        format_name=format_name,
    )
    if legacy is not None:
        return legacy

    return _canonical_select_schedule(
        payload,
        movie=movie,
        show_time=show_time,
        auditorium=auditorium,
        preferred_times=preferred_values,
        format_name=format_name,
    )


def _has_schedule_hint(
    payload: Any, movie: str, auditorium: str = ""
) -> bool:
    """Return whether the target movie has begun appearing on the target date.

    This is deliberately movie-only.  A regular 2D screening appearing before
    the requested IMAX screening is useful evidence that the target date is
    being published, so it should accelerate the pre-open watcher even though
    the requested auditorium is not available yet.  Auditorium/format matching
    remains strict enough in ``select_schedule`` to prevent crossing formats.
    """

    if not isinstance(payload, dict):
        return False
    del auditorium  # Kept for the historical call signature; hints are movie-only.
    for item in schedule_items(payload):
        item_format = str(
            item.get("movkndDsplEnm") or item.get("movkndDsplNm") or ""
        )
        if schedule_matches_movie(item, movie, item_format):
            return True
    return False


# The mature base reservation loop and the priority ladder both resolve these
# functions from their module globals at runtime. Installing the bridge here
# avoids copying either large engine while keeping all hold/checkout behavior
# unchanged.
_base_engine_module.select_schedule = select_schedule
_base_engine_module._has_schedule_hint = _has_schedule_hint
_priority_ladder_module.select_schedule = select_schedule


class CgvEngine(PriorityLadderRuntimeCgvEngine):
    """Final CGV runtime with resilient pre-open -> real schedule hand-off."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._preopen_diag_signature: tuple[Any, ...] | None = None

    @staticmethod
    def _preopen_requested(reservation_data: Mapping[str, Any]) -> bool:
        metadata = reservation_data.get("engine_metadata", {})
        if not isinstance(metadata, Mapping):
            return False
        cgv = metadata.get("cgv", {})
        if not isinstance(cgv, Mapping):
            return False

        def enabled(value: Any) -> bool:
            if isinstance(value, str):
                return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
            return bool(value)

        return enabled(cgv.get("is_preopen")) or enabled(cgv.get("reference_only"))

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        preopen = self._preopen_requested(reservation_data or {})
        token = _PREOPEN_SELECTION_ACTIVE.set(preopen)
        self._preopen_diag_signature = None
        try:
            if preopen:
                self.log(
                    "[CGV] 미오픈 복원 모드 · 참고 날짜의 시간은 우선순위로 사용하고 "
                    "실제 공개 회차에 자동 매핑합니다.",
                    "info",
                )
            return super().make_reservation_thread(reservation_data)
        finally:
            _PREOPEN_SELECTION_ACTIVE.reset(token)

    @staticmethod
    def _diagnostic_schedule_key(item: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(item.get(key, "") or "")
            for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq", "scnsrtTm")
        )

    def _log_preopen_schedule_diagnostics(self, payload: Mapping[str, Any]) -> None:
        if not _PREOPEN_SELECTION_ACTIVE.get():
            return
        movie = str(getattr(self, "_priority_movie", "") or "")
        auditorium = str(getattr(self, "_priority_auditorium", "") or "")
        format_name = str(getattr(self, "_priority_format", "") or "")
        preferred = list(getattr(self, "_priority_preferred_times", ()) or ())
        if not movie:
            return

        items = [dict(item) for item in schedule_items(payload)]
        movie_items = [
            item for item in items if schedule_matches_movie(item, movie, format_name)
        ]
        context_items = [
            item
            for item in movie_items
            if _resilient_context_matches(
                item,
                auditorium,
                format_name,
                include_controlled=True,
            )
        ]
        identified = [item for item in context_items if has_booking_identity(item)]
        selectable = [
            item
            for item in identified
            if str(item.get("cntlYn", "N") or "N").upper() != "Y"
        ]
        signature = (
            tuple(
                sorted(
                    (
                        *self._diagnostic_schedule_key(item),
                        str(item.get("cntlYn", "N") or "N").upper(),
                        str(item.get("expoScnsNm") or item.get("scnsNm") or ""),
                        str(item.get("movkndDsplEnm") or item.get("movkndDsplNm") or ""),
                    )
                    for item in movie_items
                )
            ),
            tuple(normalize_time(value) for value in preferred),
        )
        if signature == self._preopen_diag_signature:
            return
        self._preopen_diag_signature = signature

        if movie_items and not context_items:
            observed = sorted(
                {
                    f"{str(item.get('expoScnsNm') or item.get('scnsNm') or '-')} / "
                    f"{str(item.get('movkndDsplEnm') or item.get('movkndDsplNm') or '-')}"
                    for item in movie_items
                }
            )
            sample = " · ".join(observed[:3])
            self.log(
                f"[CGV] 미오픈 진단 · 목표 영화 {len(movie_items)}개가 보이지만 "
                f"요청 상영관/포맷은 아직 없음 ({sample})",
                "info",
            )
            return

        incomplete = len(context_items) - len(identified)
        if context_items and not identified and incomplete:
            self.log(
                f"[CGV] 목표 상영관 정보 {incomplete}개 부분 공개 · scnsNo/scnSseq 등 "
                "실제 회차 ID 완성 대기 · 조기 진입하지 않습니다.",
                "warning",
            )
            return

        controlled = len(identified) - len(selectable)
        if identified and not selectable and controlled:
            self.log(
                f"[CGV] 목표 상영관 회차 ID {controlled}개 선공개 · cntlYn=Y 잠금 상태 · "
                "해제 즉시 자동 진입하도록 고속 감시를 유지합니다.",
                "warning",
            )
            return

        if not selectable:
            return

        ranked = rank_preopen_schedules(selectable, preferred)
        if not ranked:
            return
        chosen = ranked[0]
        actual = normalize_time(chosen.get("scnsrtTm"))
        preferred_norm = [normalize_time(value) for value in preferred if normalize_time(value)]
        if preferred_norm and actual not in preferred_norm:
            source = min(
                preferred_norm,
                key=lambda value: abs(
                    (int(actual[:2]) * 60 + int(actual[2:]))
                    - (int(value[:2]) * 60 + int(value[2:]))
                )
                if len(actual) == 4 and len(value) == 4 and actual.isdigit() and value.isdigit()
                else 10**9,
            )
            actual_label = f"{actual[:2]}:{actual[2:]}" if len(actual) == 4 else actual
            source_label = f"{source[:2]}:{source[2:]}" if len(source) == 4 else source
            self.log(
                f"[CGV] 미오픈 시간표 드리프트 자동 보정 · 참고 {source_label} → "
                f"실제 {actual_label} · 실제 회차 ID를 즉시 채택합니다.",
                "success",
            )

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        result = super()._race_schedule(page, url, concurrency)
        if result.get("ok") and isinstance(result.get("data"), dict):
            self._log_preopen_schedule_diagnostics(result["data"])
        return result

    def _ordered_schedule_candidates(
        self, primary: dict[str, Any]
    ) -> list[dict[str, Any]]:
        ordered = list(super()._ordered_schedule_candidates(primary))
        if not _PREOPEN_SELECTION_ACTIVE.get():
            return ordered

        payload = getattr(self, "_priority_schedule_payload", {}) or {}
        if not isinstance(payload, Mapping) or not payload:
            return ordered
        candidates = matching_schedule_candidates(
            payload,
            movie=str(getattr(self, "_priority_movie", "") or ""),
            auditorium=str(getattr(self, "_priority_auditorium", "") or ""),
            format_name=str(getattr(self, "_priority_format", "") or ""),
        )
        ranked = rank_preopen_schedules(
            candidates,
            list(getattr(self, "_priority_preferred_times", ()) or ()),
        )
        seen = {self._schedule_key(item) for item in ordered}
        for item in ranked:
            key = self._schedule_key(item)
            if key not in seen:
                ordered.append(item)
                seen.add(key)
        return ordered or [primary]
