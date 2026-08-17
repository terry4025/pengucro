"""Single-page preserving Keyescape runtime.

The base engine's trusted schedule fast path used to promote an explicitly
requested one-page run to two browser pages so the second page could retain a
live target-date lookup and an independent captcha token.  This wrapper keeps
that safety observation on the existing read-only HTTP slot pipeline instead:
one requested page means one browser page and one captcha, while the live slot
lookup still runs concurrently with the trusted fast submit path.
"""

from __future__ import annotations

import asyncio

from engines.keyescape_engine import KeyescapeEngine as _BaseKeyescapeEngine


class KeyescapeEngine(_BaseKeyescapeEngine):
    """Keyescape engine that treats the requested standby page count as a limit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._single_page_http_fallback = False

    def _ensure_parallel_live_fallback(self, already_open: bool) -> bool:
        """Keep an explicit one-page run at one page and arm HTTP validation.

        The base implementation changes ``_page_count`` from 1 to 2 when a
        trusted schedule template exists.  The live target-date lookup does not
        require a browser, form or captcha, so use the already pre-warmed HTTP
        sessions instead and leave the user's page count untouched.
        """
        if not self._trusted_slot_id or already_open or self._page_count != 1:
            self._single_page_http_fallback = False
            return False
        self._single_page_http_fallback = True
        self.log(
            "[정보] 1페이지 설정 유지 · 검증 시간표 빠른 제출과 실제 슬롯 확인을 "
            "별도 HTTP 연결로 병렬 처리합니다.",
            "info",
        )
        return True

    @staticmethod
    def _target_time_from_live_state(state) -> str:
        key = str((state or {}).get("timing_key", "") or "")
        parts = key.split(":")
        if len(parts) < 4:
            return ""
        return f"{parts[-2]}:{parts[-1]}"

    async def _wait_for_http_validation_window(self) -> None:
        """Wait until the same early-read point used by the live slot pipeline."""
        if self.open_at is None:
            return
        timer_active = self._begin_high_resolution_timer()
        try:
            while not self.stop_event.is_set():
                remaining = self.clock.seconds_until(self.open_at)
                read_lead = self._live_slot_read_lead()
                if remaining <= read_lead:
                    return
                gap = remaining - read_lead
                await asyncio.sleep(0.02 if gap > 0.25 else 0.001)
        finally:
            if timer_active:
                self._end_high_resolution_timer()

    def _publish_http_validation_to_workers(
        self, slot_id: str, sources: tuple[str, ...]
    ) -> None:
        """Update the single page's trusted candidate without creating a tab."""
        self._trusted_slot_id = slot_id
        self._trusted_slot_sources = sources
        for worker in self._page_workers:
            worker._trusted_slot_id = slot_id
            worker._trusted_slot_sources = sources

    async def _validate_trusted_slot_http(self) -> None:
        """Observe the target date over HTTP while page 1 keeps the fast path.

        This is intentionally non-blocking with respect to the browser submit.
        If the target-date API answers before the trusted fire point, a newer
        live id can replace the cached id (or a confirmed capacity result can
        suppress it).  If the API is congested, the trusted fast path still fires
        at the same time as before; the HTTP result remains useful for recovery.
        """
        state = self._live_slot_state or {}
        target_date = str(state.get("target_date", "") or "")
        zizum_num = str(state.get("zizum_num", "") or "")
        theme_num = str(state.get("theme_num", "") or "")
        target_time = self._target_time_from_live_state(state)
        if not all((target_date, zizum_num, theme_num, target_time)):
            return

        trusted_before = str(self._trusted_slot_id or "")
        if not trusted_before:
            return

        await self._wait_for_http_validation_window()
        if self.stop_event.is_set() or self._page_success_event.is_set():
            return

        try:
            live_slot_id, live_status = await self._resolve_live_slot(
                target_date,
                target_time,
                zizum_num,
                theme_num,
                trusted_before,
            )
        except Exception as exc:
            self._log_throttled(
                "single_page_http_fallback",
                f"[경고] 1페이지 HTTP 슬롯 검증 실패: {exc}",
                "warning",
                interval=2.0,
            )
            return

        live_slot_id = str(live_slot_id or "")
        if live_status == "ready" and live_slot_id:
            if live_slot_id == trusted_before:
                self._trace_timing(
                    f"1페이지 HTTP 실시간 검증 완료 · ID {live_slot_id} 일치"
                )
                return
            self.log(
                "[경고] 검증 시간표 슬롯 ID와 실제 슬롯 ID가 달라 "
                f"실시간 ID {live_slot_id}를 우선 사용합니다. "
                f"(검증 ID {trusted_before})",
                "warning",
            )
            self._publish_http_validation_to_workers(
                live_slot_id, ("실시간 HTTP 검증",)
            )
            return

        if live_status == "capacity":
            self.log(
                "[정보] 1페이지 HTTP 실시간 검증에서 대상 슬롯의 마감을 확인했습니다. "
                "검증 시간표 선발사를 중단합니다.",
                "info",
            )
            self._publish_http_validation_to_workers("", ())

    async def _prewarm_near_open(self, page=None):
        """Pre-warm as before, then keep the read-only HTTP validator running."""
        await super()._prewarm_near_open(page)
        if (
            not self._single_page_http_fallback
            or self.stop_event.is_set()
            or self._page_count != 1
            or not self._trusted_slot_id
        ):
            return
        await self._validate_trusted_slot_http()
