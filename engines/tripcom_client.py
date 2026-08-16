from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


KST = ZoneInfo("Asia/Seoul")
TRIPCOM_HOME = "https://kr.trip.com/"
TRIPCOM_API_ORIGIN = "https://kr.trip.com"
TRIPCOM_SOA_PATH = "/restapi/soa2/19622"
DISCOVERY_URLS = (
    TRIPCOM_HOME,
    "https://kr.trip.com/guide/stay/hotel-deals.html",
    "https://kr.trip.com/blog/ticketcoupon/",
)
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
}


class TripComError(RuntimeError):
    pass


class TripComRateLimited(TripComError):
    def __init__(self, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = max(5.0, float(retry_after))


class TripComVerificationRequired(TripComError):
    pass


@dataclass(frozen=True)
class CampaignComponent:
    campaign_id: str
    play_ids: tuple[str, ...]
    prize_type: str
    structure_id: str
    out_of_stock_text: str = ""


@dataclass(frozen=True)
class FlashSaleComponent:
    campaign_id: str
    schema_id: str
    structure_id: str
    section_id: str = ""
    date_tab_switch_time: int = 0


@dataclass
class CampaignPage:
    url: str
    title: str
    components: list[CampaignComponent] = field(default_factory=list)
    flash_sales: list[FlashSaleComponent] = field(default_factory=list)
    text: str = ""
    page_id: str = ""


@dataclass(frozen=True)
class TripComEvent:
    event_id: str
    campaign_id: str
    play_id: str
    prize_id: str
    campaign_name: str
    event_name: str
    campaign_url: str
    structure_id: str
    prize_type: str
    open_at: str
    close_at: str
    allowed_dates: tuple[str, ...]
    open_time: str
    in_stock: bool | None
    claim_code: str
    app_only: bool
    web_url: str
    description: str = ""
    source_checked_at: str = ""
    server_epoch: float | None = None
    action_kind: str = "coupon"
    extra_metadata: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        metadata = {
            "campaign_id": self.campaign_id,
            "play_id": self.play_id,
            "prize_id": self.prize_id,
            "prize_type": self.prize_type,
            "campaign_url": self.campaign_url,
            "structure_id": self.structure_id,
            "event_name": self.event_name,
            "description": self.description,
            "open_at": self.open_at,
            "close_at": self.close_at,
            "allowed_dates": list(self.allowed_dates),
            "open_time": self.open_time,
            "timezone": "Asia/Seoul",
            "in_stock": self.in_stock,
            "claim_code": self.claim_code,
            "app_only": self.app_only,
            "web_url": self.web_url,
            "source_checked_at": self.source_checked_at,
            "server_epoch": self.server_epoch,
            "server_clock_url": self.campaign_url or TRIPCOM_HOME,
            "action_kind": self.action_kind,
        }
        metadata.update(self.extra_metadata)
        return metadata


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _digits(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, int)):
        values = re.findall(r"\d+", str(value))
    elif isinstance(value, list):
        values = [str(item) for item in value if str(item).isdigit()]
    else:
        values = []
    return tuple(dict.fromkeys(values))


