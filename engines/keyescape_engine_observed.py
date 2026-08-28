"""Keyescape runtime additions for clock and browser-network observability.

This layer intentionally leaves the proven booking gate and final submit routine
untouched.  It only hardens clock re-sync quality, warms the exact booking origin
from Chrome, and records resource timing for the real booking POST.
"""

from __future__ import annotations

import time

from engines.keyescape_engine_single_page import (
    KeyescapeEngine as _ReliabilityKeyescapeEngine,
)


class KeyescapeEngine(_ReliabilityKeyescapeEngine):
    """Reliability wrapper with conservative clock/network diagnostics."""

    CLOCK_PRECISE_MAX_AGE_SECONDS = 300.0
    CLOCK_REGRESSION_RATIO = 1.5
    CLOCK_REGRESSION_ABSOLUTE_SECONDS = 0.010

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._browser_prewarm_metrics: dict = {}

    # ------------------------------------------------------------------
    # Server-clock quality
    # ------------------------------------------------------------------
    def _has_recent_precise_clock_sample(self) -> bool:
        intervals = getattr(self.clock, "_mapping_intervals", None)
        if not isinstance(intervals, list) or not intervals:
            return False
        try:
            newest = max(float(sample[2]) for sample in intervals)
        except (TypeError, ValueError, IndexError):
            return False
        age = time.monotonic() - newest
        return 0.0 <= age <= self.CLOCK_PRECISE_MAX_AGE_SECONDS

    def _sync_server_clock(self, announce=False):
        """Never replace a recent precise mapping with a much coarser re-sync.

        The base ServerClock may legitimately fall back to a ~500 ms estimate if
        it misses the next whole-second Date-header boundary.  Near an opening,
        that fallback is worse than keeping a recently pinned 20-50 ms mapping.
        We therefore snapshot the good mapping first and restore it only when the
        new result regresses substantially.  The number of clock requests and the
        booking fire point are unchanged.
        """
        before_snapshot = self.clock.snapshot()
        try:
            before_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            before_precision = None
        try:
            before_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            before_offset = None

        recent_precise = bool(
            before_snapshot
            and before_precision is not None
            and before_precision < 0.5
            and self._has_recent_precise_clock_sample()
        )

        ok = super()._sync_server_clock(announce=announce)

        try:
            measured_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            measured_precision = None
        try:
            measured_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            measured_offset = None

        preserved = False
        if (
            ok
            and recent_precise
            and measured_precision is not None
            and before_precision is not None
        ):
            regression_threshold = max(
                before_precision * self.CLOCK_REGRESSION_RATIO,
                before_precision + self.CLOCK_REGRESSION_ABSOLUTE_SECONDS,
            )
            if measured_precision > regression_threshold:
                preserved = bool(
                    self.clock.apply_snapshot(before_snapshot, max_age=5.0)
                )

        try:
            final_precision = float(self.clock.last_precision)
        except (TypeError, ValueError):
            final_precision = measured_precision
        try:
            final_offset = float(self.clock.last_offset)
        except (TypeError, ValueError):
            final_offset = measured_offset

        # Initial sync already has its own user-facing message.  Only add the
        # detailed line for the final near-open re-sync and only from page 1 so a
        # three-page run does not print the same diagnostics three times.
        if not announce and getattr(self, "_page_index", 1) == 1:
            remaining = None
            if self.open_at is not None:
                try:
                    remaining = self.clock.seconds_until(self.open_at)
                except Exception:
                    remaining = None
            if (
                remaining is not None
                and -1.0 <= remaining <= self.FINAL_SYNC_LEAD + 2.0
            ):
                before_ms = (
                    f"{before_precision * 1000.0:.1f}"
                    if before_precision is not None else "?"
                )
                final_ms = (
                    f"{final_precision * 1000.0:.1f}"
                    if final_precision is not None else "?"
                )
                if before_offset is not None and final_offset is not None:
                    offset_delta = (final_offset - before_offset) * 1000.0
                    offset_text = f"{offset_delta:+.1f}ms"
                else:
                    offset_text = "확인 불가"
                mode = (
                    "새 측정이 더 거칠어 기존 정밀 매핑 유지"
                    if preserved else
                    "최신 측정 반영"
                )
                self.log(
                    "[정보] 최종 서버 시각 보정 · 정밀도 "
                    f"{before_ms}→{final_ms}ms · 오프셋 변화 {offset_text} · {mode}",
                    "info",
                )
        return ok

    # ------------------------------------------------------------------
    # Chrome connection warm-up
    # ------------------------------------------------------------------
    async def _prewarm_browser_connection(self, page) -> bool:
        """Warm both the page origin and the exact booking controller in Chrome."""
        if page is None:
            return False
        try:
            result = await page.evaluate(
                """async (urls) => {
                    const warm = async (baseUrl, label) => {
                        const target = new URL(baseUrl, location.href);
                        target.searchParams.set('pg_prewarm', `${label}-${Date.now()}-${Math.random()}`);
                        const requestUrl = target.href;
                        const started = performance.now();
                        try {
                            const response = await fetch(requestUrl, {
                                method: 'HEAD',
                                cache: 'no-store',
                                credentials: 'include',
                            });
                            // HEAD has no useful body, but consuming it makes the
                            // resource lifecycle settle before timing is read.
                            await response.arrayBuffer();
                            await new Promise((resolve) => setTimeout(resolve, 0));
                            const entries = performance.getEntriesByName(requestUrl);
                            const entry = entries.length ? entries[entries.length - 1] : null;
                            const numberOrMinusOne = (value) =>
                                Number.isFinite(value) ? Number(value) : -1;
                            return {
                                reached: true,
                                status: response.status,
                                duration: entry ? numberOrMinusOne(entry.duration)
                                                : performance.now() - started,
                                dnsStart: entry ? numberOrMinusOne(entry.domainLookupStart) : -1,
                                dnsEnd: entry ? numberOrMinusOne(entry.domainLookupEnd) : -1,
                                connectStart: entry ? numberOrMinusOne(entry.connectStart) : -1,
                                connectEnd: entry ? numberOrMinusOne(entry.connectEnd) : -1,
                                secureConnectionStart: entry
                                    ? numberOrMinusOne(entry.secureConnectionStart) : -1,
                            };
                        } catch (error) {
                            return {
                                reached: false,
                                status: 0,
                                duration: performance.now() - started,
                                dnsStart: -1,
                                dnsEnd: -1,
                                connectStart: -1,
                                connectEnd: -1,
                                secureConnectionStart: -1,
                            };
                        }
                    };

                    // Sequential requests are intentional: the first establishes
                    // the origin connection and the second touches the exact
                    // controller used by the final POST so Chrome can reuse the
                    // already-open same-origin socket when possible.
                    const reservation = await warm(urls.reservationUrl, 'page');
                    const controller = await warm(urls.apiUrl, 'controller');
                    return {
                        networkReached: Boolean(reservation.reached || controller.reached),
                        reservation,
                        controller,
                    };
                }""",
                {
                    "reservationUrl": f"{self.site_url}/reservation.php",
                    "apiUrl": self.api_url,
                },
            )
        except Exception:
            self._browser_prewarm_metrics = {}
            return False

        result = result if isinstance(result, dict) else {}
        self._browser_prewarm_metrics = result
        controller = result.get("controller")
        controller = controller if isinstance(controller, dict) else {}
        if controller.get("reached"):
            try:
                duration = float(controller.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            connect_ms = self._timing_duration(
                controller, "connectStart", "connectEnd"
            )
            tls_ms = self._timing_duration(
                controller, "secureConnectionStart", "connectEnd"
            )
            details = [f"HTTP {int(controller.get('status', 0) or 0)}", f"{duration:.1f}ms"]
            if connect_ms is not None:
                details.append(f"연결설정 {connect_ms:.1f}ms")
            if tls_ms is not None:
                details.append(f"TLS {tls_ms:.1f}ms")
            self.log(
                "[정보] Chrome 예약 endpoint 예열 · " + " · ".join(details),
                "info",
            )
        return bool(result.get("networkReached"))

    # ------------------------------------------------------------------
    # Final POST resource timing
    # ------------------------------------------------------------------
    @staticmethod
    def _timing_duration(values, start_key: str, end_key: str):
        if not isinstance(values, dict):
            return None
        try:
            start = float(values.get(start_key, -1))
            end = float(values.get(end_key, -1))
        except (TypeError, ValueError):
            return None
        if start < 0 or end < 0 or end < start:
            return None
        return end - start

    @staticmethod
    def _is_booking_post(request) -> bool:
        try:
            if "/controller/run_proc.php" not in str(request.url or ""):
                return False
            if str(request.method or "").upper() != "POST":
                return False
            post_data = request.post_data or ""
            if callable(post_data):
                post_data = post_data()
            return "ins_rev" in str(post_data)
        except Exception:
            return False

    @staticmethod
    def _format_network_metric(label: str, value):
        if value is None:
            return None
        return f"{label} {float(value):.1f}ms"

    async def _prepare_page(self, page, dialog_state):
        """Keep base success handling and add non-invasive network telemetry."""
        await super()._prepare_page(page, dialog_state)

        def handle_request_started(request):
            if not self._is_booking_post(request):
                return
            dialog_state["request_started"] = True
            dialog_state["request_started_monotonic"] = time.monotonic()
            self._trace_timing("예약 POST 브라우저 전송 시작")

        async def handle_request_finished(request):
            if not self._is_booking_post(request):
                return
            dialog_state["request_finished"] = True
            try:
                timing = request.timing
                if callable(timing):
                    timing = timing()
            except Exception:
                timing = {}
            timing = timing if isinstance(timing, dict) else {}

            dns_ms = self._timing_duration(
                timing, "domainLookupStart", "domainLookupEnd"
            )
            connect_ms = self._timing_duration(
                timing, "connectStart", "connectEnd"
            )
            tls_ms = self._timing_duration(
                timing, "secureConnectionStart", "connectEnd"
            )
            first_byte_ms = self._timing_duration(
                timing, "requestStart", "responseStart"
            )
            receive_ms = self._timing_duration(
                timing, "responseStart", "responseEnd"
            )
            try:
                total_ms = float(timing.get("responseEnd", -1))
                if total_ms < 0:
                    total_ms = None
            except (TypeError, ValueError):
                total_ms = None

            metrics = {
                "dns_ms": dns_ms,
                "connect_ms": connect_ms,
                "tls_ms": tls_ms,
                "request_to_first_byte_ms": first_byte_ms,
                "response_receive_ms": receive_ms,
                "total_ms": total_ms,
            }
            dialog_state["network_timing"] = metrics

            rendered = []
            for label, value in (
                ("DNS", dns_ms),
                ("연결설정", connect_ms),
                ("TLS", tls_ms),
                ("요청→첫바이트", first_byte_ms),
                ("응답수신", receive_ms),
                ("전체", total_ms),
            ):
                part = self._format_network_metric(label, value)
                if part:
                    rendered.append(part)
            if rendered:
                self._trace_timing(
                    "예약 POST 네트워크 상세 · " + " · ".join(rendered)
                )

        async def handle_request_failed(request):
            if not self._is_booking_post(request):
                return
            try:
                failure = request.failure
                if callable(failure):
                    failure = failure()
            except Exception:
                failure = ""
            dialog_state["request_failed"] = True
            dialog_state["request_failure"] = str(failure or "")
            self._trace_timing(
                "예약 POST 네트워크 실패"
                + (f" · {str(failure)[:120]}" if failure else ""),
                "warning",
            )

        page.on("request", handle_request_started)
        page.on("requestfinished", handle_request_finished)
        page.on("requestfailed", handle_request_failed)
