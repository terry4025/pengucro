import threading
import time
from datetime import datetime

class BaseEngine:
    def __init__(self, log_callback, success_callback=None, status_callback=None):
        """
        Base engine for escape room bookings.
        
        :param log_callback: A function taking (message, type) to log messages.
                             Types: 'info', 'success', 'error', 'warning'
        :param success_callback: A function to call when booking is successful.
        :param status_callback: A function taking (attempt_count, last_error) to
                                silently update the UI status badge without logging.
        """
        self.log_callback = log_callback
        self.success_callback = success_callback
        self.status_callback = status_callback
        self.stop_event = threading.Event()
        self.threads = []
        self.is_running = False
        self._seen_errors = set()
        self._attempt_count = 0
        self._last_error = ""
        self._lock = threading.Lock()
        self.submission_lock = threading.Lock()

    def log(self, message, log_type='info'):
        # Discard trailing attempts/retries/errors after stop event is set to prevent logging after success or stop
        if self.stop_event.is_set() and ("시도 중" in message or "연결 오류" in message or "오류" in message):
            return
            
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        if self.log_callback:
            self.log_callback(formatted_message, log_type)
        else:
            print(f"[{log_type.upper()}] {formatted_message}")

    def silent_tick(self, error_message):
        """Count an attempt without flooding the log. Only logs if error type is new."""
        with self._lock:
            self._attempt_count += 1
            self._last_error = error_message
            is_new = error_message not in self._seen_errors
            if is_new:
                self._seen_errors.add(error_message)

        # Notify status badge
        if self.status_callback:
            self.status_callback(self._attempt_count, error_message)

        # Log only the first occurrence of each unique error type
        if is_new:
            self.log(f"⚠️ {error_message} — 재시도 중...", "warning")

    def get_csrf_token(self, session, url):
        raise NotImplementedError("Subclasses must implement get_csrf_token")

    def make_reservation_thread(self, reservation_data):
        raise NotImplementedError("Subclasses must implement make_reservation_thread")

    async def make_reservation_async_task(self, reservation_data, task_idx):
        raise NotImplementedError("Subclasses must implement make_reservation_async_task")

    def start_reservation(self, reservation_data, num_threads, is_async=False):
        if self.is_running:
            self.log("Booking engine is already running.", "warning")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.threads = []
        
        if is_async:
            self.log(f"Starting booking attempt with {num_threads} async tasks...", "info")
            t = threading.Thread(
                target=self.start_async_reservation_loop,
                args=(reservation_data, num_threads),
                name="AsyncBookingThread"
            )
            t.daemon = True
            self.threads.append(t)
            t.start()
        else:
            self.log(f"Starting booking attempt with {num_threads} threads...", "info")
            for i in range(num_threads):
                t = threading.Thread(
                    target=self.make_reservation_thread, 
                    args=(reservation_data,),
                    name=f"BookingThread-{i+1}"
                )
                t.daemon = True
                self.threads.append(t)
                t.start()

        # Monitor thread in a separate thread so GUI doesn't freeze
        monitor_thread = threading.Thread(target=self._monitor_threads)
        monitor_thread.daemon = True
        monitor_thread.start()

    def start_async_reservation_loop(self, reservation_data, num_tasks):
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.run_async_tasks(reservation_data, num_tasks))
        except Exception as e:
            self.log(f"Async loop error: {e}", "error")
        finally:
            loop.close()

    async def run_async_tasks(self, reservation_data, num_tasks):
        import asyncio
        self.async_submission_lock = asyncio.Lock()
        
        tasks = []
        for i in range(num_tasks):
            tasks.append(asyncio.create_task(self.make_reservation_async_task(reservation_data, i)))
            
        await asyncio.gather(*tasks)

    def _monitor_threads(self):
        for t in self.threads:
            t.join()
        self.is_running = False
        self.log("All booking threads have stopped.", "info")

    def stop_reservation(self):
        if not self.is_running:
            self.log("Booking engine is not running.", "warning")
            return
        
        self.log("Stopping all booking threads...", "info")
        self.stop_event.set()
