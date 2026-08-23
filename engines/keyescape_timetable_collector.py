"""Background collector for Keyescape's currently published timetables."""

from __future__ import annotations

import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

from engines.keyescape_schedule_cache import remember_slot_template


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

    def _discover_catalog(self) -> tuple[int, list[KeyescapeThemeTarget]]:
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

    def collect(self, progress_callback=None) -> KeyescapeCacheResult:
        callback = progress_callback if callable(progress_callback) else lambda _value: None
        callback(KeyescapeCacheProgress(phase="catalog"))
        branch_count, targets = self._discover_catalog()
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

        with ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="KeyescapeCache",
        ) as executor:
            futures = {
                executor.submit(self._fetch_slots, target, source_day):
                (target, source_day)
                for target, source_day in tasks
            }
            for future in as_completed(futures):
                target, source_day = futures[future]
                try:
                    slots = future.result()
                    if slots is None:
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
        )