def _parse_epoch(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc).astimezone(KST)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    dotnet = re.search(r"/Date\((\d+)(?:[+-]\d+)?\)/", text)
    if dotnet:
        try:
            return datetime.fromtimestamp(int(dotnet.group(1)) / 1000.0, tz=timezone.utc).astimezone(KST)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def _first(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_campaign_page(html: str, url: str) -> CampaignPage:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    title_node = soup.find("title")
    if title_node:
        title = title_node.get_text(" ", strip=True)
    script = soup.find("script", id="__foxpage_data__")
    if script is None or not script.string:
        raise TripComError("Trip.com 캠페인 데이터(__foxpage_data__)를 찾지 못했습니다.")
    try:
        payload = json.loads(script.string)
    except json.JSONDecodeError as exc:
        raise TripComError(f"Trip.com 캠페인 JSON 해석 실패: {exc}") from exc

    raw_structures = payload.get("structures", [])
    if isinstance(raw_structures, dict):
        structures = [item for item in raw_structures.values() if isinstance(item, dict)]
    elif isinstance(raw_structures, list):
        structures = [item for item in raw_structures if isinstance(item, dict)]
    else:
        structures = []
    structure_map = {
        str(item.get("id", "")): item for item in structures if item.get("id")
    }

    components: list[CampaignComponent] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for node in _iter_dicts(raw_structures or payload):
        campaign_id = str(node.get("campaignId", "")).strip()
        play_ids = _digits(node.get("playIds", node.get("playId", [])))
        if not campaign_id or not play_ids:
            continue
        prize_type = str(node.get("prizeType", "PRIVATE_COUPON") or "PRIVATE_COUPON")
        structure_id = str(
            _first(node, "structureId", "id", "componentId", "moduleId", default="")
        )
        out_text = str(_first(node, "txtOutOfStock", "outOfStockText", default=""))
        key = (campaign_id, play_ids, structure_id)
        if key in seen:
            continue
        seen.add(key)
        components.append(
            CampaignComponent(campaign_id, play_ids, prize_type, structure_id, out_text)
        )
    promo_node = soup.find(id="promo_id")
    page_campaign_id = str(promo_node.get("value", "") if promo_node else "").strip()
    flash_sales: list[FlashSaleComponent] = []
    seen_flash: set[tuple[str, str]] = set()
    for node in structures:
        name = str(node.get("name", ""))
        props = node.get("props") or {}
        if not isinstance(props, dict) or "sales4-flash-sale" not in name:
            continue
        schema_id = str(props.get("flashSaleSchemaId", "")).strip()
        if not schema_id:
            continue
        campaign_id = str(props.get("campaignId") or page_campaign_id).strip()
        if not campaign_id:
            continue
        structure_id = str(node.get("id", "")).strip()
        extension = node.get("extension") or {}
        parent = structure_map.get(str(extension.get("parentId", "")), {})
        parent_props = parent.get("props") or {}
        section_id = str(parent_props.get("id", "")).strip()
        key = (campaign_id, schema_id)
        if key in seen_flash:
            continue
        seen_flash.add(key)
        flash_sales.append(
            FlashSaleComponent(
                campaign_id=campaign_id,
                schema_id=schema_id,
                structure_id=structure_id,
                section_id=section_id,
                date_tab_switch_time=int(props.get("dateTabSwitchTime") or 0),
            )
        )
    if not components and not flash_sales:
        raise TripComError("쿠폰/핫딜 구성요소를 찾지 못했습니다.")
    return CampaignPage(
        url=url,
        title=title or url,
        components=components,
        flash_sales=flash_sales,
        text=soup.get_text(" ", strip=True),
        page_id=_page_id_from_html(html),
    )


def _parse_daily_time(text: str) -> str:
    match = re.search(
        r"매일\s*(?:오전|오후)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?",
        text,
    )
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if "오후" in match.group(0) and hour < 12:
        hour += 12
    if "오전" in match.group(0) and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _allowed_dates(start: datetime, end: datetime, recurring_text: str, now: datetime) -> tuple[str, ...]:
    daily_time = _parse_daily_time(recurring_text)
    if not daily_time:
        claim_date = start.date() if start >= now else now.date()
        return (claim_date.isoformat(),)
    first = max(start.date(), now.date())
    last = min(end.date(), first + timedelta(days=61))
    if last < first:
        return (start.date().isoformat(),)
    values: list[str] = []
    current = first
    while current <= last:
        values.append(current.isoformat())
        current += timedelta(days=1)
    return tuple(values)


FLASH_SALE_STATUS = {
    1: "preheat",
    2: "flash_sale",
    3: "backup_sale",
    4: "sold_out",
    5: "ended",
}

FLIGHT_FLASH_STATUS = {
    "preheat": "preheat",
    "active": "flash_sale",
    "sold_out": "sold_out",
    "ended": "ended",
}


def build_flight_search_url(flight_product: dict[str, Any]) -> str:
    """Return the campaign-provided flight URL without inventing fare parameters."""
    link = str(flight_product.get("link") or "").strip()
    if not link:
        return ""
    return urllib.parse.urljoin(TRIPCOM_API_ORIGIN, link)


def _flight_flash_status(product: dict[str, Any], *, now_epoch: float) -> str:
    start = _parse_epoch(product.get("bookStartTime"))
    end = _parse_epoch(product.get("bookEndTime"))
    if start and now_epoch < start.timestamp():
        return FLIGHT_FLASH_STATUS["preheat"]
    if end and now_epoch > end.timestamp():
        return FLIGHT_FLASH_STATUS["ended"]
    # The official sales4 component only allows the web anchor to continue
    # when ruleStatus becomes "2".  Before the boundary the live API currently
    # returns "3" even though the product is visible as a preheat card.
    if str(product.get("ruleStatus") or "") == "2":
        return FLIGHT_FLASH_STATUS["active"]
    if start and now_epoch >= start.timestamp():
        return FLIGHT_FLASH_STATUS["sold_out"]
    return FLIGHT_FLASH_STATUS["preheat"]


def build_hotel_detail_url(
    hotel_product: dict[str, Any],
    *,
    campaign_id: str,
    page_id: str = "",
) -> str:
    hotel = hotel_product.get("hotelInfo") or {}
    room = hotel_product.get("roomInfo") or {}
    price_type = int(room.get("amountShowType") or 0)
    display = {0: "exavg", 1: "inctotal", 2: "incavg", 3: "cmatotal"}.get(
        price_type, "exavg"
    )
    params: list[tuple[str, Any]] = [
        ("hotelId", hotel.get("hotelId", "")),
        ("currency", room.get("currency", "KRW")),
        ("checkIn", room.get("checkIn", "")),
        ("checkOut", room.get("checkOut", "")),
        ("adult", room.get("adultCount", 1)),
        ("cityId", hotel.get("cityId", "")),
        ("showtotalamt", price_type),
        ("display", display),
        ("highlight", room.get("roomId", "")),
        ("crn", room.get("roomAmount", 1)),
        ("tid", f"H0002_{page_id}_flash" if page_id else ""),
        ("campaigntype", 1),
        ("msr", hotel.get("msr", "")),
        ("traffic_source_from_tag", "IBU_CAMPAIGN"),
        ("peak-type", "IBU_CAMPAIGN_T"),
        ("peak-id", campaign_id),
        ("peak-source-from", "HOTEL"),
        ("hoteluniquekey", hotel.get("hotelUniqueKey", "")),
    ]
    return "https://kr.trip.com/hotels/detail/?" + urllib.parse.urlencode(
        [(key, value) for key, value in params if value not in (None, "")]
    )


def _page_id_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for selector in ("page_id", "pageId"):
        node = soup.find(id=selector)
        if node and node.get("value"):
            return str(node.get("value"))
    match = re.search(r'"pageId"\s*:\s*"?(\d+)"?', html)
    return match.group(1) if match else ""


class TripComClient:
    """Read-only campaign discovery client used by the catalog and engine.

    This class intentionally does not expose Trip.com's claim endpoint. Final
    participation is performed through the normal website in Chrome so login,
    verification and the site's own confirmation state stay authoritative.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 12.0,
        flash_fetcher=None,
    ):
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.flash_fetcher = flash_fetcher

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
        final_host = urllib.parse.urlparse(response.url).netloc.casefold()
        body_hint = response.text[:4000].casefold() if response.content else ""
        if response.status_code in {429, 432}:
            retry = response.headers.get("Retry-After", "60")
            try:
                retry_after = float(retry)
            except ValueError:
                retry_after = 60.0
            raise TripComRateLimited(
                f"Trip.com 요청 제한(HTTP {response.status_code})", retry_after
            )
        if "verify.trip.com" in final_host or "tripverify" in body_hint:
            raise TripComVerificationRequired("Trip.com 사람 인증 화면이 응답했습니다.")
        response.raise_for_status()
        return response

    def discover_campaign_urls(self, max_campaigns: int = 3) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for source_url in DISCOVERY_URLS:
            response = self._request("GET", source_url)
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urllib.parse.urljoin(response.url, anchor["href"])
                parsed = urllib.parse.urlparse(url)
                if parsed.netloc.casefold() not in {"kr.trip.com", "www.trip.com"}:
                    continue
                if not parsed.path.startswith("/sale/w/"):
                    continue
                normalized = urllib.parse.urlunparse(
                    ("https", "kr.trip.com", parsed.path, "", "", "")
                ).rstrip("/")
                if normalized in seen:
                    continue
                seen.add(normalized)
                found.append(normalized)
                if len(found) >= max_campaigns:
                    return found
        return found

    def load_campaign(self, url: str) -> CampaignPage:
        response = self._request("GET", url)
        return parse_campaign_page(response.text, response.url)

    @staticmethod
    def _browser_flash_payload(page, component: FlashSaleComponent) -> dict[str, Any]:
        return page.evaluate(
            r"""
            async ({campaignId, schemaId}) => {
              const makeHead = () => ({
                locale: 'ko-KR', group: 'trip', currency: 'KRW', source: 'ONLINE',
                vid: window.TripComponentHelper?.getVid?.() || '',
                p: String(Date.now()), aid: '', sid: '', ouid: '', rmsToken: ''
              });
              const request = async (method, body) => {
                try {
                  return await window.webCoreFetch.request({
                    url: `/restapi/soa2/17585/${method}`,
                    method: 'POST',
                    headers: {
                      'x-ctx-peak-type': 'IBU_CAMPAIGN_T',
                      'x-ctx-peak-id': String(campaignId),
                      'content-type': 'application/json; charset=UTF-8',
                      'Accept': 'application/json'
                    },
                    body,
                    clientId: {api: window.webCoreClientIdHook},
                    timeout: 10000,
                    credentials: 'same-origin'
                  });
                } catch (error) {
                  let responseText = '';
                  try { responseText = await error?.response?.text?.(); } catch (_) {}
                  throw new Error(`HTTP ${error?.response?.status || 0} ${responseText || error}`);
                }
              };
              const groups = await request('queryFlashSaleGroupInfos', {
                head: makeHead(), pageCampaignId: Number(campaignId), schemaId: Number(schemaId)
              });
              const byDay = {};
              for (const item of groups.flashSaleGroupInfos || []) {
                const key = String(item.sellingStartTime || 0);
                (byDay[key] ||= []).push({
                  pkId: item.pkId,
                  productLine: item.productLine,
                  sceneName: item.sceneInfo?.sceneName || ''
                });
              }
              const products = {};
              for (const [key, groupInfo] of Object.entries(byDay)) {
                products[key] = await request('queryFlashSaleProductInfos', {
                  head: makeHead(), pageCampaignId: Number(campaignId),
                  schemaId: Number(schemaId), groupInfo
                });
              }
              return {groups, products};
            }
            """,
            {
                "campaignId": component.campaign_id,
                "schemaId": component.schema_id,
            },
        )

    def _load_flash_payloads(
        self, pages: list[CampaignPage]
    ) -> list[tuple[CampaignPage, FlashSaleComponent, dict[str, Any]]]:
        if self.flash_fetcher is not None:
            return [
                (page, component, self.flash_fetcher(page, component))
                for page in pages
                for component in page.flash_sales
            ]
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise TripComError("Trip.com 호텔 이벤트 조회용 브라우저 모듈이 없습니다.") from exc

        results: list[tuple[CampaignPage, FlashSaleComponent, dict[str, Any]]] = []
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=[
                        "--lang=ko-KR",
                        "--window-position=-32000,-32000",
                        "--window-size=800,600",
                    ],
                )
            except Exception as exc:
                raise TripComError(f"Trip.com 이벤트 조회용 Chrome 실행 실패: {exc}") from exc
            try:
                context = browser.new_context(locale="ko-KR")
                browser_page = context.new_page()
                for campaign_page in pages:
                    if not campaign_page.flash_sales:
                        continue
                    response = browser_page.goto(
                        campaign_page.url,
                        wait_until="networkidle",
                        timeout=max(45000, int(self.timeout * 1000)),
                    )
                    if response and response.status in {429, 430, 432}:
                        raise TripComRateLimited(
                            f"Trip.com 이벤트 조회 제한(HTTP {response.status})", 120
                        )
                    for component in campaign_page.flash_sales:
                        try:
                            payload = self._browser_flash_payload(browser_page, component)
                        except Exception as exc:
                            message = str(exc)
                            if any(code in message for code in ("HTTP 429", "HTTP 430", "HTTP 432")):
                                raise TripComRateLimited(
                                    f"Trip.com 이벤트 API 요청 제한: {message}", 120
                                ) from exc
                            raise TripComError(
                                f"Trip.com 호텔 이벤트({component.schema_id}) 조회 실패: {message}"
                            ) from exc
                        results.append((campaign_page, component, payload))
            finally:
                browser.close()
        return results

    @staticmethod
    def _flash_hotel_events(
        page: CampaignPage,
        component: FlashSaleComponent,
        payload: dict[str, Any],
        *,
        checked_at: str,
    ) -> list[TripComEvent]:
        groups = payload.get("groups", {}).get("flashSaleGroupInfos", [])
        start_by_pk = {
            str(item.get("pkId")): item.get("sellingStartTime")
            for item in groups
            if isinstance(item, dict)
        }
        server_epoch = TripComClient._server_epoch(payload.get("groups", {}))
        events: list[TripComEvent] = []
        for product_payload in (payload.get("products") or {}).values():
            for row in product_payload.get("flashSaleProductInfos", []):
                if not isinstance(row, dict) or row.get("productLine") != "HOTEL":
                    continue
                product = row.get("hotelProductInfo") or {}
                hotel = product.get("hotelInfo") or {}
                room = product.get("roomInfo") or {}
                pk_id = str(product.get("pkId", "")).strip()
                start = _parse_epoch(product.get("sellStartTime") or start_by_pk.get(pk_id))
                if not pk_id or start is None or not hotel.get("hotelId"):
                    continue
                end = _parse_epoch(product.get("sellEndTime")) or (start + timedelta(days=1))
                status_code = int(product.get("sellStatus") or 0)
                status = FLASH_SALE_STATUS.get(status_code, "unknown")
                hotel_name = str(hotel.get("hotelName") or f"호텔 {hotel.get('hotelId')}").strip()
                room_name = str(room.get("physicalRoomName") or "객실 미정").strip()
                event_name = f"{start.strftime('%m/%d')} · {hotel_name} · {room_name}"
                event_id = (
                    f"flash:{component.campaign_id}:{component.schema_id}:{pk_id}"
                )
                product_url = build_hotel_detail_url(
                    product,
                    campaign_id=component.campaign_id,
                    page_id=page.page_id,
                )
                events.append(
                    TripComEvent(
                        event_id=event_id,
                        campaign_id=component.campaign_id,
                        play_id=component.schema_id,
                        prize_id=pk_id,
                        campaign_name=page.title,
                        event_name=event_name,
                        campaign_url=page.url,
                        structure_id=component.structure_id,
                        prize_type="HOTEL",
                        open_at=start.isoformat(timespec="seconds"),
                        close_at=end.isoformat(timespec="seconds"),
                        allowed_dates=(start.date().isoformat(),),
                        open_time=start.strftime("%H:%M"),
                        in_stock=(True if status == "flash_sale" else None if status == "preheat" else False),
                        claim_code="",
                        app_only=False,
                        web_url=product_url,
                        description=str(
                            (product.get("hotelStaticMarketInfo") or {}).get("sellingPoint") or ""
                        ),
                        source_checked_at=checked_at,
                        server_epoch=server_epoch,
                        action_kind="hotel_flash_sale",
                        extra_metadata={
                            "schema_id": component.schema_id,
                            "product_pk_id": pk_id,
                            "product_line": "HOTEL",
                            "section_id": component.section_id,
                            "sale_status": status,
                            "sale_status_code": status_code,
                            "hotel_id": str(hotel.get("hotelId", "")),
                            "hotel_name": hotel_name,
                            "room_id": str(room.get("roomId", "")),
                            "room_name": room_name,
                            "check_in": str(room.get("checkIn", "")),
                            "check_out": str(room.get("checkOut", "")),
                            "event_price": str(
                                (product.get("hotelPreheatInfo") or {}).get("preheatPrice") or ""
                            ),
                            "current_price": str(room.get("price") or ""),
                            "product_url": product_url,
                            "page_id": page.page_id,
                        },
                    )
                )
        return events

    @staticmethod
    def _flash_flight_events(
        page: CampaignPage,
        component: FlashSaleComponent,
        payload: dict[str, Any],
        *,
        checked_at: str,
        now: datetime,
    ) -> list[TripComEvent]:
        groups = payload.get("groups", {}).get("flashSaleGroupInfos", [])
        start_by_pk = {
            str(item.get("pkId")): item.get("sellingStartTime")
            for item in groups
            if isinstance(item, dict) and item.get("productLine") == "FLIGHT"
        }
        server_epoch = TripComClient._server_epoch(payload.get("groups", {}))
        status_epoch = server_epoch or now.timestamp()
        events: list[TripComEvent] = []
        for product_payload in (payload.get("products") or {}).values():
            for row in product_payload.get("flashSaleProductInfos", []):
                if not isinstance(row, dict) or row.get("productLine") != "FLIGHT":
                    continue
                product = row.get("flightProductInfo") or {}
                pk_id = str(product.get("pkId", "")).strip()
                start = _parse_epoch(
                    product.get("bookStartTime") or start_by_pk.get(pk_id)
                )
                if not pk_id or start is None:
                    continue
                end = _parse_epoch(product.get("bookEndTime")) or (
                    start + timedelta(hours=13)
                )
                one_price = str(
                    product.get("showOneFixedPrice")
                    or product.get("oneFixedPrice")
                    or ""
                ).strip()
                normal_price = str(
                    product.get("showSalePrice") or product.get("salePrice") or ""
                ).strip()
                # Only the official one-price inventory is a flash/ultra-cheap
                # fare. Ordinary route cards and generic discounted fares are
                # intentionally excluded from the Trip.com event catalog.
                try:
                    is_real_flash = (
                        int(product.get("inventoryType") or 0) == 3
                        and float(one_price) > 0
                        and (not normal_price or float(one_price) < float(normal_price))
                    )
                except (TypeError, ValueError):
                    is_real_flash = False
                if not is_real_flash:
                    continue
                d_city = str(product.get("dCity") or product.get("dCode") or "출발지")
                a_city = str(product.get("aCity") or product.get("aCode") or "도착지")
                airline = str(
                    product.get("showAirlineName")
                    or product.get("airlineName")
                    or product.get("airlineCode")
                    or "항공사 미정"
                )
                status = _flight_flash_status(product, now_epoch=status_epoch)
                price_label = (
                    f"{int(float(one_price)):,}원" if one_price else "초특가"
                )
                event_name = (
                    f"{start.strftime('%m/%d %H:%M')} · {d_city}→{a_city} · "
                    f"{price_label} · {airline}"
                )
                product_url = build_flight_search_url(product)
                events.append(
                    TripComEvent(
                        event_id=(
                            f"flight:{component.campaign_id}:{component.schema_id}:{pk_id}"
                        ),
                        campaign_id=component.campaign_id,
                        play_id=component.schema_id,
                        prize_id=pk_id,
                        campaign_name=page.title,
                        event_name=event_name,
                        campaign_url=page.url,
                        structure_id=component.structure_id,
                        prize_type="FLIGHT",
                        open_at=start.isoformat(timespec="seconds"),
                        close_at=end.isoformat(timespec="seconds"),
                        allowed_dates=(start.date().isoformat(),),
                        open_time=start.strftime("%H:%M"),
                        in_stock=(
                            True
                            if status == "flash_sale"
                            else None if status == "preheat" else False
                        ),
                        claim_code="",
                        app_only=False,
                        web_url=product_url,
                        description=(
                            f"{d_city}→{a_city} {product.get('dDate') or ''} · "
                            f"정상 표시가 {normal_price or '-'}원"
                        ),
                        source_checked_at=checked_at,
                        server_epoch=server_epoch,
                        action_kind="flight_flash_sale",
                        extra_metadata={
                            "schema_id": component.schema_id,
                            "product_pk_id": pk_id,
                            "product_line": "FLIGHT",
                            "section_id": component.section_id,
                            "sale_status": status,
                            "rule_status": str(product.get("ruleStatus") or ""),
                            "inventory_type": int(product.get("inventoryType") or 0),
                            "product_id": str(product.get("productId") or ""),
                            "stock_id": str(product.get("stockId") or ""),
                            "activity_code": str(product.get("activityCode") or ""),
                            "departure_city": d_city,
                            "arrival_city": a_city,
                            "departure_code": str(product.get("dCode") or ""),
                            "arrival_code": str(product.get("aCode") or ""),
                            "departure_date": str(product.get("dDate") or ""),
                            "return_date": str(product.get("aDate") or ""),
                            "travel_date_start": str(product.get("outboundDateStart") or ""),
                            "travel_date_end": str(product.get("outboundDateEnd") or ""),
                            "trip_type": str(product.get("fType") or ""),
                            "airline_code": str(product.get("airlineCode") or ""),
                            "airline_name": airline,
                            "class_type": str(product.get("classType") or ""),
                            "event_price": one_price,
                            "normal_price": normal_price,
                            "product_url": product_url,
                            "page_id": page.page_id,
                        },
                    )
                )
        return events

    @staticmethod
    def _head(campaign_id: str, play_id: str = "0") -> dict[str, Any]:
        return {
            "head": {
                "locale": "ko-KR",
                "group": "Trip",
                "currency": "KRW",
                "source": "ONLINE",
                "p": str(int(time.time() * 1000)),
            },
            "campaignRequestHead": {
                "campaignId": str(campaign_id),
                "playId": int(play_id or 0),
            },
        }

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"{TRIPCOM_API_ORIGIN}{TRIPCOM_SOA_PATH}/{method}",
            json=payload,
            headers={"Content-Type": "application/json", "Origin": TRIPCOM_API_ORIGIN},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TripComError(f"Trip.com {method} 응답이 JSON이 아닙니다.") from exc
        if not isinstance(data, dict):
            raise TripComError(f"Trip.com {method} 응답 형식이 올바르지 않습니다.")
        return data

    def get_play_times(self, campaign_id: str, prize_type: str) -> dict[str, Any]:
        payload = self._head(campaign_id)
        # Foxpage uses numeric prizeType values (for example 4), while this
        # service expects the logical play type string.
        payload["playType"] = "PRIVATE_COUPON"
        return self._api("getPlayTimeInfo", payload)

    def get_gifts(self, campaign_id: str, play_id: str, prize_type: str) -> dict[str, Any]:
        payload = self._head(campaign_id, play_id)
        payload["prizeType"] = prize_type or "PRIVATE_COUPON"
        return self._api("getPromoGiftInfo", payload)

    @staticmethod
    def _server_epoch(payload: dict[str, Any]) -> float | None:
        status = payload.get("ResponseStatus", payload.get("responseStatus", {}))
        raw = status.get("Timestamp") if isinstance(status, dict) else None
        parsed = _parse_epoch(raw)
        return parsed.timestamp() if parsed else None

    @staticmethod
    def _time_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        for node in _iter_dicts(payload):
            rows = node.get("timeInfoList")
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
        return []

    @staticmethod
    def _gift_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for node in _iter_dicts(payload):
            if "prizeId" in node and any(
                key in node for key in ("privateCoupon", "publicCoupon", "marketInfo")
            ):
                candidates.append(node)
        return candidates

    def discover_events(self, max_campaigns: int = 3, now: datetime | None = None) -> list[TripComEvent]:
        now = (now or datetime.now(KST)).astimezone(KST)
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        events: list[TripComEvent] = []
        errors: list[str] = []
        pages: list[CampaignPage] = []
        for campaign_url in self.discover_campaign_urls(max_campaigns=max_campaigns):
            try:
                pages.append(self.load_campaign(campaign_url))
            except (requests.RequestException, TripComError) as exc:
                errors.append(f"{campaign_url}: {exc}")

        flash_pages = [page for page in pages if page.flash_sales]
        if flash_pages:
            try:
                for page, component, payload in self._load_flash_payloads(flash_pages):
                    events.extend(
                        self._flash_hotel_events(
                            page,
                            component,
                            payload,
                            checked_at=checked_at,
                        )
                    )
                    events.extend(
                        self._flash_flight_events(
                            page,
                            component,
                            payload,
                            checked_at=checked_at,
                            now=now,
                        )
                    )
            except (TripComError, TripComRateLimited) as exc:
                errors.append(str(exc))

        for page in pages:
            component_map: dict[tuple[str, str], CampaignComponent] = {}
            time_cache: dict[tuple[str, str], dict[str, Any]] = {}
            for component in page.components:
                cache_key = (component.campaign_id, component.prize_type)
                if cache_key not in time_cache:
                    try:
                        time_cache[cache_key] = self.get_play_times(*cache_key)
                    except (requests.RequestException, TripComError) as exc:
                        errors.append(f"{component.campaign_id}: {exc}")
                        continue
                    time.sleep(0.08)
                for play_id in component.play_ids:
                    component_map[(component.campaign_id, play_id)] = component

            for (campaign_id, prize_type), time_payload in time_cache.items():
                server_epoch = self._server_epoch(time_payload)
                for row in self._time_rows(time_payload):
                    play_id = str(_first(row, "playId", "id", default="")).strip()
                    component = component_map.get((campaign_id, play_id))
                    if not play_id or component is None:
                        continue
                    start = _parse_epoch(_first(row, "startTime", "startDate", default=""))
                    end = _parse_epoch(_first(row, "endTime", "endDate", default=""))
                    if start is None:
                        continue
                    end = end or (start + timedelta(days=1))
                    if end < now - timedelta(days=1):
                        continue
                    try:
                        gift_payload = self.get_gifts(campaign_id, play_id, prize_type)
                    except (requests.RequestException, TripComError) as exc:
                        errors.append(f"{campaign_id}/{play_id}: {exc}")
                        continue
                    time.sleep(0.08)
                    gift_rows = self._gift_rows(gift_payload)
                    for index, gift in enumerate(gift_rows):
                        coupon = gift.get("privateCoupon") or gift.get("publicCoupon") or {}
                        market = gift.get("marketInfo") or {}
                        if not isinstance(coupon, dict):
                            coupon = {}
                        if not isinstance(market, dict):
                            market = {}
                        prize_id = str(_first(gift, "prizeId", default=index))
                        name = str(
                            _first(
                                coupon,
                                "couponName",
                                "title",
                                "name",
                                default=_first(market, "displayName", "name", default=f"이벤트 {play_id}"),
                            )
                        ).strip()
                        description = str(
                            _first(coupon, "couponDescription", "description", default="")
                        ).strip()
                        terms = str(
                            _first(coupon, "term", "couponTerms", default="")
                        ).strip()
                        product_line_id = str(
                            _first(
                                coupon,
                                "userProductLineId",
                                "productLineId",
                                default="",
                            )
                        ).strip()
                        coupon_text = " ".join(
                            (page.title, name, description, terms)
                        ).casefold()
                        deal_text = " ".join((name, description, terms)).casefold()
                        is_flight_coupon = product_line_id == "1" or (
                            "항공권" in coupon_text and "항공+호텔" not in coupon_text
                        )
                        first_come = any(
                            token in coupon_text
                            for token in ("선착순", "한정 수량", "수량은 제한")
                        )
                        hotdeal_word = any(
                            token in deal_text
                            for token in ("초특가", "특가", "핫딜", "만원")
                        )
                        strategies = coupon.get("deductionStrategyList") or coupon.get(
                            "deductionStrategy"
                        ) or []
                        discount_values = [
                            _number(item.get("deductionAmount"))
                            for item in strategies
                            if isinstance(item, dict)
                        ]
                        maximum_discount = max(discount_values, default=0.0)
                        coupon_amount = _number(coupon.get("couponAmount"))
                        strategy_type = int(_number(coupon.get("deductionStrategyType")))
                        meaningful_discount = (
                            (strategy_type == 2 and coupon_amount >= 10)
                            or (strategy_type == 1 and coupon_amount >= 20_000)
                            or (strategy_type == 2 and maximum_discount >= 10)
                            or any(
                                _number(item.get("deductionAmountLimit")) >= 20_000
                                for item in strategies
                                if isinstance(item, dict)
                            )
                        )
                        if not (
                            is_flight_coupon
                            and first_come
                            and (hotdeal_word or meaningful_discount)
                        ):
                            continue
                        claim_code = str(_first(coupon, "claimCode", default=""))
                        stock_raw = _first(coupon, "isInStock", default=None)
                        in_stock = stock_raw if isinstance(stock_raw, bool) else None
                        app_only = bool(_first(coupon, "appOnly", "isAppOnly", default=False))
                        web_url = str(
                            _first(coupon, "deepLinkOnline", default=_first(market, "openUrl", default=""))
                        )
                        recurring_time = _parse_daily_time(component.out_of_stock_text)
                        event_time = recurring_time or start.strftime("%H:%M")
                        dates = _allowed_dates(start, end, component.out_of_stock_text, now)
                        event_id = f"{campaign_id}:{play_id}:{prize_id}"
                        events.append(
                            TripComEvent(
                                event_id=event_id,
                                campaign_id=campaign_id,
                                play_id=play_id,
                                prize_id=prize_id,
                                campaign_name=page.title,
                                event_name=name or f"이벤트 {play_id}",
                                campaign_url=page.url,
                                structure_id=component.structure_id,
                                prize_type=str(_first(gift, "prizeType", default=prize_type)),
                                open_at=start.isoformat(timespec="seconds"),
                                close_at=end.isoformat(timespec="seconds"),
                                allowed_dates=dates,
                                open_time=event_time,
                                in_stock=in_stock,
                                claim_code=claim_code,
                                app_only=app_only,
                                web_url=web_url,
                                description=description,
                                source_checked_at=checked_at,
                                server_epoch=server_epoch,
                                action_kind="flight_coupon",
                                extra_metadata={
                                    "product_line": "FLIGHT",
                                    "product_line_id": product_line_id,
                                    "coupon_code": str(coupon.get("couponCode") or ""),
                                    "coupon_terms": terms,
                                    "coupon_amount": coupon_amount,
                                    "deduction_strategy_type": strategy_type,
                                    "first_come": first_come,
                                },
                            )
                        )
        if not events:
            detail = errors[0] if errors else "공식 페이지에서 진행 중인 웹 이벤트를 찾지 못했습니다."
            raise TripComError(detail)
        unique = {event.event_id: event for event in events}
        return sorted(
            unique.values(),
            key=lambda item: (item.allowed_dates[0] if item.allowed_dates else "9999", item.open_time, item.event_name),
        )
