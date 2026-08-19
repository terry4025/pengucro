from __future__ import annotations

from typing import Any, Mapping

from engines.cgv_client import normalize_time, schedule_items
from engines.cgv_engine_movie_identity_runtime import (
    CgvEngine as MovieIdentityCgvEngine,
    _PREOPEN_SELECTION_ACTIVE,
)
from engines.cgv_movie_identity import schedule_matches_movie
from engines.cgv_preopen_matching import (
    PREOPEN_TIME_DRIFT_WINDOW_MINUTES,
    context_matches,
    has_booking_identity,
    rank_preopen_schedules,
)


class CgvEngine(MovieIdentityCgvEngine):
    """Final CGV runtime layer that explains every pre-open rejection stage.

    Reservation behavior stays in the underlying movie-identity runtime. This
    layer only turns the target-date schedule payload into a compact funnel so a
    future log can answer exactly why a screening was or was not promoted to the
    visitor/seat phase.
    """

    @staticmethod
    def _time_label(value: Any) -> str:
        normalized = normalize_time(value)
        if len(normalized) == 4 and normalized.isdigit():
            return f"{normalized[:2]}:{normalized[2:]}"
        return normalized or "-"

    @classmethod
    def _time_list(cls, items: list[Mapping[str, Any]]) -> str:
        values: list[str] = []
        for item in items:
            label = cls._time_label(item.get("scnsrtTm"))
            if label != "-" and label not in values:
                values.append(label)
        return ", ".join(values) if values else "-"

    @classmethod
    def _preferred_time_list(cls, preferred: list[str]) -> str:
        values: list[str] = []
        for value in preferred:
            label = cls._time_label(value)
            if label != "-" and label not in values:
                values.append(label)
        return ", ".join(values) if values else "-"

    @staticmethod
    def _time_minutes(value: Any) -> int | None:
        normalized = normalize_time(value)
        if len(normalized) != 4 or not normalized.isdigit():
            return None
        hour = int(normalized[:2])
        minute = int(normalized[2:])
        if hour > 29 or minute > 59:
            return None
        return hour * 60 + minute

    @classmethod
    def _time_distance_minutes(cls, left: Any, right: Any) -> int:
        left_minutes = cls._time_minutes(left)
        right_minutes = cls._time_minutes(right)
        if left_minutes is None or right_minutes is None:
            return 10**9
        direct = abs(left_minutes - right_minutes)
        return min(
            direct,
            abs((left_minutes + 1440) - right_minutes),
            abs(left_minutes - (right_minutes + 1440)),
        )

    @classmethod
    def _closest_preference(cls, actual: Any, preferred: list[str]) -> str:
        normalized = [normalize_time(value) for value in preferred if normalize_time(value)]
        if not normalized:
            return ""
        return min(normalized, key=lambda value: cls._time_distance_minutes(actual, value))

    @staticmethod
    def _missing_identity_fields(item: Mapping[str, Any]) -> list[str]:
        missing: list[str] = []
        for key in ("siteNo", "scnYmd", "scnsNo", "scnSseq"):
            value = item.get(key)
            if value is None or not str(value).strip():
                missing.append(key)
        return missing

    @staticmethod
    def _candidate_signature(item: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(
            str(item.get(key, "") or "")
            for key in (
                "siteNo",
                "scnYmd",
                "scnsNo",
                "scnSseq",
                "scnsrtTm",
                "cntlYn",
                "movNm",
                "expoProdNm",
                "expoScnsNm",
                "scnsNm",
                "movkndDsplEnm",
                "movkndDsplNm",
            )
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
            if context_matches(
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
        ranked = rank_preopen_schedules(selectable, preferred)
        chosen = ranked[0] if ranked else None

        signature = (
            len(items),
            len(movie_items),
            len(context_items),
            len(identified),
            len(selectable),
            len(ranked),
            tuple(normalize_time(value) for value in preferred),
            tuple(sorted(self._candidate_signature(item) for item in movie_items)),
        )
        if signature == self._preopen_diag_signature:
            return
        self._preopen_diag_signature = signature

        final_time = self._time_label(chosen.get("scnsrtTm")) if chosen else "-"
        funnel = (
            f"[CGV][미오픈 판정] 전체 {len(items)} → 영화 {len(movie_items)} → "
            f"관/포맷 {len(context_items)} → 회차ID {len(identified)} → "
            f"판매가능 {len(selectable)} → 시간허용 {len(ranked)} → 최종 {final_time}"
        )
        self.log(funnel, "success" if chosen else "info")

        if not movie_items:
            self.log(
                f"[CGV][미오픈 거절] 영화 단계 0 · 목표 '{movie}'가 목표 날짜 응답에 아직 없습니다.",
                "info",
            )
            return

        if not context_items:
            observed = sorted(
                {
                    f"{str(item.get('expoScnsNm') or item.get('scnsNm') or '-')} / "
                    f"{str(item.get('movkndDsplEnm') or item.get('movkndDsplNm') or '-')}"
                    for item in movie_items
                }
            )
            sample = " | ".join(observed[:4]) or "-"
            self.log(
                f"[CGV][미오픈 거절] 관/포맷 단계 0 · 요청 {auditorium or '-'} / "
                f"{format_name or '-'} · 현재 영화 회차 [{sample}]",
                "info",
            )
            return

        if not identified:
            partial_details: list[str] = []
            for item in context_items[:4]:
                missing = self._missing_identity_fields(item)
                partial_details.append(
                    f"{self._time_label(item.get('scnsrtTm'))}:missing={'+'.join(missing) or '-'}"
                )
            self.log(
                "[CGV][미오픈 거절] 회차ID 단계 0 · 예약에 필요한 실제 회차 ID가 아직 "
                f"완성되지 않았습니다 [{', '.join(partial_details)}]",
                "warning",
            )
            return

        if not selectable:
            locked = [
                item
                for item in identified
                if str(item.get("cntlYn", "N") or "N").upper() == "Y"
            ]
            self.log(
                f"[CGV][미오픈 거절] 판매가능 단계 0 · cntlYn=Y 잠금 {len(locked)}개 · "
                f"잠긴 실제시간 [{self._time_list(locked)}] · 해제 대기",
                "warning",
            )
            return

        if not ranked:
            self.log(
                f"[CGV][미오픈 거절] 시간 조건 0 · 참고시간 "
                f"[{self._preferred_time_list(preferred)}] · 실제 판매가능 "
                f"[{self._time_list(selectable)}] · 허용 ±{PREOPEN_TIME_DRIFT_WINDOW_MINUTES}분 내 후보 없음",
                "warning",
            )
            return

        actual = normalize_time(chosen.get("scnsrtTm"))
        source = self._closest_preference(actual, preferred)
        scns_no = str(chosen.get("scnsNo", "") or "-")
        scn_sseq = str(chosen.get("scnSseq", "") or "-")
        if source and source != actual:
            mapping = (
                f"참고 {self._time_label(source)} → 실제 {self._time_label(actual)}"
            )
        elif source:
            mapping = f"참고 {self._time_label(source)}와 exact match"
        else:
            mapping = f"실제 {self._time_label(actual)}"
        self.log(
            f"[CGV][미오픈 선택] {mapping} · 허용 후보 {len(ranked)}개 · "
            f"scnsNo={scns_no} · scnSseq={scn_sseq} · 다음 단계=관람인원/좌석 진입",
            "success",
        )
