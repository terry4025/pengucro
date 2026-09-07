from __future__ import annotations

import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from engines import browser_session
from engines.cgv_login import CgvLoginAssistant
from engines.cgv_client import (
    CGV_COMPANY_CODE,
    CGV_HOME_URL,
    CgvAccessBlocked,
    CgvError,
    CgvRegion,
    CgvSeat,
    CgvSite,
    normalize_time,
    parse_api_seats,
    parse_imax_site_nos,
    parse_imax_unit_content_relation_no,
    parse_site_catalog,
    schedule_items,
)


class CgvRequestCancelled(Exception):
    """Raised when a background CGV browser client request has been cancelled."""
    pass


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CgvRequestCancelled("CGV 조회 요청이 취소되었습니다.")


class CgvLoginRequired(CgvError):
    pass


@dataclass(frozen=True)
class CgvCatalogSnapshot:
    regions: tuple[CgvRegion, ...]
    sites: tuple[CgvSite, ...]


def _screening_date(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 8:
        raise CgvError("CGV 조회 날짜를 YYYY-MM-DD 형식으로 입력해주세요.")
    return digits


def _booking_payload(schedule: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "coCd", "siteNo", "scnsNo", "scnYmd", "scnSseq", "scnsrtTm",
        "scnendTm", "prodNo", "salsTznCd", "movkndCd", "tcscnsGradCd",
        "sascnsGradCd", "movTirCd", "siteGradCd", "srvltKindCd", "movfNo",
        "prdcmpTypCd", "prdtypCd", "prddtlTypCd", "dblfrNo", "dblfrRpsntYn",
        "videoAddexpCd", "sbtdivCd", "bzplcNo", "cxprdYn", "scnsGradCd",
        "speclIndctTypCd", "prcrulDivCd", "cratgClsCd", "cndSalYnList",
        "vatincYn", "slddKindCd", "iceconYn", "arthsYn", "srlsYn",
        "childnMovYn", "movNo", "movNm", "prmddNo", "prodImg",
        "movkndDsplEnm", "expoProdNm",
    )
    payload = {key: schedule.get(key, "") for key in keys}
    payload["coCd"] = payload.get("coCd") or CGV_COMPANY_CODE
    payload["salsTznCd"] = payload.get("salsTznCd") or "26"
    payload["soldierJoinStus"] = "N"
    return payload


def _movie_name(item: Mapping[str, Any]) -> str:
    return str(item.get("expoProdNm") or item.get("movNm") or item.get("prodNm") or "").strip()


def _auditorium_name(item: Mapping[str, Any]) -> str:
    return str(item.get("expoScnsNm") or item.get("scnsNm") or "").strip()


def _format_name(item: Mapping[str, Any]) -> str:
    return str(item.get("movkndDsplEnm") or item.get("movkndDsplNm") or "").strip()


def _is_imax_text(text: str) -> bool:
    return "IMAX" in str(text or "").upper()


def _is_imax_schedule(item: Mapping[str, Any]) -> bool:
    fields = (
        item.get("expoScnsNm"),
        item.get("scnsNm"),
        item.get("movkndDsplEnm"),
        item.get("movkndDsplNm"),
        item.get("hallNm"),
    )
    return any(_is_imax_text(str(f or "")) for f in fields)


def _is_equivalent_screening(
    item: Mapping[str, Any], exact_schedules: Iterable[Mapping[str, Any]]
) -> bool:
    """Check if an equivalent real schedule already exists on the target date.

    A normal 2D screening on the target date must NOT suppress an IMAX historical
    candidate. Only when an actual matching IMAX screening for that movie exists
    on the target date should the equivalent historical template be suppressed.
    """
    item_movie = re.sub(r"\s+", "", _movie_name(item)).casefold()
    if not item_movie:
        return False
    item_is_imax = _is_imax_schedule(item)
    item_auditorium = re.sub(r"\s+", "", _auditorium_name(item)).casefold()
    item_format = re.sub(r"\s+", "", _format_name(item)).casefold()

    for exact in exact_schedules:
        exact_movie = re.sub(r"\s+", "", _movie_name(exact)).casefold()
        if not exact_movie or exact_movie != item_movie:
            continue
        exact_is_imax = _is_imax_schedule(exact)
        if item_is_imax and not exact_is_imax:
            # Target date only has a non-IMAX screening for this movie; keep IMAX candidate!
            continue
        if not item_is_imax and exact_is_imax:
            continue
        exact_auditorium = re.sub(r"\s+", "", _auditorium_name(exact)).casefold()
        exact_format = re.sub(r"\s+", "", _format_name(exact)).casefold()
        if item_auditorium and exact_auditorium and item_auditorium != exact_auditorium:
            continue
        if item_format and exact_format and item_format != exact_format:
            continue
        return True
    return False


def _schedule_identity(schedule: Mapping[str, Any]) -> tuple[str, str, str]:
    def compact(*keys: str) -> str:
        value = next((schedule.get(key) for key in keys if schedule.get(key)), "")
        return re.sub(r"\s+", "", str(value)).casefold()

    return (
        compact("expoProdNm", "movNm", "prodNm"),
        compact("expoScnsNm", "scnsNm"),
        compact("movkndDsplEnm", "movkndDsplNm"),
    )


def _seat_reference_for(
    target: Mapping[str, Any], references: tuple[dict[str, Any], ...]
) -> dict[str, Any] | None:
    target_movie, target_auditorium, target_format = _schedule_identity(target)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for reference in references:
        movie, auditorium, format_name = _schedule_identity(reference)
        score = 0
        if target_auditorium and auditorium == target_auditorium:
            score += 8
        if target_format and format_name == target_format:
            score += 4
        if target_movie and movie == target_movie:
            score += 2
        if str(reference.get("scnsNo", "")) == str(target.get("scnsNo", "")):
            score += 1
        ranked.append((score, reference))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked[0][0] >= 8 else None


def _aggregate_historical_candidates(
    history_by_date: Mapping[str, tuple[dict[str, Any], ...]],
    target_date: str,
    exact_schedules: tuple[dict[str, Any], ...],
    site_no: str,
) -> tuple[dict[str, Any], ...]:
    """Aggregate unique candidate movies and times across recent published dates.

    Movies already published on the target date are excluded here so real
    target-date schedules always have absolute priority.
    """
    all_history_items = [
        item for items in history_by_date.values() for item in items
    ]
    if not all_history_items:
        return ()

    target_digits = re.sub(r"\D", "", target_date)

    # Group by (canonical_movie_name, auditorium_name, format_name)
    groups: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    display_names: dict[tuple[str, str, str], tuple[str, str, str]] = {}

    for item in all_history_items:
        # Strictly IMAX records only for candidate discovery
        if not _is_imax_schedule(item):
            continue
        movie = _movie_name(item)
        if not movie:
            continue
        if _is_equivalent_screening(item, exact_schedules):
            # Already published on target date! Real schedule has priority.
            continue
        auditorium = _auditorium_name(item)
        format_name = _format_name(item)
        canon_movie = re.sub(r"\s+", "", movie).casefold()
        canon_auditorium = re.sub(r"\s+", "", auditorium).casefold()
        canon_format = re.sub(r"\s+", "", format_name).casefold()
        group_key = (canon_movie, canon_auditorium, canon_format)
        if group_key not in display_names:
            display_names[group_key] = (movie, auditorium, format_name)
        raw_time = normalize_time(item.get("scnsrtTm"))
        if not raw_time or len(raw_time) < 4:
            continue
        date_str = str(item.get("scnYmd") or "")
        groups.setdefault(group_key, {}).setdefault(raw_time, []).append(date_str)

    candidates: list[dict[str, Any]] = []
    for group_key, times_dict in groups.items():
        movie, auditorium, format_name = display_names[group_key]
        sample_target = {
            "expoProdNm": movie,
            "expoScnsNm": auditorium,
            "movkndDsplEnm": format_name,
        }
        seat_ref = _seat_reference_for(sample_target, tuple(all_history_items))
        ref_date = ""
        if seat_ref:
            raw_ymd = str(seat_ref.get("scnYmd", ""))
            if len(raw_ymd) == 8:
                ref_date = f"{raw_ymd[:4]}-{raw_ymd[4:6]}-{raw_ymd[6:]}"

        for time_digits, dates in sorted(times_dict.items(), key=lambda pair: pair[0]):
            unique_dates = tuple(dict.fromkeys(d for d in dates if d))
            candidates.append(
                {
                    "siteNo": str(site_no),
                    "scnYmd": target_digits,
                    "scnsrtTm": time_digits,
                    "expoProdNm": movie,
                    "movNm": movie,
                    "expoScnsNm": auditorium,
                    "scnsNm": auditorium,
                    "movkndDsplEnm": format_name,
                    "movkndDsplNm": format_name,
                    "frSeatCnt": 0,
                    "_pengucroPreopen": True,
                    "_pengucroObservedDates": unique_dates,
                    "_pengucroSeatReference": dict(seat_ref) if seat_ref else None,
                    "_pengucroSeatReferenceDate": ref_date,
                }
            )

    def sort_key(item: dict[str, Any]):
        aud = _auditorium_name(item)
        fmt = _format_name(item)
        is_imax = 0 if _is_imax_text(f"{aud} {fmt}") else 1
        return (is_imax, _movie_name(item), normalize_time(item.get("scnsrtTm")))

    candidates.sort(key=sort_key)
    return tuple(candidates)


class CgvBrowserClient:
    """Read CGV data through the same-origin BFF used by the official web app.

    CGV rejects the legacy public API path with 401.  This client opens the
    official page in a persistent Chrome profile, then performs only the normal
    BFF reads that the page itself performs.  Seat layout reads require the user
    to be logged in; no authentication or access control is bypassed.
    """

    def __init__(self, log: Callable[[str, str], None] | None = None) -> None:
        self.log = log

    def _emit(self, message: str, level: str = "info") -> None:
        if self.log:
            self.log(message, level)

    @staticmethod
    def _matches_navigation_target(current_url: str, target_url: str) -> bool:
        current = urllib.parse.urlsplit(str(current_url or ""))
        target = urllib.parse.urlsplit(str(target_url or ""))
        return (
            current.scheme.lower() == target.scheme.lower()
            and current.netloc.lower() == target.netloc.lower()
            and current.path.rstrip("/") == target.path.rstrip("/")
        )

    def _goto_with_retry(
        self,
        page,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
        timeout: int = 45000,
        attempts: int = 3,
    ):
        """Tolerate CGV's login/React redirects replacing an in-flight navigation."""

        last_error: Exception | None = None
        for attempt in range(max(1, int(attempts))):
            try:
                return page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as exc:
                last_error = exc
                if "ERR_ABORTED" not in str(exc).upper():
                    raise CgvError(
                        "CGV 페이지를 열지 못했습니다. 잠시 후 다시 시도해주세요."
                    ) from exc
                # Chromium reports ERR_ABORTED when a login redirect replaces
                # our navigation. If the intended route actually won, allow
                # it to settle; otherwise retry after the redirect finishes.
                if self._matches_navigation_target(page.url, url):
                    page.wait_for_timeout(700)
                    if self._matches_navigation_target(page.url, url):
                        try:
                            page.wait_for_load_state(wait_until, timeout=5000)
                        except Exception:
                            pass
                        return None
                if attempt + 1 < max(1, int(attempts)):
                    self._emit("CGV 페이지 이동이 겹쳐 자동으로 다시 연결합니다.", "warning")
                    page.wait_for_timeout(700 * (attempt + 1))
                    continue
        raise CgvError(
            "CGV 로그인 후 페이지 이동이 겹쳤습니다. 잠시 뒤 자동 조회를 다시 눌러주세요."
        ) from last_error

    @staticmethod
    def _wait_for_post_login_navigation(page, *, timeout_seconds: int = 12) -> None:
        """Wait until the login form's own redirect is no longer changing the page."""

        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while "/mem/login" in page.url and time.monotonic() < deadline:
            if page.is_closed():
                raise CgvLoginRequired("CGV 로그인 창이 닫혀 좌석도 조회를 중단했습니다.")
            page.wait_for_timeout(250)

        last_url = page.url
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            if page.is_closed():
                raise CgvLoginRequired("CGV 로그인 창이 닫혀 좌석도 조회를 중단했습니다.")
            current_url = page.url
            if current_url != last_url:
                last_url = current_url
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= 0.75:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                return
            page.wait_for_timeout(250)

    @staticmethod
    def _has_member_session(context) -> bool:
        try:
            return any(
                str(cookie.get("name", "")) in {"accessToken", "refresh_token"}
                and bool(cookie.get("value"))
                for cookie in context.cookies(CGV_HOME_URL)
            )
        except Exception:
            return False

    def _wait_for_member_login(
        self,
        page,
        *,
        timeout_seconds: int = 600,
        require_fresh_login: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> None:
        _check_cancel(cancel_event)
        if self._has_member_session(page.context) and not require_fresh_login:
            return
        if "/mem/login" not in page.url:
            self._goto_with_retry(
                page,
                f"{CGV_HOME_URL}/mem/login?nmbrAtktFlag=Y",
                wait_until="domcontentloaded",
                timeout=45000,
            )
        self._emit(
            "열린 CGV Chrome에서 로그인해주세요. 로그인 완료 후 좌석도를 자동으로 계속 불러옵니다.",
            "warning",
        )
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        login_assistant = CgvLoginAssistant(self._emit, cancel_event)
        saw_login_page = "/mem/login" in page.url
        while time.monotonic() < deadline:
            _check_cancel(cancel_event)
            if page.is_closed():
                raise CgvLoginRequired("CGV 로그인 창이 닫혀 좌석도 조회를 중단했습니다.")
            on_login_page = "/mem/login" in page.url
            saw_login_page = saw_login_page or on_login_page
            login_completed = self._has_member_session(page.context) and (
                not require_fresh_login or (saw_login_page and not on_login_page)
            )
            if login_completed:
                self._wait_for_post_login_navigation(page)
                self._emit("CGV 회원 로그인을 확인했습니다. 좌석도 조회를 계속합니다.", "success")
                return
            login_assistant.step(page)
            page.wait_for_timeout(500)
        raise CgvLoginRequired(
            "CGV 로그인 대기 시간이 끝났습니다. 좌석 불러오기를 다시 눌러주세요."
        )

    @staticmethod
    def _is_recoverable_browser_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(
            err in msg
            for err in (
                "targetclosederror",
                "target closed",
                "page closed",
                "browser closed",
                "cdp disconnected",
                "connection closed",
                "session closed",
                "browser has been closed",
            )
        )

    def _with_page(self, operation):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CgvError("CGV 데이터 조회에 필요한 브라우저 모듈을 찾지 못했습니다.") from exc

        last_error: Exception | None = None
        for attempt in range(2):
            chrome = browser_session.start_isolated(log=self._emit)
            if chrome is None:
                raise CgvError("CGV 데이터 조회용 Chrome을 시작하지 못했습니다.")
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.new_page()
                    try:
                        self._goto_with_retry(
                            page, CGV_HOME_URL, wait_until="domcontentloaded", timeout=45000
                        )
                        result = operation(page)
                        if attempt > 0:
                            self._emit("[CGV] 브라우저 자동 복구 성공", "success")
                        return result
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
            except Exception as exc:
                last_error = exc
                if attempt == 0 and self._is_recoverable_browser_error(exc):
                    self._emit("[CGV] 브라우저 연결 끊김 · 1회 자동 복구", "warning")
                    continue
                if attempt > 0:
                    self._emit("[CGV] 브라우저 자동 복구 실패", "error")
                raise
            finally:
                # Keep the persistent Chrome open. If login is required the user can
                # finish it there and press retry without losing the authenticated
                # profile; the slot itself is released for the booking engine.
                chrome.release()
        if last_error:
            raise last_error

    @staticmethod
    def _fetch_json(page, path: str, timeout_ms: int = 10000) -> dict[str, Any]:
        result = page.evaluate(
            """
            async ([path, timeoutMs]) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeoutMs || 10000);
              try {
                const response = await fetch(path, {
                  method: 'GET',
                  credentials: 'include',
                  cache: 'no-store',
                  headers: {'Accept': 'application/json'},
                  signal: controller.signal
                });
                const text = await response.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: response.status, data, text: text.slice(0, 500)};
              } catch (err) {
                if (err && err.name === 'AbortError') {
                  return {status: 408, timeout: true, error: 'TIMEOUT'};
                }
                return {status: 0, error: String(err)};
              } finally {
                clearTimeout(timer);
              }
            }
            """,
            [path, timeout_ms],
        )
        if isinstance(result, dict) and (result.get("timeout") or result.get("status") == 408):
            raise CgvError(f"CGV 데이터 조회 응답 시간이 초과되었습니다. (제한시간 {timeout_ms // 1000}초)")
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
        if status in {401, 403}:
            raise CgvAccessBlocked(
                "CGV 세션 인증이 만료되었습니다. 열린 CGV Chrome에서 로그인 후 다시 시도해주세요."
            )
        if status == 429:
            raise CgvAccessBlocked("CGV 조회가 일시적으로 제한되었습니다. 잠시 뒤 다시 시도해주세요.")
        data = result.get("data") if isinstance(result, dict) else None
        if status < 200 or status >= 300 or not isinstance(data, dict):
            error_msg = result.get("error") if isinstance(result, dict) else ""
            detail = f" - {error_msg}" if error_msg else f" (HTTP {status or '응답 없음'})"
            raise CgvError(f"CGV 데이터 조회에 실패했습니다.{detail}")
        if int(data.get("statusCode", 0) or 0) != 0:
            raise CgvError(str(data.get("statusMessage") or "CGV가 조회 요청을 처리하지 못했습니다."))
        return data

    def _fetch_official_imax_site_nos(
        self,
        page,
        *,
        cancel_event: threading.Event | None = None,
    ) -> frozenset[str]:
        """Read the current IMAX sites from CGV's special-cinema content."""

        _check_cancel(cancel_event)
        component_query = urllib.parse.urlencode(
            {"coCd": CGV_COMPANY_CODE, "sscnsNo": "1"}
        )
        component_payload = self._fetch_json(
            page,
            "/api/v1/common/meta/dsp/sscnsDsp/searchSscnsDspCpotList?"
            f"{component_query}",
        )
        relation_no = parse_imax_unit_content_relation_no(component_payload)
        if not relation_no:
            raise CgvError("CGV IMAX 극장 안내 구성 정보를 찾지 못했습니다.")

        _check_cancel(cancel_event)
        detail_query = urllib.parse.urlencode(
            {"coCd": CGV_COMPANY_CODE, "unitCpotRelNo": relation_no}
        )
        detail_payload = self._fetch_json(
            page,
            "/api/v1/common/meta/dsp/scrDsp/searchScrDspCpotDtl?"
            f"{detail_query}",
        )
        site_nos = parse_imax_site_nos(detail_payload)
        if not site_nos:
            raise CgvError("CGV IMAX 극장 안내의 지점 목록이 비어 있습니다.")
        return site_nos

    def fetch_catalog(
        self, *, imax_only: bool = True, cancel_event: threading.Event | None = None
    ) -> CgvCatalogSnapshot:
        _check_cancel(cancel_event)
        self._emit("[CGV] 지점 목록 조회 시작", "info")

        def operation(page):
            _check_cancel(cancel_event)
            query = urllib.parse.urlencode(
                {"coCd": CGV_COMPANY_CODE, "custNo": "", "lntd": "", "lttd": "", "srchKwrd": ""}
            )
            payload = self._fetch_json(
                page, f"/api/v1/content/site/searchAllRegionAndSite?{query}"
            )
            _check_cancel(cancel_event)
            imax_site_nos: frozenset[str] | None = None
            if imax_only:
                try:
                    imax_site_nos = self._fetch_official_imax_site_nos(
                        page, cancel_event=cancel_event
                    )
                    self._emit(
                        f"[CGV] 공식 IMAX 지점 목록 적용 · {len(imax_site_nos)}개",
                        "info",
                    )
                except CgvError:
                    self._emit(
                        "[CGV] 공식 IMAX 지점 목록을 일시적으로 조회하지 못해 "
                        "내장 목록으로 계속합니다.",
                        "warning",
                    )
            regions, sites = parse_site_catalog(
                payload,
                imax_only=imax_only,
                imax_site_nos=imax_site_nos,
            )
            if imax_only and not sites:
                raise CgvError("CGV IMAX 지점 정보를 확인하지 못했습니다.")
            if not regions or not sites:
                raise CgvError("CGV 지역·지점 목록이 비어 있습니다.")
            return CgvCatalogSnapshot(regions, sites)

        snapshot = self._with_page(operation)
        _check_cancel(cancel_event)
        self._emit(f"[CGV] 지점 목록 조회 완료 · {len(snapshot.sites)}개 지점", "success")
        return snapshot

    def fetch_schedule(
        self,
        site_no: str,
        screening_date: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], ...]:
        _check_cancel(cancel_event)
        date_digits = _screening_date(screening_date)

        def operation(page):
            _check_cancel(cancel_event)
            return self._fetch_schedule_on_page(page, site_no, date_digits)

        result = self._with_page(operation)
        _check_cancel(cancel_event)
        return result

    def fetch_schedule_with_reference(
        self,
        site_no: str,
        screening_date: str,
        *,
        max_reference_days: int = 14,
        cancel_event: threading.Event | None = None,
    ) -> tuple[tuple[dict[str, Any], ...], str, bool]:
        _check_cancel(cancel_event)
        date_digits = _screening_date(screening_date)
        target = datetime.strptime(date_digits, "%Y%m%d").date()
        today = datetime.now().date()
        self._emit(f"[CGV] 시간표 조회 시작 · site={site_no} · date={screening_date}", "info")

        def operation(page):
            _check_cancel(cancel_event)
            exact = self._fetch_schedule_on_page(page, site_no, date_digits)
            _check_cancel(cancel_event)
            history_by_date: dict[str, tuple[dict[str, Any], ...]] = {}
            all_history_items: list[dict[str, Any]] = []

            # Sample bounded recent published dates
            # Check up to max_reference_days backwards, not older than 7 days before today
            max_days = max(1, min(int(max_reference_days), 14))
            for offset in range(1, max_days + 1):
                _check_cancel(cancel_event)
                candidate = target - timedelta(days=offset)
                if candidate < today - timedelta(days=7):
                    break
                items = self._fetch_schedule_on_page(
                    page, site_no, candidate.strftime("%Y%m%d")
                )
                _check_cancel(cancel_event)
                if items:
                    history_by_date[candidate.isoformat()] = items
                    all_history_items.extend(items)
                if len(history_by_date) >= 7:
                    break

            latest_ref_date = next(iter(history_by_date.keys()), "")

            decorated_exact: list[dict[str, Any]] = []
            if exact:
                for schedule in exact:
                    _check_cancel(cancel_event)
                    copied = dict(schedule)
                    reference = _seat_reference_for(copied, tuple(all_history_items))
                    if reference is not None:
                        copied["_pengucroSeatReference"] = dict(reference)
                        raw_ymd = str(reference.get("scnYmd", ""))
                        if len(raw_ymd) == 8:
                            copied["_pengucroSeatReferenceDate"] = f"{raw_ymd[:4]}-{raw_ymd[4:6]}-{raw_ymd[6:]}"
                        else:
                            copied["_pengucroSeatReferenceDate"] = latest_ref_date
                    decorated_exact.append(copied)

            _check_cancel(cancel_event)
            templates = _aggregate_historical_candidates(
                history_by_date, screening_date, tuple(exact or ()), str(site_no)
            )
            combined = tuple(decorated_exact) + tuple(templates)

            _check_cancel(cancel_event)

            if exact:
                return combined, target.isoformat(), False
            if combined:
                return combined, (latest_ref_date or target.isoformat()), True

            raise CgvError(
                "선택한 날짜의 시간표가 아직 열리지 않았고, 최근 공개 일정에서도 사전선택 후보를 찾지 못했습니다."
            )

        combined, ref_date, is_ref = self._with_page(operation)
        _check_cancel(cancel_event)
        self._emit(f"[CGV] 시간표 조회 완료 · {len(combined)}개", "success")
        return combined, ref_date, is_ref

    def _fetch_schedule_on_page(
        self, page, site_no: str, date_digits: str, *, timeout_ms: int = 5000
    ) -> tuple[dict[str, Any], ...]:
        query = urllib.parse.urlencode(
            {
                "coCd": CGV_COMPANY_CODE,
                "siteNo": str(site_no),
                "scnYmd": date_digits,
                "scnsNo": "",
                "scnSseq": "",
                "rtctlScopCd": "08",
                "custNo": "",
            }
        )
        payload = self._fetch_json(
            page, f"/api/v1/booking/searchMovScnInfo?{query}", timeout_ms=timeout_ms
        )
        return tuple(schedule_items(payload))

    def fetch_seat_map(
        self,
        schedule: Mapping[str, Any],
        people: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[CgvSeat, ...]:
        _check_cancel(cancel_event)
        payload = _booking_payload(schedule)

        def operation(page):
            _check_cancel(cancel_event)
            def open_visitor_page() -> str:
                cinema_query = urllib.parse.urlencode(
                    {
                        "siteNo": payload.get("siteNo", ""),
                        "siteNm": schedule.get("siteNm", "CGV"),
                    }
                )
                self._goto_with_retry(
                    page,
                    f"{CGV_HOME_URL}/cnm/movieBook/cinema?{cinema_query}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.evaluate(
                    """payload => {
                      let previous = {};
                      try { previous = JSON.parse(sessionStorage.getItem('query') || '{}'); }
                      catch (_) {}
                      const merged = {...previous, ...payload};
                      for (const key of ['custNo', 'bymd', 'mbltNo', 'nmbrCrtfNo']) {
                        if (!merged[key] && previous[key]) merged[key] = previous[key];
                      }
                      sessionStorage.setItem('query', JSON.stringify(merged));
                      sessionStorage.setItem('rsrtHistoryBack', 'Y');
                    }""",
                    payload,
                )
                self._goto_with_retry(
                    page,
                    f"{CGV_HOME_URL}/cnm/selectVisitorCnt",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                page.wait_for_timeout(500)
                return page.locator("body").inner_text(timeout=5000)

            body_text = open_visitor_page()
            if "로그인후 이용해 주세요" in body_text or "/mem/login" in page.url:
                try:
                    confirm = page.get_by_text("확인", exact=True)
                    for index in range(confirm.count()):
                        button = confirm.nth(index)
                        if button.is_visible():
                            button.click(timeout=1500)
                            break
                except Exception:
                    pass
                self._wait_for_member_login(page, require_fresh_login=True, cancel_event=cancel_event)
                body_text = open_visitor_page()
                if "로그인후 이용해 주세요" in body_text or "/mem/login" in page.url:
                    raise CgvLoginRequired(
                        "로그인은 확인됐지만 CGV 예매 세션을 만들지 못했습니다. 좌석 불러오기를 다시 눌러주세요."
                    )
            current_query = page.evaluate(
                """() => { try { return JSON.parse(sessionStorage.getItem('query') || '{}'); }
                catch (_) { return {}; } }"""
            )
            cust_no = str(current_query.get("custNo", "")) if isinstance(current_query, dict) else ""
            query = urllib.parse.urlencode(
                {
                    "coCd": payload.get("coCd", CGV_COMPANY_CODE),
                    "siteNo": payload.get("siteNo", ""),
                    "scnYmd": payload.get("scnYmd", ""),
                    "scnsNo": payload.get("scnsNo", ""),
                    "scnSseq": payload.get("scnSseq", ""),
                    "custNo": cust_no,
                }
            )
            seat_payload = self._fetch_json(
                page, f"/api/v1/booking/searchIfSeatData?{query}"
            )
            seats = parse_api_seats(seat_payload)
            if not seats:
                raise CgvError("CGV 공식 좌석 API에서 이 회차의 좌석 배치를 찾지 못했습니다.")
            return seats

        return self._with_page(operation)
