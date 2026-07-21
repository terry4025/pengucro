from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any, Callable

from pengucro.models import BookingEvent, BookingEventType, BookingResult


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

        if "시도 중" in message:
            error_part = "재시도"
            if "시도 중... (" in message:
                error_part = message.split("시도 중... (", 1)[1].rstrip(")")
            self._record_attempt(error_part)

        if self.log_callback:
            self.log_callback(formatted_message, log_type)

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
        with self._lock:
            self._attempt_count += 1
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
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run_async_tasks(reservation_data, num_tasks))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.log(f"비동기 예약 실행 오류: {exc}", "error")
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
        if hasattr(self, "pre_fetch_sessions_async"):
            await self.pre_fetch_sessions_async(num_tasks, reservation_data)
        try:
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
                    self.log(f"예약 작업 오류: {result}", "error")
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

    def _monitor_threads(self) -> None:
        for worker in self.threads:
            worker.join()
        self.is_running = False
        self.listener_stop.set()
        self.emit_event(BookingEventType.STATE, "stopped")
        self.log("예약 작업이 종료되었습니다.", "info")

    def stop_reservation(self) -> None:
        if not self.is_running:
            return
        self.log("예약 작업을 안전하게 중지하는 중입니다...", "info")
        self.stop_event.set()
        self.listener_stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
