import threading
import time
from datetime import datetime
import multiprocessing

def child_process_run(engine_class_name, site_url, reservation_data, num_tasks, stop_event, log_queue, success_event, is_shin=False):
    try:
        import asyncio
        import sys
        from engines.zeroworld_engine import ZeroWorldEngine
        from engines.jigubyeol_engine import JigubyeolEngine
        from engines.keyescape_engine import KeyescapeEngine
        
        classes = {
            'ZeroWorldEngine': ZeroWorldEngine,
            'JigubyeolEngine': JigubyeolEngine,
            'KeyescapeEngine': KeyescapeEngine
        }
        
        engine_class = classes[engine_class_name]
        
        def child_log(message, log_type='info'):
            log_queue.put(('log', message, log_type))
            if "시도 중" in message:
                error_part = "재시도"
                if "시도 중... (" in message:
                    error_part = message.split("시도 중... (")[1].rstrip(")")
                log_queue.put(('tick', 1, error_part))
                
        def child_success():
            success_event.set()
            log_queue.put(('success',))
            
        if engine_class_name == 'ZeroWorldEngine':
            engine = engine_class(site_url, child_log, child_success, is_shin=is_shin)
        else:
            engine = engine_class(child_log, child_success, site_url)
            
        engine.stop_event = stop_event
        engine.is_running = True
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(engine.run_async_tasks(reservation_data, num_tasks))
        except Exception as e:
            child_log(f"Child process async loop error: {e}", "error")
        finally:
            loop.close()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            log_queue.put(('log', f"CRITICAL Child Process Boot Error: {e}\n{tb}", "error"))
        except Exception:
            pass

class BaseEngine:
    def __init__(self, log_callback, success_callback=None, status_callback=None, log_batch_callback=None):
        """
        Base engine for escape room bookings.
        
        :param log_callback: A function taking (message, type) to log messages.
                             Types: 'info', 'success', 'error', 'warning'
        :param success_callback: A function to call when booking is successful.
        :param status_callback: A function taking (attempt_count, last_error) to
                                silently update the UI status badge without logging.
        :param log_batch_callback: A function taking (list of (message, type)) to log in batches.
        """
        self.log_callback = log_callback
        self.success_callback = success_callback
        self.status_callback = status_callback
        self.log_batch_callback = log_batch_callback
        self.stop_event = threading.Event()
        self.threads = []
        self.processes = []
        self.is_running = False
        self._seen_errors = set()
        self._attempt_count = 0
        self._last_error = ""
        self._lock = threading.Lock()
        self.submission_lock = threading.Lock()
        self.listener_stop = threading.Event()

    def log(self, message, log_type='info'):
        # Discard trailing attempts/retries/errors after stop event is set to prevent logging after success or stop
        if self.stop_event.is_set() and ("시도 중" in message or "연결 오류" in message or "오류" in message):
            return
            
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        
        # Accumulate tick count on attempts
        if "시도 중" in message:
            error_part = "재시도"
            if "시도 중... (" in message:
                error_part = message.split("시도 중... (")[1].rstrip(")")
            with self._lock:
                self._attempt_count += 1
                self._last_error = error_part
            if self.status_callback:
                self.status_callback(self._attempt_count, error_part)
                
        if self.log_callback:
            self.log_callback(formatted_message, log_type)

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
        self.listener_stop.clear()
        self.threads = []
        self.processes = []
        self._attempt_count = 0
        self._seen_errors = set()
        
        if is_async:
            self.multiprocess_stop_event = multiprocessing.Event()
            self.multiprocess_success_event = multiprocessing.Event()
            self.log_queue = multiprocessing.Queue()
            
            # Divide work across processes (up to CPU core count, max 4)
            num_proc = min(multiprocessing.cpu_count(), 4)
            if num_threads < num_proc:
                num_proc = num_threads
            tasks_per_proc = num_threads // num_proc
            remainder = num_threads % num_proc
            
            self.log(f"Starting booking attempt with {num_threads} async tasks across {num_proc} processes...", "info")
            
            # Start Queue Listener Thread
            self.queue_thread = threading.Thread(target=self._queue_listener, name="QueueListenerThread")
            self.queue_thread.daemon = True
            self.queue_thread.start()
            
            class_name = self.__class__.__name__
            site_url = self.site_url if hasattr(self, "site_url") else self.base_url
            is_shin = getattr(self, "is_shin", False)
            for i in range(num_proc):
                p_tasks = tasks_per_proc + (1 if i < remainder else 0)
                if p_tasks <= 0:
                    continue
                p = multiprocessing.Process(
                    target=child_process_run,
                    args=(
                        class_name,
                        site_url,
                        reservation_data,
                        p_tasks,
                        self.multiprocess_stop_event,
                        self.log_queue,
                        self.multiprocess_success_event,
                        is_shin
                    ),
                    name=f"BookingProcess-{i+1}"
                )
                p.daemon = True
                self.processes.append(p)
                p.start()
                
            # Monitor processes
            monitor_thread = threading.Thread(target=self._monitor_processes)
            monitor_thread.daemon = True
            monitor_thread.start()
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
                
            # Monitor threads
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
        
        # Pre-fetch if available in subclass
        if hasattr(self, "pre_fetch_sessions_async"):
            await self.pre_fetch_sessions_async(num_tasks, reservation_data)
            
        tasks = []
        for i in range(num_tasks):
            tasks.append(asyncio.create_task(self.make_reservation_async_task(reservation_data, i)))
            
        await asyncio.gather(*tasks)

    def _monitor_threads(self):
        for t in self.threads:
            t.join()
        self.is_running = False
        self.listener_stop.set()
        self.log("All booking threads have stopped.", "info")
        
    def _monitor_processes(self):
        for p in self.processes:
            p.join()
        self.is_running = False
        self.listener_stop.set()
        self.log("All booking processes have stopped.", "info")

    def _queue_listener(self):
        import queue
        while self.is_running or not self.listener_stop.is_set():
            try:
                item = self.log_queue.get(timeout=0.1)
                itype = item[0]
                if itype == 'log':
                    _, msg, ltype = item
                    self.log(msg, ltype)
                elif itype == 'tick':
                    _, inc, error_msg = item
                    with self._lock:
                        self._attempt_count += inc
                        self._last_error = error_msg
                    if self.status_callback:
                        self.status_callback(self._attempt_count, error_msg)
                elif itype == 'success':
                    if self.success_callback:
                        self.success_callback()
            except queue.Empty:
                pass
            except Exception:
                break

    def stop_reservation(self):
        if not self.is_running:
            self.log("Booking engine is not running.", "warning")
            return
        
        self.log("Stopping all booking processes/threads...", "info")
        self.stop_event.set()
        if hasattr(self, "multiprocess_stop_event"):
            self.multiprocess_stop_event.set()
            
        # Terminate processes
        for p in self.processes:
            if p.is_alive():
                p.terminate()
                
        self.listener_stop.set()
