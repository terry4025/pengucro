"""Background collector for Keyescape's currently published timetables."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from engines.keyescape_coordination import _pid_alive
from engines.keyescape_schedule_cache import remember_slot_template
from pengucro.storage import data_path, load_json, save_json


AUTO_COLLECTION_STATE_FILE = "keyescape_auto_collection.json"
AUTO_COLLECTION_CLAIM_FILE = "keyescape_auto_collection.claim"
AUTO_COLLECTION_INTERVAL_SECONDS = 6 * 60 * 60
AUTO_COLLECTION_CLAIM_STALE_SECONDS = 15 * 60
_CANCELLED = object()


class KeyescapeAutoCollectionLease:
    """Allow only one local program to run the low-priority full collector."""

    def __init__(self, interval_seconds: float = AUTO_COLLECTION_INTERVAL_SECONDS):
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.claim_path = data_path(AUTO_COLLECTION_CLAIM_FILE)
        self.acquired = False

    def is_due(self, now: float | None = None) -> bool:
        state = load_json(AUTO_COLLECTION_STATE_FILE, {})
        try:
            last_success = float(state.get("last_success", 0.0))
        except (AttributeError, TypeError, ValueError):
            last_success = 0.0
        return float(time.time() if now is None else now) - last_success >= self.interval_seconds

    def acquire(self) -> bool:
        if not self.is_due():
            return False
        self.claim_path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                handle = os.open(
                    self.claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                try:
                    claim = json.loads(self.claim_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    claim = {}
                try:
                    pid = int(claim.get("pid") or 0)
                    created = float(claim.get("created") or 0.0)
                except (AttributeError, TypeError, ValueError):
                    pid, created = 0, 0.0
                stale = time.time() - created > AUTO_COLLECTION_CLAIM_STALE_SECONDS
                if _pid_alive(pid) and not stale:
                    return False
                try:
                    self.claim_path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                return False
            try:
                os.write(
                    handle,
                    json.dumps(
                        {"pid": os.getpid(), "created": time.time()},
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            finally:
                os.close(handle)
            self.acquired = True
            # Another process may have completed between our due check and
            # claim acquisition. Avoid repeating a full-site read in that case.
            if not self.is_due():
                self.release(success=False)
                return False
            return True
        return False

    def release(self, *, success: bool) -> None:
        if not self.acquired:
            return
        if success:
            try:
                save_json(
                    AUTO_COLLECTION_STATE_FILE,
                    {"last_success": time.time(), "pid": os.getpid()},
                )
            except OSError:
                pass
        try:
            self.claim_path.unlink()
        except OSError:
            pass
        self.acquired = False


@dataclass(frozen=True)
class KeyescapeThemeTarget:
    branch_id: str
    branch_name: str
    theme_num: str
    info_num: str
    theme_name: str
    doing: int


@dataclass(frozen=True)
class KeyescapeCacheProgress:
    phase: str
    completed: int = 0
    total: int = 0
    branch_count: int = 0
    theme_count: int = 0
    saved_count: int = 0
    unavailable_count: int = 0
    failed_count: int = 0


@dataclass(frozen=True)
class KeyescapeCacheResult:
    branch_count: int
    theme_count: int
    request_count: int
    saved_count: int
    unavailable_count: int
    failed_count: int
    coverage: dict[str, int]
    cancelled: bool = False


class KeyescapeTimetableCollector:
    """Collect real A/B/C/D timetable rows without opening booking pages."""

    GROUP_LIMITS = {"A": 2, "B": 1, "C": 1, "D": 1}

    def __init__(
        self,
        base_url: str = "https://www.keyescape.com",
        *,
        timeout: float = 10.0,
        max_workers: int = 3,
        request_interval: float = 0.12,
    ) -> None:
        value = str(base_url or "https://www.keyescape.com").rstrip("/")
        for suffix in ("/reservation.php", "/reservation2.php"):
            if value.lower().endswith(suffix):
                value = value[:-len(suffix)]
                break
        self.base_url = value
        self.api_url = urllib.parse.urljoin(f"{value}/", "controller/run_proc.php")
        self.reservation_url = urllib.parse.urljoin(f"{value}/", "reservation.php")
        self.timeout = max(1.0, float(timeout))
        self.max_workers = max(1, min(int(max_workers), 3))
        self.request_interval = max(0.05, float(request_interval))
        self._rate_lock = threading.Lock()
        self._next_request_at = 0.0
        self._thread_local = threading.local()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.reservation_url,
            "Origin": self.base_url,
        })
        return session

    def _worker_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._new_session()
            self._thread_local.session = session
        return session

    def _wait_rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            delay = max(0.0, self._next_request_at - now)
            if delay:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + self.request_interval

    def _post(self, session: requests.Session, payload: dict) -> dict:
        last_error = None
        for attempt in range(2):
            try:
                self._wait_rate_limit()
                response = session.post(
                    self.api_url, data=payload, timeout=self.timeout
                )
                response.raise_for_status()
                value = response.json()
                return value if isinstance(value, dict) else {}
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
        raise RuntimeError(type(last_error).__name__ if last_error else "request failed")

    def _discover_catalog(
        self, cancel_event: threading.Event | None = None
    ) -> tuple[int, list[KeyescapeThemeTarget]]:
        session = self._new_session()
        response = session.get(self.reservation_url, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        options = [
            (option.get_text(" ", strip=True), str(option.get("value", "")))
            for option in soup.select('select[name="zizum"] option')
            if str(option.get("value", "")).strip()
        ]
        targets = []
        for branch_name, branch_id in options:
            if cancel_event is not None and cancel_event.is_set():
                break
            payload = self._post(session, {
                "t": "get_theme_info_list",
                "zizum_num": branch_id,
            })
            for row in payload.get("data") or [] if payload.get("status") else []:
                info_num = str(row.get("info_num", "") or "")
                theme_num = str(
                    row.get("level_num", row.get("theme_num", "")) or ""
                )
                theme_name = str(row.get("info_name", "") or "").strip()
                try:
                    doing = max(0, min(int(row.get("doing") or 0), 14))
                except (TypeError, ValueError):
                    doing = 0
                if info_num and theme_num and theme_name and doing:
                    targets.append(KeyescapeThemeTarget(
                        branch_id, branch_name, theme_num, info_num,
                        theme_name, doing,
                    ))
        return len(options), targets

    def _server_day(self, target: KeyescapeThemeTarget) -> date:
        payload = self._post(self._new_session(), {
            "t": "get_theme_date",
            "num": target.info_num,
        })
        raw = str((payload.get("calendarData") or {}).get("today") or "")
        return datetime.strptime(raw, "%Y-%m-%d").date()

    @classmethod
    def candidate_dates(cls, server_day: date, doing: int) -> list[date]:
        selected: dict[str, list[date]] = {key: [] for key in cls.GROUP_LIMITS}
        published = [server_day + timedelta(days=offset) for offset in range(doing)]
        for day in reversed(published):
            group = ("A", "A", "A", "A", "B", "C", "D")[day.weekday()]
            if len(selected[group]) < cls.GROUP_LIMITS[group]:
                selected[group].append(day)
        return sorted(
            (day for values in selected.values() for day in values),
            reverse=True,
        )

    def _fetch_slots(self, target: KeyescapeThemeTarget, source_day: date):
        payload = self._post(self._worker_session(), {
            "t": "get_theme_time",
            "date": source_day.isoformat(),
            "zizumNum": target.branch_id,
            "themeNum": target.theme_num,
            "endDay": "0",
        })
        if not payload.get("status") or not payload.get("data"):
            return None
        return list(payload["data"])

    def collect(
        self, progress_callback=None, cancel_event: threading.Event | None = None
    ) -> KeyescapeCacheResult:
        callback = progress_callback if callable(progress_callback) else lambda _value: None
        callback(KeyescapeCacheProgress(phase="catalog"))
        branch_count, targets = (
            self._discover_catalog()
            if cancel_event is None
            else self._discover_catalog(cancel_event)
        )
        if cancel_event is not None and cancel_event.is_set():
            return KeyescapeCacheResult(
                branch_count, len(targets), 0, 0, 0, 0,
                {group: 0 for group in ("A", "B", "C", "D")},
                cancelled=True,
            )
        if not targets:
            raise RuntimeError("키이스케이프 테마 목록을 찾지 못했습니다.")
        server_day = self._server_day(targets[0])
        tasks = [
            (target, source_day)
            for target in targets
            for source_day in self.candidate_dates(server_day, target.doing)
        ]
        total = len(tasks)
        callback(KeyescapeCacheProgress(
            phase="timetables", total=total,
            branch_count=branch_count, theme_count=len(targets),
        ))
        saved = 0
        unavailable = 0
        failed = 0
        completed = 0
        covered_themes: set[tuple[str, str, str]] = set()

        def fetch_if_active(target, source_day):
            if cancel_event is not None and cancel_event.is_set():
                return _CANCELLED
            return self._fetch_slots(target, source_day)

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="KeyescapeCache",
        ) as executor:
            futures = {
                executor.submit(fetch_if_active, target, source_day):
                (target, source_day)
                for target, source_day in tasks
            }
            for future in as_completed(futures):
                target, source_day = futures[future]
                try:
                    slots = future.result()
                    if slots is _CANCELLED:
                        stored = False
                    elif slots is None:
                        stored = False
                        unavailable += 1
                    else:
                        stored = bool(slots) and remember_slot_template(
                            self.base_url,
                            source_day.isoformat(),
                            target.branch_id,
                            target.theme_num,
                            slots,
                        )
                        if not stored:
                            failed += 1
                except Exception:
                    stored = False
                    failed += 1
                completed += 1
                if stored:
                    saved += 1
                    group = ("A", "A", "A", "A", "B", "C", "D")[
                        source_day.weekday()
                    ]
                    covered_themes.add((group, target.branch_id, target.theme_num))
                callback(KeyescapeCacheProgress(
                    phase="timetables", completed=completed, total=total,
                    branch_count=branch_count, theme_count=len(targets),
                    saved_count=saved, unavailable_count=unavailable,
                    failed_count=failed,
                ))
        return KeyescapeCacheResult(
            branch_count=branch_count,
            theme_count=len(targets),
            request_count=total,
            saved_count=saved,
            unavailable_count=unavailable,
            failed_count=failed,
            coverage={
                group: sum(item[0] == group for item in covered_themes)
                for group in ("A", "B", "C", "D")
            },
            cancelled=bool(cancel_event is not None and cancel_event.is_set()),
        )
