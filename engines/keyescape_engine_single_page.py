"""Keyescape runtime reliability extensions.

The base engine owns the actual booking flow.  This wrapper deliberately keeps
that submission path intact and concentrates the newer safety/reliability work
here:

* one requested page always means one browser page and one captcha;
* live target-date validation runs on the read-only HTTP pipeline in parallel
  with browser pre-warming instead of being sequenced behind it;
* timing history records the boundary-fetch and observed-ready delays without
  moving the trusted Fast Path earlier;
* followers in a multi-process slot lookup use a timing-aware rendezvous budget
  before falling back to their own read;
* a proven trusted/live slot-id mismatch quarantines only the risky one-template
  weekend relaxation until a two-date match rebuilds confidence.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from engines.keyescape_engine import KeyescapeEngine as _BaseKeyescapeEngine
from pengucro.storage import load_json, save_json


class KeyescapeEngine(_BaseKeyescapeEngine):
    """Keyescape engine that preserves user page-count and Fast Path semantics."""

    TRUSTED_HEALTH_FILE = "keyescape_trusted_health.json"
    TRUSTED_MISMATCH_QUARANTINE_DAYS = 14
    SHARED_WAIT_MIN_SECONDS = 0.65
    SHARED_WAIT_MARGIN_SECONDS = 0.15
    TIMING_PROFILE_MIN_SAMPLES = 3
    TIMING_PROFILE_MAX_SAMPLES = 12

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._single_page_http_fallback = False

    # ------------------------------------------------------------------
    # User-visible page semantics
    # ------------------------------------------------------------------
    def start_reservation(self, reservation_data, num_threads, is_async=False):
        result = super().start_reservation(
            reservation_data, num_threads, is_async=is_async
        )
        if not self.is_running:
            return result
        if getattr(self, "_cancel_watch_state", None) is not None:
            # Cancellation watches use only fresh live rows, never Fast Path.
            return result
        requested = int(num_threads or 1)
        page_count = max(1, min(requested, self.MAX_STANDBY_PAGES))
        if page_count == 1:
            self.log(
                "[정보] 페이지 역할 · 1번 페이지가 빠른 제출을 담당하고, "
                "실제 슬롯 확인은 추가 탭·추가 캡차 없이 별도 HTTP 연결에서 병렬 검증합니다.",
                "info",
            )
        else:
            self.log(
                f"[정보] 페이지 역할 · 1번 페이지는 검증 시간표 Fast Path를 사용할 수 있고, "
                f"2~{page_count}번 페이지는 실제 슬롯 확인 결과를 공유받는 독립 백업 페이지입니다.",
                "info",
            )
        return result

    def _ensure_parallel_live_fallback(self, already_open: bool) -> bool:
        """Keep an explicit one-page run at one page and arm HTTP validation."""
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

    # ------------------------------------------------------------------
    # Trusted-template health
    # ------------------------------------------------------------------
    @staticmethod
    def _target_time_from_live_state(state) -> str:
        key = str((state or {}).get("timing_key", "") or "")
        parts = key.split(":")
        if len(parts) < 4:
            return ""
        return f"{parts[-2]}:{parts[-1]}"

    def _trusted_health_key(self, target_date, zizum_num, theme_num) -> str:
        try:
            target_day = datetime.strptime(str(target_date), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return ""
        return (
            f"{self.site_url.lower()}|{zizum_num}|{theme_num}|"
            f"{self._schedule_group(target_day)}"
        )

    def _record_trusted_mismatch(
        self,
        target_date: str,
        zizum_num: str,
        theme_num: str,
        trusted_id: str,
        live_id: str,
    ) -> None:
        key = self._trusted_health_key(target_date, zizum_num, theme_num)
        if not key:
            return
        health = load_json(self.TRUSTED_HEALTH_FILE, {"version": 1, "entries": {}})
        if not isinstance(health, dict):
            health = {"version": 1, "entries": {}}
        entries = health.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            health["entries"] = entries
        previous = entries.get(key, {})
        previous = previous if isinstance(previous, dict) else {}
        entries[key] = {
            "mismatch_count": int(previous.get("mismatch_count", 0) or 0) + 1,
            "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_date": str(target_date),
            "trusted_id": str(trusted_id),
            "live_id": str(live_id),
        }
        try:
            save_json(self.TRUSTED_HEALTH_FILE, health)
        except OSError:
            pass

    def _clear_trusted_mismatch(
        self, target_date: str, zizum_num: str, theme_num: str
    ) -> None:
        key = self._trusted_health_key(target_date, zizum_num, theme_num)
        if not key:
            return
        health = load_json(self.TRUSTED_HEALTH_FILE, {"version": 1, "entries": {}})
        entries = health.get("entries", {}) if isinstance(health, dict) else {}
        if not isinstance(entries, dict) or key not in entries:
            return
        entries.pop(key, None)
        try:
            save_json(self.TRUSTED_HEALTH_FILE, health)
        except OSError:
            pass

    def _single_template_quarantined(
        self, target_date: str, zizum_num: str, theme_num: str
    ) -> bool:
        key = self._trusted_health_key(target_date, zizum_num, theme_num)
        if not key:
            return False
        health = load_json(self.TRUSTED_HEALTH_FILE, {"entries": {}})
        entries = health.get("entries", {}) if isinstance(health, dict) else {}
        row = entries.get(key, {}) if isinstance(entries, dict) else {}
        if not isinstance(row, dict) or not row.get("observed_at"):
            return False
        try:
            observed = datetime.fromisoformat(str(row["observed_at"]))
            age_seconds = (
                datetime.now().astimezone() - observed.astimezone()
            ).total_seconds()
        except (TypeError, ValueError):
            return False
        return 0 <= age_seconds <= self.TRUSTED_MISMATCH_QUARANTINE_DAYS * 86400

    def _trusted_slot_from_cache(
        self, target_date, target_time, zizum_num, theme_num
    ) -> tuple[str, tuple[str, ...]]:
        slot_id, sources = super()._trusted_slot_from_cache(
            target_date, target_time, zizum_num, theme_num
        )
        if not slot_id:
            return "", ()
        if len(sources) >= 2:
            # Two matching published dates are the stronger proof and rebuild
            # confidence after any prior one-template mismatch.
            self._clear_trusted_mismatch(target_date, zizum_num, theme_num)
            return slot_id, sources
        if len(sources) == 1 and self._single_template_quarantined(
            target_date, zizum_num, theme_num
        ):
            self._log_throttled(
                "trusted_single_template_quarantine",
                "[정보] 이전 실행에서 검증 시간표 ID와 실제 ID 불일치가 관측되어 "
                "주말 1개 날짜 Fast Path를 잠시 보류합니다. 2개 날짜 일치가 확인되면 자동 복구됩니다.",
                "warning",
                interval=30.0,
            )
            return "", ()
        return slot_id, sources

    # ------------------------------------------------------------------
    # Timing telemetry / conservative timing adaptation
    # ------------------------------------------------------------------
    @staticmethod
    def _percentile(values, ratio: float) -> float:
        ordered = sorted(float(value) for value in values)
        if not ordered:
            return 0.0
        if len(ordered) == 1:
            return ordered[0]
        position = max(0.0, min(1.0, float(ratio))) * (len(ordered) - 1)
        low = int(position)
        high = min(len(ordered) - 1, low + 1)
        fraction = position - low
        return ordered[low] * (1.0 - fraction) + ordered[high] * fraction

    @classmethod
    def _timing_parameters(cls, samples):
        hedge_delay, retry_delay, read_lead = super()._timing_parameters(samples)
        valid_rtts = []
        for sample in samples or []:
            try:
                value = float(sample.get("read_rtt_ms", 0.0)) / 1000.0
            except (AttributeError, TypeError, ValueError):
                continue
            if value > 0:
                valid_rtts.append(value)
        if len(valid_rtts) < 3:
            return hedge_delay, retry_delay, read_lead

        # Keep retry/hedge behavior from the proven base model.  Only make the
        # first read a little more conservative using p75 instead of a median,
        # which changes observation timing but never moves the booking POST.
        p75_rtt = cls._percentile(valid_rtts, 0.75)
        read_lead = min(
            cls.SLOT_READ_LEAD_MAX_SECONDS,
            max(cls.SLOT_READ_LEAD_MIN_SECONDS, read_lead, p75_rtt / 2.0),
        )
        return hedge_delay, retry_delay, read_lead

    def _load_timing_profile(self, zizum_num, theme_num, target_time):
        """Fill sparse exact profiles from progressively broader recent history."""
        key = self._timing_key(zizum_num, theme_num, target_time)
        history = load_json(self.TIMING_HISTORY_FILE, {})
        if not isinstance(history, dict):
            history = {}

        selected = []
        used_keys = set()

        def extend(profile_keys):
            for profile_key in profile_keys:
                if profile_key in used_keys:
                    continue
                used_keys.add(profile_key)
                entry = history.get(profile_key, {})
                samples = entry.get("samples", []) if isinstance(entry, dict) else []
                for sample in samples:
                    if isinstance(sample, dict):
                        selected.append(sample)
                if len(selected) >= self.TIMING_PROFILE_MAX_SAMPLES:
                    return

        extend([key])
        exact_count = len(selected)
        if len(selected) < self.TIMING_PROFILE_MIN_SAMPLES:
            prefix = f"{zizum_num}:{theme_num}:"
            extend(sorted(candidate for candidate in history if candidate.startswith(prefix)))
        if len(selected) < self.TIMING_PROFILE_MIN_SAMPLES:
            prefix = f"{zizum_num}:"
            extend(sorted(candidate for candidate in history if candidate.startswith(prefix)))
        if len(selected) < self.TIMING_PROFILE_MIN_SAMPLES:
            extend(sorted(history))

        selected = selected[-self.TIMING_PROFILE_MAX_SAMPLES:]
        hedge_delay, retry_delay, read_lead = self._timing_parameters(selected)
        return key, hedge_delay, retry_delay, read_lead, bool(exact_count or selected)

    async def _fetch_live_slots(self, target_date, zizum_num, theme_num, target_time):
        started = time.monotonic()
        started_delta = self._t0_delta_ms()
        try:
            return await super()._fetch_live_slots(
                target_date, zizum_num, theme_num, target_time
            )
        finally:
            state = self._live_slot_state if isinstance(self._live_slot_state, dict) else {}
            state.setdefault(
                "boundary_fetch_started_ms",
                None if started_delta is None else round(float(started_delta), 1),
            )
            state.setdefault(
                "boundary_fetch_elapsed_ms",
                round((time.monotonic() - started) * 1000.0, 1),
            )

    def _remember_slot_timing(self, state):
        super()._remember_slot_timing(state)
        sample = state.get("timing_sample")
        if not isinstance(sample, dict):
            return
        sample.setdefault("observed_at_epoch", round(time.time(), 3))
        current_delta = self._t0_delta_ms()
        if current_delta is not None:
            sample.setdefault("observed_ready_delay_ms", round(max(0.0, current_delta), 1))
        for key in (
            "boundary_fetch_started_ms",
            "boundary_fetch_elapsed_ms",
            "shared_wait_timeout_ms",
        ):
            if state.get(key) is not None:
                sample.setdefault(key, state[key])

        trusted_id = str(self._trusted_slot_id or "")
        live_id = str(state.get("slot_id") or "")
        if (
            trusted_id
            and live_id
            and trusted_id != live_id
            and not state.get("trusted_mismatch_recorded")
        ):
            target_date = str(state.get("target_date", "") or "")
            zizum_num = str(state.get("zizum_num", "") or "")
            theme_num = str(state.get("theme_num", "") or "")
            if all((target_date, zizum_num, theme_num)):
                self._record_trusted_mismatch(
                    target_date, zizum_num, theme_num, trusted_id, live_id
                )
                state["trusted_mismatch_recorded"] = True

    def _adaptive_shared_wait_seconds(self) -> float:
        state = self._live_slot_state or {}
        try:
            read_lead = float(state.get("read_lead") or self.SLOT_READ_LEAD_SECONDS)
        except (TypeError, ValueError):
            read_lead = self.SLOT_READ_LEAD_SECONDS
        # read_lead is normally roughly half the learned RTT.  Give the elected
        # reader about 1.35x that expected RTT plus a fixed safety margin.  The
        # existing 1.5 s cap remains the absolute maximum.
        expected_rtt = max(0.20, read_lead * 2.0)
        timeout = expected_rtt * 1.35 + self.SHARED_WAIT_MARGIN_SECONDS
        return min(
            self.SHARED_SLOT_WAIT_SECONDS,
            max(self.SHARED_WAIT_MIN_SECONDS, timeout),
        )

    async def _fetch_coordinated_live_slots(
        self, target_date, zizum_num, theme_num, target_time
    ):
        share = self._slot_share
        if share is None or share.owner:
            return await super()._fetch_coordinated_live_slots(
                target_date, zizum_num, theme_num, target_time
            )

        wait_timeout = self._adaptive_shared_wait_seconds()
        state = self._live_slot_state or {}
        state["shared_wait_timeout_ms"] = round(wait_timeout * 1000.0, 1)
        wait_started = time.monotonic()
        slots = await asyncio.get_running_loop().run_in_executor(
            None, lambda: share.wait_for_result(wait_timeout)
        )
        waited = time.monotonic() - wait_started
        if slots:
            state["last_rtt"] = waited
            self._trace_timing(
                f"다른 실행의 동일 시간표 응답 수신 · 공유 대기 {waited * 1000.0:.1f}ms"
            )
            return slots

        self._trace_timing(
            f"공유 시간표 응답이 {wait_timeout * 1000.0:.0f}ms 내 없어 "
            "이 실행의 독립 조회로 전환",
            "warning",
        )
        self._slot_share = None
        return await self._fetch_live_slots(
            target_date, zizum_num, theme_num, target_time
        )

    # ------------------------------------------------------------------
    # Independent one-page HTTP validation
    # ------------------------------------------------------------------
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
        """Observe the target date over HTTP while page 1 keeps the Fast Path."""
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
        """Run browser/socket prewarm and one-page HTTP validation independently."""
        validator_task = None
        if (
            self._single_page_http_fallback
            and not self.stop_event.is_set()
            and self._page_count == 1
            and self._trusted_slot_id
        ):
            # Start this task immediately.  It waits on its own read window, so a
            # slow Chrome prewarm cannot delay the target-date HTTP observation.
            validator_task = asyncio.create_task(self._validate_trusted_slot_http())

        try:
            await super()._prewarm_near_open(page)
        finally:
            if validator_task is not None:
                try:
                    await validator_task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log_throttled(
                        "single_page_http_validator_task",
                        f"[경고] 1페이지 HTTP 검증 작업 종료 오류: {exc}",
                        "warning",
                        interval=2.0,
                    )
