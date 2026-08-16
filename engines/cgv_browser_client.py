from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from engines import browser_session
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
    parse_site_catalog,
    schedule_items,
)


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
    exact_movies = {
        re.sub(r"\s+", "", _movie_name(item)).casefold()
        for item in exact_schedules
        if _movie_name(item)
    }

    # Group by (canonical_movie_name, auditorium_name, format_name)
    groups: dict[tuple[str, str, str], dict[str, list[str]]] = {}
    display_names: dict[tuple[str, str, str], tuple[str, str, str]] = {}

    for item in all_history_items:
        movie = _movie_name(item)
        if not movie:
            continue
        canon_movie = re.sub(r"\s+", "", movie).casefold()
        if canon_movie in exact_movies:
            # Already published on target date! Real schedule has priority.
            continue
        auditorium = _auditorium_name(item)
        format_name = _format_name(item)
        group_key = (canon_movie, auditorium, format_name)
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
    ) -> None:
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
        saw_login_page = "/mem/login" in page.url
        while time.monotonic() < deadline:
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
            page.wait_for_timeout(500)
        raise CgvLoginRequired(
            "CGV 로그인 대기 시간이 끝났습니다. 좌석 불러오기를 다시 눌러주세요."
        )

    def _with_page(self, operation):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CgvError("CGV 데이터 조회에 필요한 브라우저 모듈을 찾지 못했습니다.") from exc

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
                    return operation(page)
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
        finally:
            # Keep the persistent Chrome open.  If login is required the user can
            # finish it there and press retry without losing the authenticated
            # profile; the slot itself is released for the booking engine.
            chrome.release()

    @staticmethod
    def _fetch_json(page, path: str) -> dict[str, Any]:
        result = page.evaluate(
            """
            async path => {
              const response = await fetch(path, {
                method: 'GET', credentials: 'include', cache: 'no-store',
                headers: {'Accept': 'application/json'}
              });
              const text = await response.text();
              let data = null;
              try { data = JSON.parse(text); } catch (_) {}
              return {status: response.status, data, text: text.slice(0, 500)};
            }
            """,
            path,
        )
        status = int(result.get("status", 0)) if isinstance(result, dict) else 0
        if status in {401, 403}:
            raise CgvAccessBlocked(
                "CGV 세션 인증이 만료되었습니다. 열린 CGV Chrome에서 로그인 후 다시 시도해주세요."
            )
        if status == 429:
            raise CgvAccessBlocked("CGV 조회가 일시적으로 제한되었습니다. 잠시 뒤 다시 시도해주세요.")
        data = result.get("data") if isinstance(result, dict) else None
        if status < 200 or status >= 300 or not isinstance(data, dict):
            raise CgvError(f"CGV 데이터 조회에 실패했습니다. (HTTP {status or '응답 없음'})")
        if int(data.get("statusCode", 0) or 0) != 0:
            raise CgvError(str(data.get("statusMessage") or "CGV가 조회 요청을 처리하지 못했습니다."))
        return data

    def fetch_catalog(self, *, imax_only: bool = True) -> CgvCatalogSnapshot:
        def operation(page):
            query = urllib.parse.urlencode(
                {"coCd": CGV_COMPANY_CODE, "custNo": "", "lntd": "", "lttd": "", "srchKwrd": ""}
            )
            payload = self._fetch_json(
                page, f"/api/v1/content/site/searchAllRegionAndSite?{query}"
            )
            regions, sites = parse_site_catalog(payload, imax_only=imax_only)
            if not sites and imax_only:
                regions, sites = parse_site_catalog(payload, imax_only=False)
            if not regions or not sites:
                raise CgvError("CGV 지역·지점 목록이 비어 있습니다.")
            return CgvCatalogSnapshot(regions, sites)

        return self._with_page(operation)

    def fetch_schedule(self, site_no: str, screening_date: str) -> tuple[dict[str, Any], ...]:
        date_digits = _screening_date(screening_date)

        def operation(page):
            return self._fetch_schedule_on_page(page, site_no, date_digits)

        return self._with_page(operation)

    def fetch_schedule_with_reference(
        self,
        site_no: str,
        screening_date: str,
        *,
        max_reference_days: int = 14,
    ) -> tuple[tuple[dict[str, Any], ...], str, bool]:
        date_digits = _screening_date(screening_date)
        target = datetime.strptime(date_digits, "%Y%m%d").date()
        today = datetime.now().date()

        def operation(page):
            exact = self._fetch_schedule_on_page(page, site_no, date_digits)
            history_by_date: dict[str, tuple[dict[str, Any], ...]] = {}
            all_history_items: list[dict[str, Any]] = []

            # Sample bounded recent published dates
            # Check up to max_reference_days backwards, not older than 7 days before today
            max_days = max(1, min(int(max_reference_days), 14))
            for offset in range(1, max_days + 1):
                candidate = target - timedelta(days=offset)
                if candidate < today - timedelta(days=7):
                    break
                items = self._fetch_schedule_on_page(
                    page, site_no, candidate.strftime("%Y%m%d")
                )
                if items:
                    history_by_date[candidate.isoformat()] = items
                    all_history_items.extend(items)
                if len(history_by_date) >= 7:
                    break

            latest_ref_date = next(iter(history_by_date.keys()), "")

            decorated_exact: list[dict[str, Any]] = []
            if exact:
                for schedule in exact:
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

            templates = _aggregate_historical_candidates(
                history_by_date, screening_date, tuple(exact or ()), str(site_no)
            )
            combined = tuple(decorated_exact) + tuple(templates)

            if exact:
                return combined, target.isoformat(), False
            if combined:
                return combined, (latest_ref_date or target.isoformat()), True

            raise CgvError(
                "선택한 날짜의 시간표가 아직 열리지 않았고, 최근 공개 일정에서도 사전선택 후보를 찾지 못했습니다."
            )

        return self._with_page(operation)

    def _fetch_schedule_on_page(
        self, page, site_no: str, date_digits: str
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
        payload = self._fetch_json(page, f"/api/v1/booking/searchMovScnInfo?{query}")
        return tuple(schedule_items(payload))

    def fetch_seat_map(
        self, schedule: Mapping[str, Any], people: int
    ) -> tuple[CgvSeat, ...]:
        payload = _booking_payload(schedule)

        def operation(page):
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
                self._wait_for_member_login(page, require_fresh_login=True)
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
