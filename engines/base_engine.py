from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from engines.async_hot_path import AsyncHotPathScheduler
from pengucro import logging_setup
from pengucro.diagnostics import format_exception
from pengucro.models import BookingEvent, BookingEventType, BookingResult

logger = logging.getLogger(__name__)


def _is_benign_proactor_teardown(context: dict[str, Any]) -> bool:
    """Recognise only the harmless Windows socket teardown callback."""

    error = context.get("exception")
    if not isinstance(error, OSError) or getattr(error, "winerror", None) != 10022:
        return False
    return "_call_connection_lost" in str(context.get("handle", "")) or (
        "connection_lost" in str(context.get("message", ""))
    )


def _install_quiet_teardown_handler(loop: asyncio.AbstractEventLoop) -> None:
    previous_handler = loop.get_exception_handler()

    def handler(target_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_benign_proactor_teardown(context):
            return
        if previous_handler is not None:
            previous_handler(target_loop, context)
        else:
            target_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


LogCallback = Callable[[str, str], None]
SuccessCallback = Callable[[], None]
StatusCallback = Callable[[int, str], None]
EventCallback = Callable[[BookingEvent], None]


class SubmissionLock:
    """Lock with compatibility for both `blocking=` and legacy `block=` callers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, *, block: bool | None = None) -> bool:
        if block is not None:
            blocking = block
        return self._lock.acquire(blocking=blocking)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()


class BaseEngine:
    """Common lifecycle and event handling for all booking engines.

    HTTP engines run their async workers in one background event-loop thread. This
    keeps the Tk main thread responsive without the process/queue complexity that
    previously duplicated state and made graceful shutdown unreliable.
    """

    def __init__(
        self,
        log_callback: LogCallback,
        success_callback: SuccessCallback | None = None,
        status_callback: StatusCallback | None = None,
        log_batch_callback: Callable[[list[tuple[str, str]]], None] | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.log_callback = log_callback
        self.success_callback = success_callback
        self.status_callback = status_callback
        self.log_batch_callback = log_batch_callback
        self.event_callback = event_callback

        self.stop_event = threading.Event()
        self.listener_stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.async_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.is_running = False

        self._seen_errors: set[str] = set()
        self._attempt_count = 0
        self._last_error = ""
        self._lock = threading.Lock()
        self._success_lock = threading.Lock()
        self.submission_lock = SubmissionLock()
        self._success_fired = False

    @property
    def attempt_count(self) -> int:
        with self._lock:
            return self._attempt_count

    def emit_event(
        self,
        event_type: BookingEventType,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.event_callback:
            self.event_callback(
                BookingEvent(
                    event_type=event_type,
                    message=message,
                    attempt_count=self.attempt_count,
                    details=details or {},
                )
            )

    def log(self, message: str, log_type: str = "info") -> None:
        if self.stop_event.is_set() and any(token in message for token in ("시도 중", "연결 오류", "통신 에러")):
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"

        # Persist at the engine boundary so file I/O stays off the Tk event
        # consumer and each line retains its concrete engine class.
        logging_setup.persist_log_line(self.__class__.__name__, message, log_type)

        # ``silent_tick`` already counted the attempt before emitting its first
        # visible warning line.  Do not count that diagnostic line a second time.
        if "시도 중" in message and not message.startswith("⚠️ "):
            error_part = "재시도"
            if "시도 중... (" in message:
                error_part = message.split("시도 중... (", 1)[1].rstrip(")")
            self._record_attempt(error_part)

        if self.log_callback:
            try:
                self.log_callback(formatted_message, log_type)
            except UnicodeEncodeError as exc:
                # A Windows CP949 console cannot encode warning emoji or some
                # punctuation. Logging is observational and must never abort an
                # already-running reservation flow, so retry once with a
                # callback-safe representation in the failing encoding.
                safe_message = (
                    formatted_message.replace("⚠️", "[주의]")
                    .replace("⚠", "[주의]")
                    .replace("—", "-")
                )
                try:
                    encoding = str(exc.encoding or "ascii")
                    safe_message = safe_message.encode(
                        encoding, errors="replace"
                    ).decode(encoding, errors="replace")
                except (LookupError, ValueError):
                    safe_message = safe_message.encode(
                        "ascii", errors="replace"
                    ).decode("ascii")
                try:
                    self.log_callback(safe_message, log_type)
                except Exception:
                    pass
            except Exception:
                # UI/console logging callbacks are non-authoritative. A broken
                # renderer must not cancel seat monitoring or checkout.
                pass

        event_type = {
            "success": BookingEventType.SUCCESS,
            "error": BookingEventType.ERROR,
            "warning": BookingEventType.WARNING,
        }.get(log_type, BookingEventType.INFO)
        self.emit_event(event_type, message)

    def _record_attempt(self, error_message: str) -> None:
        with self._lock:
            self._attempt_count += 1
            self._last_error = error_message
            count = self._attempt_count
        if self.status_callback:
            self.status_callback(count, error_message)
        self.emit_event(BookingEventType.ATTEMPT, error_message)

    def silent_tick(self, error_message: str) -> None:
        self.silent_ticks(1, error_message)

    def silent_ticks(self, count: int, error_message: str) -> None:
        """Record a burst of equivalent attempts with one UI/event update."""

        count = max(1, int(count))
        with self._lock:
            self._attempt_count += count
            self._last_error = error_message
            count = self._attempt_count
            is_new = error_message not in self._seen_errors
            if is_new:
                self._seen_errors.add(error_message)

        if self.status_callback:
            self.status_callback(count, error_message)
        self.emit_event(BookingEventType.ATTEMPT, error_message)
        if is_new:
            self.log(f"⚠️ {error_message} — 재시도 중...", "warning")

    def notify_success(self, result: BookingResult | None = None) -> bool:
        """Fire success exactly once, even when workers finish simultaneously."""
        with self._success_lock:
            if self._success_fired:
                return False
            self._success_fired = True
            self.stop_event.set()

        if result:
            self.emit_event(
                BookingEventType.SUCCESS,
                result.message,
                details={"booking_number": result.booking_number, **dict(result.details)},
            )
        if self.success_callback:
            self.success_callback()
        return True

    def get_csrf_token(self, session: Any, url: str | None = None) -> str:
        raise NotImplementedError("Subclasses must implement get_csrf_token")

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        raise NotImplementedError("Subclasses must implement make_reservation_thread")

    async def make_reservation_async_task(self, reservation_data: dict[str, Any], task_idx: int) -> None:
        raise NotImplementedError("Subclasses must implement make_reservation_async_task")

    def start_reservation(self, reservation_data: dict[str, Any], num_threads: int, is_async: bool = False) -> None:
        # Protect non-GUI/legacy entry points as well. MainWindow replaces the
        # per-run registry earlier; this idempotent registration closes the gap
        # for callers that instantiate an engine directly.
        logging_setup.register_sensitive_mapping(reservation_data)
        if self.is_running:
            self.log("예약 엔진이 이미 실행 중입니다.", "warning")
            return
        if num_threads < 1:
            self.log("동시 시도 수는 1 이상이어야 합니다.", "error")
            return

        self.is_running = True
        self.stop_event.clear()
        self.listener_stop.clear()
        self.threads = []
        self._attempt_count = 0
        self._last_error = ""
        self._seen_errors.clear()
        self._success_fired = False
        self.emit_event(BookingEventType.STATE, "running")

        if is_async:
            self.log(f"{num_threads}개의 비동기 작업으로 예약을 시작합니다.", "info")
            self.async_thread = threading.Thread(
                target=self._run_async_loop,
                args=(reservation_data, num_threads),
                name=f"{self.__class__.__name__}AsyncLoop",
                daemon=True,
            )
            self.async_thread.start()
            return

        self.log(f"{num_threads}개의 작업 스레드로 예약을 시작합니다.", "info")
        for index in range(num_threads):
            worker = threading.Thread(
                target=self.make_reservation_thread,
                args=(reservation_data,),
                name=f"BookingThread-{index + 1}",
                daemon=True,
            )
            self.threads.append(worker)
            worker.start()
        monitor = threading.Thread(target=self._monitor_threads, name="BookingThreadMonitor", daemon=True)
        monitor.start()

    def _run_async_loop(self, reservation_data: dict[str, Any], num_tasks: int) -> None:
        if hasattr(asyncio, "ProactorEventLoop"):
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        _install_quiet_teardown_handler(loop)
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run_async_tasks(reservation_data, num_tasks))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.log(f"비동기 예약 실행 오류: {format_exception(exc)}", "error")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = None
            self.is_running = False
            self.listener_stop.set()
            self.emit_event(BookingEventType.STATE, "stopped")
            self.log("예약 작업이 종료되었습니다.", "info")

    async def run_async_tasks(
        self,
        reservation_data: dict[str, Any],
        num_tasks: int,
        start_idx_offset: int = 0,
    ) -> None:
        self.async_submission_lock = asyncio.Lock()
        self.async_csrf_lock = asyncio.Lock()
        if getattr(self, "USE_ASYNC_HOT_PATH", False):
            self.async_request_scheduler = AsyncHotPathScheduler(num_tasks)
            self.async_csrf_semaphore = asyncio.Semaphore(max(1, min(num_tasks, 8)))
            self.log(
                f"비동기 연속 스캔 시작 · 최대 동시 요청 {num_tasks}개 · "
                f"초기 요청 간격 {self.async_request_scheduler.spacing_seconds * 1000:.1f}ms · "
                "응답 완료 슬롯 즉시 재투입",
                "info",
            )
        try:
            if hasattr(self, "pre_fetch_sessions_async"):
                await self.pre_fetch_sessions_async(num_tasks, reservation_data)
            workers = [
                asyncio.create_task(
                    self.make_reservation_async_task(reservation_data, start_idx_offset + index),
                    name=f"booking-worker-{start_idx_offset + index + 1}",
                )
                for index in range(num_tasks)
            ]
            results = await asyncio.gather(*workers, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception) and not self.stop_event.is_set():
                    self.log(f"예약 작업 오류: {format_exception(result)}", "error")
        finally:
            await self._close_session_pool()

    async def _close_session_pool(self) -> None:
        pool = getattr(self, "session_pool", [])
        for item in pool:
            session = item[0] if isinstance(item, tuple) else item
            try:
                result = session.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                continue
        if hasattr(self, "session_pool"):
            self.session_pool = []
        connector = getattr(self, "_shared_connector", None)
        if connector is not None:
            try:
                result = connector.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            self._shared_connector = None

    async def wait_async_scan_turn(self) -> float:
        scheduler = getattr(self, "async_request_scheduler", None)
        if scheduler is None:
            return 0.0
        return await scheduler.wait_turn(self.stop_event)

    def observe_async_response(self, response, rtt_ms: float) -> float:
        scheduler = getattr(self, "async_request_scheduler", None)
        if scheduler is None:
            return 0.0
        return scheduler.observe_response(
            int(getattr(response, "status", 0) or 0),
            rtt_ms,
            getattr(response, "headers", None),
        )

    def observe_async_network_failure(self) -> float:
        scheduler = getattr(self, "async_request_scheduler", None)
        if scheduler is None:
            return 0.0
        return scheduler.observe_network_failure()

    # How long workers get to wind down *after* stop_event has been set. This is
    # not a limit on how long a run may take: a booking that opens tomorrow waits
    # for a day and that is normal. Without any bound, though, a worker stuck on
    # a socket kept is_running True forever, so MainWindow never ran its
    # completion handler and the CTA button stayed disabled on "중지 중...".
    SHUTDOWN_GRACE_SECONDS = 15.0

    def _monitor_threads(self) -> None:
        # The grace period applies only *after* a stop has been requested.
        #
        # An earlier version computed the deadline the moment this monitor
        # started, which is when the engine starts -- so any run that legitimately
        # waited longer than the grace period (a booking that opens tomorrow, for
        # instance) was declared stalled after 15 seconds and had its state torn
        # down while the worker was still doing its job.
        stragglers: list[str] = []
        stop_deadline: float | None = None
        while True:
            alive = [worker for worker in self.threads if worker.is_alive()]
            if not alive:
                break
            if self.stop_event.is_set():
                if stop_deadline is None:
                    stop_deadline = time.monotonic() + self.SHUTDOWN_GRACE_SECONDS
                elif time.monotonic() >= stop_deadline:
                    stragglers = [worker.name for worker in alive]
                    break
            for worker in alive:
                worker.join(timeout=0.2)
                if self.stop_event.is_set() and stop_deadline is not None \
                        and time.monotonic() >= stop_deadline:
                    break

        # The state is released either way. A daemon thread that outlives this
        # point cannot block interpreter shutdown, and leaving the GUI wedged is
        # a worse failure than reporting an unclean stop.
        self.is_running = False
        self.listener_stop.set()
        self.emit_event(BookingEventType.STATE, "stopped")
        if stragglers:
            self.log(
                f"일부 작업 스레드가 제한 시간 내에 종료되지 않았습니다 "
                f"({len(stragglers)}개). 상태를 초기화합니다.",
                "warning",
            )
            logger.warning("Workers did not exit within grace period: %s", ", ".join(stragglers))
        self.log("예약 작업이 종료되었습니다.", "info")

    def stop_reservation(self) -> None:
        if not self.is_running:
            return
        self.log("예약 작업을 안전하게 중지하는 중입니다...", "info")
        self.stop_event.set()
        self.listener_stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
