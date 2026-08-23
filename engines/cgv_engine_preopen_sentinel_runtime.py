from __future__ import annotations

from datetime import datetime, timedelta
import re
import time
import urllib.parse
from typing import Any, Mapping

from engines.cgv_client import (
    CGV_BFF_BOOKING_URL,
    CGV_COMPANY_CODE,
    schedule_items,
)
from engines.cgv_engine_funnel_runtime import CgvEngine as FunnelCgvEngine
from engines.cgv_engine_movie_identity_runtime import _PREOPEN_SELECTION_ACTIVE
from engines.cgv_movie_identity import (
    schedule_matches_movie,
    schedule_matches_movie_title_exact,
)
from engines.cgv_preopen_matching import (
    context_matches,
    matching_schedule_candidates,
    rank_preopen_schedules,
)


class CgvEngine(FunnelCgvEngine):
    """Long-running pre-open watcher with an independent CGV date sentinel.

    ``searchMovScnInfo`` remains the authoritative booking feed. The
    ``searchSiteScnscYmdListByMov`` endpoint is only a low-frequency early
    signal: when CGV starts advertising the target date for the selected movie,
    the normal schedule watcher temporarily enters its existing 0.5 s burst.

    The sentinel is deliberately fail-open. Any sentinel error is ignored by
    the booking path, so a multi-day watcher can still succeed solely through
    the mature schedule loop.
    """

    DATE_SENTINEL_INTERVAL_SECONDS = 15.0
    DATE_SENTINEL_TIMEOUT_MS = 3500
    DATE_SENTINEL_BURST_SECONDS = 90.0
    DATE_LISTED_INTERVAL_SECONDS = 1.0
    MOVIE_NO_DISCOVERY_INTERVAL_SECONDS = 300.0
    MOVIE_NO_CATALOG_DISCOVERY_INTERVAL_SECONDS = 900.0
    SENTINEL_ERROR_LOG_INTERVAL_SECONDS = 600.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._preopen_sentinel_reference_date = ""
        self._preopen_sentinel_mov_no = ""
        self._preopen_sentinel_last_probe = 0.0
        self._preopen_sentinel_last_discovery = 0.0
        self._preopen_sentinel_last_catalog_discovery = 0.0
        self._preopen_sentinel_date_listed: bool | None = None
        self._preopen_sentinel_last_error_log = 0.0

    @staticmethod
    def _date_digits(value: Any) -> str:
        digits = re.sub(r"\D", "", str(value or ""))
        return digits if len(digits) == 8 else ""

    def _schedule_burst_active(self) -> bool:
        """A hint may request a burst, but it must never pin high-speed mode forever."""

        return time.monotonic() < float(
            getattr(self, "_schedule_burst_until", 0.0) or 0.0
        )

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        data = dict(reservation_data or {})
        metadata = data.get("engine_metadata", {})
        cgv = metadata.get("cgv", {}) if isinstance(metadata, Mapping) else {}
        cgv = cgv if isinstance(cgv, Mapping) else {}

        self._preopen_sentinel_reference_date = self._date_digits(
            cgv.get("reference_date")
        )
        self._preopen_sentinel_mov_no = str(
            cgv.get("mov_no") or cgv.get("movNo") or ""
        ).strip()
        self._preopen_sentinel_last_probe = 0.0
        self._preopen_sentinel_last_discovery = 0.0
        self._preopen_sentinel_last_catalog_discovery = 0.0
        self._preopen_sentinel_date_listed = None
        self._preopen_sentinel_last_error_log = 0.0
        return super().make_reservation_thread(data)

    @staticmethod
    def _schedule_target_from_url(url: str) -> tuple[str, str]:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
        except Exception:
            return "", ""
        site_no = str((query.get("siteNo") or [""])[0] or "").strip()
        target_date = CgvEngine._date_digits((query.get("scnYmd") or [""])[0])
        return site_no, target_date

    @staticmethod
    def _fetch_same_origin_json(
        page,
        url: str,
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        try:
            result = page.evaluate(
                r"""
                async ({url, timeoutMs}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const headers = new Headers({
                      'Accept': 'application/json, text/plain, */*',
                      'Accept-Language': 'ko-KR',
                    });
                    const item = String(document.cookie || '').split('; ')
                      .find(value => value.startsWith('accessToken='));
                    if (item) {
                      let token = item.slice('accessToken='.length);
                      try { token = decodeURIComponent(token); } catch (_) {}
                      if (token) headers.set('Authorization', `Bearer ${token}`);
                    }
                    const response = await fetch(url, {
                      method: 'GET',
                      cache: 'no-store',
                      credentials: 'include',
                      headers,
                      signal: controller.signal,
                    });
                    let data = null;
                    try { data = await response.json(); } catch (_) {}
                    return {ok: response.ok, status: response.status, data};
                  } catch (error) {
                    return {
                      ok: false,
                      status: 0,
                      timedOut: Boolean(error && error.name === 'AbortError'),
                      error: String(error || 'fetch failed'),
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {"url": url, "timeoutMs": max(500, int(timeout_ms))},
            )
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        return (
            dict(result)
            if isinstance(result, dict)
            else {"ok": False, "status": 0, "error": "invalid-sentinel-result"}
        )

    @staticmethod
    def _schedule_url(site_no: str, screening_date: str) -> str:
        query = urllib.parse.urlencode(
            {
                "coCd": CGV_COMPANY_CODE,
                "siteNo": str(site_no),
                "scnYmd": CgvEngine._date_digits(screening_date),
                "scnsNo": "",
                "scnSseq": "",
                "rtctlScopCd": "08",
                "custNo": "",
            }
        )
        return f"{CGV_BFF_BOOKING_URL}/searchMovScnInfo?{query}"

    @staticmethod
    def _date_sentinel_url(site_no: str, mov_no: str) -> str:
        query = urllib.parse.urlencode(
            {
                "coCd": CGV_COMPANY_CODE,
                "siteNo": str(site_no),
                "movNo": str(mov_no),
            }
        )
        return f"{CGV_BFF_BOOKING_URL}/searchSiteScnscYmdListByMov?{query}"

    @staticmethod
    def _movie_catalog_query(movie: str) -> str:
        return urllib.parse.urlencode(
            {
                "coCd": CGV_COMPANY_CODE,
                "movNm": str(movie or "").strip(),
                "div": "",
                "attrCd": "",
            }
        )

    @staticmethod
    def _extract_catalog_mov_no(
        payload,
        *,
        movie: str,
        format_name: str = "",
    ) -> str:
        """Return one unambiguous exact-title ID from the booking catalog."""

        if not isinstance(payload, Mapping):
            return ""
        rows = payload.get("data")
        if isinstance(rows, Mapping):
            rows = rows.get("data")
        if not isinstance(rows, list):
            return ""
        matches: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if not schedule_matches_movie_title_exact(row, movie, format_name):
                continue
            mov_no = str(row.get("movNo") or "").strip()
            if mov_no:
                matches.add(mov_no)
        return next(iter(matches)) if len(matches) == 1 else ""

    def _maybe_discover_mov_no_from_catalog(self, page) -> None:
        """Resolve an unknown movie ID through CGV's own booking catalog.

        The reference-date schedule probes only work when the film already has
        published screenings. Brand-new pre-open titles can be absent there,
        so one bounded same-origin catalog query keeps the date sentinel
        usable from day one. Fail-open like every other sentinel helper."""

        if self._preopen_sentinel_mov_no:
            return
        now = time.monotonic()
        if (
            self._preopen_sentinel_last_catalog_discovery > 0
            and now - self._preopen_sentinel_last_catalog_discovery
            < self.MOVIE_NO_CATALOG_DISCOVERY_INTERVAL_SECONDS
        ):
            return
        self._preopen_sentinel_last_catalog_discovery = now
        movie = str(getattr(self, "_priority_movie", "") or "").strip()
        if not movie:
            return
        result = self._fetch_same_origin_json(
            page,
            f"{CGV_BFF_BOOKING_URL}/searchAtktTopPostrList?{self._movie_catalog_query(movie)}",
            timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
        )
        data = result.get("data")
        if result.get("ok") and isinstance(data, Mapping):
            mov_no = self._extract_catalog_mov_no(
                data,
                movie=movie,
                format_name=str(getattr(self, "_priority_format", "") or ""),
            )
            if self._remember_mov_no(mov_no, source="예매 영화 목록 조회"):
                return
        # A missing title is normal before CGV lists it; stay quiet and retry
        # on the next bounded interval without touching the booking path.
        return

    def _extract_target_mov_no(self, payload: Mapping[str, Any]) -> str:
        movie = str(getattr(self, "_priority_movie", "") or "")
        auditorium = str(getattr(self, "_priority_auditorium", "") or "")
        format_name = str(getattr(self, "_priority_format", "") or "")
        if not movie or not isinstance(payload, Mapping):
            return ""

        movie_items = [
            item
            for item in schedule_items(payload)
            if schedule_matches_movie(item, movie, format_name)
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
        # A film has the same movNo across its 2D/IMAX screenings, but prefer the
        # requested auditorium first in case CGV ever returns similarly named rows.
        for pool in (context_items, movie_items):
            for item in pool:
                mov_no = str(item.get("movNo") or "").strip()
                if mov_no:
                    return mov_no
        return ""

    def _payload_has_bookable_target(self, payload: Mapping[str, Any]) -> bool:
        if not isinstance(payload, Mapping):
            return False
        candidates = matching_schedule_candidates(
            payload,
            movie=str(getattr(self, "_priority_movie", "") or ""),
            auditorium=str(getattr(self, "_priority_auditorium", "") or ""),
            format_name=str(getattr(self, "_priority_format", "") or ""),
        )
        if not candidates:
            return False
        preferred = list(getattr(self, "_priority_preferred_times", ()) or ())
        return bool(rank_preopen_schedules(candidates, preferred))

    def _reference_dates(self, target_date: str) -> tuple[str, ...]:
        dates: list[str] = []
        reference = self._date_digits(self._preopen_sentinel_reference_date)
        if reference and reference != target_date:
            dates.append(reference)

        try:
            target = datetime.strptime(target_date, "%Y%m%d").date()
        except ValueError:
            return tuple(dates)

        for days in (1, 2, 7):
            candidate = (target - timedelta(days=days)).strftime("%Y%m%d")
            if candidate != target_date and candidate not in dates:
                dates.append(candidate)
        return tuple(dates)

    def _remember_mov_no(self, mov_no: str, *, source: str) -> bool:
        clean = str(mov_no or "").strip()
        if not clean:
            return False
        if clean == self._preopen_sentinel_mov_no:
            return True
        self._preopen_sentinel_mov_no = clean
        self.log(
            f"[CGV][미오픈 sentinel] 영화 ID 확보 · movNo={clean} · {source}",
            "info",
        )
        return True

    def _maybe_discover_mov_no(
        self,
        page,
        *,
        site_no: str,
        target_date: str,
        target_payload: Mapping[str, Any] | None,
    ) -> None:
        if self._preopen_sentinel_mov_no:
            return

        if isinstance(target_payload, Mapping):
            mov_no = self._extract_target_mov_no(target_payload)
            if self._remember_mov_no(mov_no, source="목표 날짜 회차 응답"):
                return

        self._maybe_discover_mov_no_from_catalog(page)

        now = time.monotonic()
        if (
            self._preopen_sentinel_last_discovery > 0
            and now - self._preopen_sentinel_last_discovery
            < self.MOVIE_NO_DISCOVERY_INTERVAL_SECONDS
        ):
            return
        self._preopen_sentinel_last_discovery = now

        for reference_date in self._reference_dates(target_date):
            if self.stop_event.is_set():
                return
            result = self._fetch_same_origin_json(
                page,
                self._schedule_url(site_no, reference_date),
                timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
            )
            data = result.get("data")
            if not result.get("ok") or not isinstance(data, Mapping):
                continue
            mov_no = self._extract_target_mov_no(data)
            if self._remember_mov_no(
                mov_no,
                source=(
                    f"참고 날짜 {reference_date[:4]}-{reference_date[4:6]}-"
                    f"{reference_date[6:]}"
                ),
            ):
                return

    def _log_sentinel_error(self, result: Mapping[str, Any]) -> None:
        now = time.monotonic()
        if (
            self._preopen_sentinel_last_error_log > 0
            and now - self._preopen_sentinel_last_error_log
            < self.SENTINEL_ERROR_LOG_INTERVAL_SECONDS
        ):
            return
        self._preopen_sentinel_last_error_log = now
        status = int(result.get("status", 0) or 0)
        reason = (
            f"HTTP {status}"
            if status
            else "timeout"
            if result.get("timedOut")
            else "network"
        )
        self.log(
            f"[CGV][미오픈 sentinel] 보조 날짜 조회 일시 실패({reason}) · "
            "기존 회차 감시는 중단하지 않고 계속합니다.",
            "warning",
        )

    def _maybe_probe_date_sentinel(
        self,
        page,
        *,
        site_no: str,
        target_date: str,
    ) -> None:
        mov_no = str(self._preopen_sentinel_mov_no or "").strip()
        if not mov_no or not site_no or not target_date:
            return

        # Once the date has been observed this endpoint has fulfilled its only
        # purpose. Do not keep polling it for the remaining hours/days.
        if self._preopen_sentinel_date_listed is True:
            return

        # A schedule/fingerprint hint already puts us in the fastest useful mode.
        # Avoid extra traffic while that burst is active.
        if self._schedule_burst_active():
            return

        now = time.monotonic()
        if (
            self._preopen_sentinel_last_probe > 0
            and now - self._preopen_sentinel_last_probe
            < self.DATE_SENTINEL_INTERVAL_SECONDS
        ):
            return
        self._preopen_sentinel_last_probe = now

        result = self._fetch_same_origin_json(
            page,
            self._date_sentinel_url(site_no, mov_no),
            timeout_ms=self.DATE_SENTINEL_TIMEOUT_MS,
        )
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, Mapping):
            self._log_sentinel_error(result)
            return

        rows = data.get("data")
        if not isinstance(rows, list):
            rows = []
        published_dates = {
            self._date_digits(item.get("scnYmd"))
            for item in rows
            if isinstance(item, Mapping)
        }
        published_dates.discard("")
        listed = target_date in published_dates
        previous = self._preopen_sentinel_date_listed
        self._preopen_sentinel_date_listed = listed

        target_label = (
            f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:]}"
            if len(target_date) == 8
            else target_date
        )
        if listed and previous is not True:
            self._activate_schedule_burst(
                "영화별 상영일 목록에서 목표 날짜 게시 감지",
                seconds=self.DATE_SENTINEL_BURST_SECONDS,
                log_transition=False,
            )
            self.log(
                f"[CGV][미오픈 sentinel] 목표 날짜 {target_label} 게시 감지 · "
                f"movNo={mov_no} · {self.DATE_SENTINEL_BURST_SECONDS:.0f}초 동안 "
                "0.5초 실제 회차 집중 감시로 전환합니다.",
                "success",
            )
        elif previous is None:
            self.log(
                f"[CGV][미오픈 sentinel] 준비 완료 · movNo={mov_no} · "
                f"목표 날짜 {target_label}는 아직 영화별 상영일 목록에 없습니다. "
                "장기 감시를 계속합니다.",
                "info",
            )

    def _race_schedule(self, page, url: str, concurrency: int) -> dict[str, Any]:
        result = super()._race_schedule(page, url, concurrency)
        if not _PREOPEN_SELECTION_ACTIVE.get() or self.stop_event.is_set():
            return result

        site_no, target_date = self._schedule_target_from_url(url)
        if not site_no or not target_date:
            return result

        payload = (
            result.get("data")
            if result.get("ok") and isinstance(result.get("data"), Mapping)
            else None
        )

        if isinstance(payload, Mapping):
            self._remember_mov_no(
                self._extract_target_mov_no(payload),
                source="목표 날짜 회차 응답",
            )
            # Never delay promotion of a real bookable IMAX row with a secondary
            # sentinel request. The authoritative schedule result wins now.
            if self._payload_has_bookable_target(payload):
                return result

        self._maybe_discover_mov_no(
            page,
            site_no=site_no,
            target_date=target_date,
            target_payload=payload,
        )
        self._maybe_probe_date_sentinel(
            page,
            site_no=site_no,
            target_date=target_date,
        )
        return result
