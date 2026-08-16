from __future__ import annotations

import threading
import time
import urllib.parse
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from engines import browser_session
from engines.base_engine import BaseEngine
from engines.server_clock import ServerClock
from engines.tripcom_client import (
    FLASH_SALE_STATUS,
    build_flight_search_url,
    build_hotel_detail_url,
)
from pengucro.diagnostics import format_exception
from pengucro.models import BookingResult, parse_bool_flag


KST = ZoneInfo("Asia/Seoul")


class TripComEngine(BaseEngine):
    """Claim a selected web-capable Trip.com campaign item in normal Chrome."""

    VERIFY_TOKENS = (
        "verify.trip.com",
        "사람인지 확인",
        "보안 인증",
        "complete verification",
        "drag the slider",
    )
    SUCCESS_TOKENS = (
        "발급 완료",
        "받기 완료",
        "쿠폰 받기 완료",
        "이미 받",
        "사용하기",
        "claimed",
        "received",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._browser_lock = threading.Lock()

    @staticmethod
    def _event_metadata(reservation_data: dict[str, Any]) -> dict[str, Any]:
        engine_metadata = reservation_data.get("engine_metadata", {})
        if not isinstance(engine_metadata, dict):
            return {}
        theme = engine_metadata.get("theme", {})
        return dict(theme) if isinstance(theme, dict) else {}

    @staticmethod
    def _target_datetime(reservation_data: dict[str, Any], metadata: dict[str, Any]) -> datetime:
        selected_date = str(reservation_data.get("reservationDate", ""))
        target_time = str(metadata.get("open_time") or reservation_data.get("reservationTime", ""))[:5]
        target = datetime.strptime(f"{selected_date} {target_time}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        allowed = metadata.get("allowed_dates", ())
        if len(allowed) == 1 and metadata.get("open_at"):
            try:
                original = datetime.fromisoformat(str(metadata["open_at"]))
                if original.tzinfo is None:
                    original = original.replace(tzinfo=KST)
                if original < datetime.now(KST):
                    return original.astimezone(KST)
            except ValueError:
                pass
        return target

    @staticmethod
    def _release_when_closed(chrome) -> None:
        def waiter() -> None:
            while browser_session.cdp_descriptor(chrome.port):
                time.sleep(0.5)
            chrome.release()

        threading.Thread(
            target=waiter,
            name=f"TripComChromeRelease-{chrome.port}",
            daemon=True,
        ).start()

    @classmethod
    def _verification_visible(cls, page) -> bool:
        current = str(page.url).casefold()
        if "verify.trip.com" in current:
            return True
        try:
            body = page.locator("body").inner_text(timeout=1200).casefold()
        except Exception:
            return False
        return any(token in body for token in cls.VERIFY_TOKENS)

    @staticmethod
    def _find_and_click(page, event_name: str, structure_id: str) -> dict[str, Any]:
        return page.evaluate(
            r"""
            ({needle, structureId}) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const target = norm(needle);
              const action = /받기|쿠폰|claim|get|참여|응모|구매|예약/i;
              const roots = [];
              if (structureId) {
                for (const selector of [`.e-class-${CSS.escape(structureId)}`, `#${CSS.escape(structureId)}`]) {
                  try { roots.push(...document.querySelectorAll(selector)); } catch (_) {}
                }
              }
              roots.push(document.body);
              const seen = new Set();
              for (const root of roots) {
                if (!root || seen.has(root)) continue;
                seen.add(root);
                const candidates = [...root.querySelectorAll('button,[role="button"],a')];
                for (const candidate of candidates) {
                  if (candidate.disabled || candidate.getAttribute('aria-disabled') === 'true') continue;
                  const buttonText = norm(candidate.innerText || candidate.textContent);
                  if (!action.test(buttonText)) continue;
                  let parent = candidate;
                  for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
                    const parentText = norm(parent.innerText || parent.textContent);
                    if (target && parentText.includes(target)) {
                      candidate.scrollIntoView({block: 'center', inline: 'center'});
                      candidate.click();
                      return {clicked: true, text: buttonText, matched: target};
                    }
                  }
                }
              }
              return {clicked: false, reason: 'matching-action-not-found'};
            }
            """,
            {"needle": event_name, "structureId": structure_id},
        )

    @staticmethod
    def _open_global_login(page) -> bool:
        return bool(
            page.evaluate(
                r"""
                () => {
                  const nodes = [...document.querySelectorAll('a,button,[role="button"]')];
                  const login = nodes.find(node => {
                    const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
                    const href = node.href || '';
                    return /로그인\s*\/\s*회원가입|^로그인$|sign\s*in/i.test(text)
                      || /account\/signin/i.test(href);
                  });
                  if (!login) return false;
                  login.scrollIntoView({block: 'center'});
                  login.click();
                  return true;
                }
                """
            )
        )

    @staticmethod
    def _live_hotel_product(page, metadata: dict[str, Any]) -> dict[str, Any]:
        return page.evaluate(
            r"""
            async ({campaignId, schemaId, productPkId}) => {
              if (!window.webCoreFetch || !window.webCoreClientIdHook) {
                return {ok: false, error: 'campaign-api-not-ready'};
              }
              const makeHead = () => ({
                locale: 'ko-KR', group: 'trip', currency: 'KRW', source: 'ONLINE',
                vid: window.TripComponentHelper?.getVid?.() || '',
                p: String(Date.now()), aid: '', sid: '', ouid: '', rmsToken: ''
              });
              try {
                const response = await window.webCoreFetch.request({
                  url: '/restapi/soa2/17585/queryFlashSaleProductInfos',
                  method: 'POST',
                  headers: {
                    'x-ctx-peak-type': 'IBU_CAMPAIGN_T',
                    'x-ctx-peak-id': String(campaignId),
                    'content-type': 'application/json; charset=UTF-8',
                    'Accept': 'application/json'
                  },
                  body: {
                    head: makeHead(), pageCampaignId: Number(campaignId),
                    schemaId: Number(schemaId),
                    groupInfo: [{pkId: Number(productPkId), productLine: 'HOTEL'}]
                  },
                  clientId: {api: window.webCoreClientIdHook},
                  timeout: 10000,
                  credentials: 'same-origin'
                });
                const row = (response.flashSaleProductInfos || []).find(item =>
                  String(item?.hotelProductInfo?.pkId || '') === String(productPkId)
                );
                return row?.hotelProductInfo
                  ? {ok: true, product: row.hotelProductInfo}
                  : {ok: false, error: 'product-not-found'};
              } catch (error) {
                let body = '';
                try { body = await error?.response?.text?.(); } catch (_) {}
                return {ok: false, status: error?.response?.status || 0, error: body || String(error)};
              }
            }
            """,
            {
                "campaignId": metadata.get("campaign_id", ""),
                "schemaId": metadata.get("schema_id", ""),
                "productPkId": metadata.get("product_pk_id", ""),
            },
        )

    @staticmethod
    def _live_flight_product(page, metadata: dict[str, Any]) -> dict[str, Any]:
        return page.evaluate(
            r"""
            async ({campaignId, schemaId, productPkId}) => {
              if (!window.webCoreFetch || !window.webCoreClientIdHook) {
                return {ok: false, error: 'campaign-api-not-ready'};
              }
              const makeHead = () => ({
                locale: 'ko-KR', group: 'trip', currency: 'KRW', source: 'ONLINE',
                vid: window.TripComponentHelper?.getVid?.() || '',
                p: String(Date.now()), aid: '', sid: '', ouid: '', rmsToken: ''
              });
              try {
                const response = await window.webCoreFetch.request({
                  url: '/restapi/soa2/17585/queryFlashSaleProductInfos',
                  method: 'POST',
                  headers: {
                    'x-ctx-peak-type': 'IBU_CAMPAIGN_T',
                    'x-ctx-peak-id': String(campaignId),
                    'content-type': 'application/json; charset=UTF-8',
                    'Accept': 'application/json'
                  },
                  body: {
                    head: makeHead(), pageCampaignId: Number(campaignId),
                    schemaId: Number(schemaId),
                    groupInfo: [{pkId: Number(productPkId), productLine: 'FLIGHT'}]
                  },
                  clientId: {api: window.webCoreClientIdHook},
                  timeout: 10000,
                  credentials: 'same-origin'
                });
                const row = (response.flashSaleProductInfos || []).find(item =>
                  String(item?.flightProductInfo?.pkId || '') === String(productPkId)
                );
                return row?.flightProductInfo
                  ? {ok: true, product: row.flightProductInfo}
                  : {ok: false, error: 'product-not-found'};
              } catch (error) {
                let body = '';
                try { body = await error?.response?.text?.(); } catch (_) {}
                return {ok: false, status: error?.response?.status || 0, error: body || String(error)};
              }
            }
            """,
            {
                "campaignId": metadata.get("campaign_id", ""),
                "schemaId": metadata.get("schema_id", ""),
                "productPkId": metadata.get("product_pk_id", ""),
            },
        )

    @staticmethod
    def _click_flight_deal(page, metadata: dict[str, Any]) -> dict[str, Any]:
        return page.evaluate(
            r"""
            ({departure, arrival, airline, eventPrice}) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const digits = value => String(value || '').replace(/[^0-9]/g, '');
              const dep = norm(departure), arr = norm(arrival), carrier = norm(airline);
              const price = digits(eventPrice);
              const actionRe = /자세히\s*보기|선택|예약|예매|구매|book|select|reserve/i;
              const actions = [...document.querySelectorAll('button,a,[role="button"]')]
                .filter(node => actionRe.test(node.innerText || node.textContent || ''));
              let fallback = null;
              for (const action of actions) {
                if (action.disabled || action.getAttribute('aria-disabled') === 'true') continue;
                let parent = action;
                for (let depth = 0; parent && depth < 12; depth++, parent = parent.parentElement) {
                  const raw = parent.innerText || parent.textContent || '';
                  const text = norm(raw);
                  const routeVisible = (dep && text.includes(dep)) || (arr && text.includes(arr));
                  const routeMatch = !routeVisible || ((!dep || text.includes(dep)) && (!arr || text.includes(arr)));
                  const airlineMatch = !carrier || text.includes(carrier);
                  const priceMatch = !price || digits(raw).includes(price) || text.includes('만원');
                  const promoMatch = /초특가|깜짝특가|특가|만원|one\s*price/i.test(text);
                  if (routeMatch && airlineMatch && priceMatch && promoMatch) {
                    action.scrollIntoView({block: 'center', inline: 'center'});
                    action.click();
                    return {clicked: true, text: norm(action.innerText || action.textContent), exact: true};
                  }
                  if (!fallback && routeMatch && airlineMatch && priceMatch) fallback = action;
                }
              }
              if (fallback) {
                fallback.scrollIntoView({block: 'center', inline: 'center'});
                fallback.click();
                return {clicked: true, text: norm(fallback.innerText || fallback.textContent), exact: false};
              }
              return {clicked: false, reason: 'matching-flight-deal-not-found'};
            }
            """,
            {
                "departure": metadata.get("departure_city", ""),
                "arrival": metadata.get("arrival_city", ""),
                "airline": metadata.get("airline_name", ""),
                "eventPrice": metadata.get("event_price", ""),
            },
        )

    @staticmethod
    def _click_flight_booking_action(page, metadata: dict[str, Any]) -> dict[str, Any]:
        return page.evaluate(
            r"""
            ({airline, eventPrice}) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const digits = value => String(value || '').replace(/[^0-9]/g, '');
              const carrier = norm(airline), price = digits(eventPrice);
              const actions = [...document.querySelectorAll('button,a,[role="button"]')]
                .filter(node => /선택|예약|예매|book|choose|reserve/i.test(
                  node.innerText || node.textContent || ''
                ));
              for (const action of actions) {
                if (action.disabled || action.getAttribute('aria-disabled') === 'true') continue;
                let parent = action;
                for (let depth = 0; parent && depth < 12; depth++, parent = parent.parentElement) {
                  const raw = parent.innerText || parent.textContent || '';
                  const text = norm(raw);
                  if ((!carrier || text.includes(carrier)) &&
                      (!price || digits(raw).includes(price) || text.includes('만원'))) {
                    action.scrollIntoView({block: 'center', inline: 'center'});
                    action.click();
                    return {clicked: true, text: norm(action.innerText || action.textContent)};
                  }
                }
              }
              return {clicked: false, reason: 'booking-action-not-found'};
            }
            """,
            {
                "airline": metadata.get("airline_name", ""),
                "eventPrice": metadata.get("event_price", ""),
            },
        )

    @staticmethod
    def _fill_flight_contact(page, name: str, phone: str) -> dict[str, Any]:
        return page.evaluate(
            r"""
            ({name, phone}) => {
              const result = {name: false, phone: false};
              const fields = [...document.querySelectorAll('input:not([type="hidden"])')];
              const descriptor = input => [
                input.name, input.id, input.placeholder, input.getAttribute('aria-label'),
                input.autocomplete
              ].filter(Boolean).join(' ').toLowerCase();
              const setValue = (input, value) => {
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                setter ? setter.call(input, value) : (input.value = value);
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
              };
              for (const input of fields) {
                const info = descriptor(input);
                if (!result.phone && /phone|mobile|contact|tel|전화|연락처/.test(info)) {
                  setValue(input, phone); result.phone = true; continue;
                }
                if (!result.name && /contact.*name|booker.*name|예약자|연락인|연락처.*이름/.test(info)) {
                  setValue(input, name); result.name = true;
                }
              }
              return result;
            }
            """,
            {"name": name, "phone": phone},
        )

    @staticmethod
    def _click_hotel_room(page, room_name: str, hotel_name: str) -> dict[str, Any]:
        return page.evaluate(
            r"""
            ({roomName, hotelName}) => {
              const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const room = norm(roomName);
              const hotel = norm(hotelName);
              const actions = [...document.querySelectorAll('button,a,[role="button"]')]
                .filter(node => /예약|선택|book|reserve/i.test(node.innerText || node.textContent || ''));
              for (const action of actions) {
                if (action.disabled || action.getAttribute('aria-disabled') === 'true') continue;
                let parent = action;
                for (let depth = 0; parent && depth < 10; depth++, parent = parent.parentElement) {
                  const text = norm(parent.innerText || parent.textContent);
                  if ((room && text.includes(room)) || (!room && hotel && text.includes(hotel))) {
                    action.scrollIntoView({block: 'center', inline: 'center'});
                    action.click();
                    return {clicked: true, text: norm(action.innerText || action.textContent)};
                  }
                }
              }
              return {clicked: false, reason: 'matching-room-action-not-found'};
            }
            """,
            {"roomName": room_name, "hotelName": hotel_name},
        )

    @staticmethod
    def _open_login_for_event(page, event_name: str) -> bool:
        return bool(
            page.evaluate(
                r"""
                (needle) => {
                  const norm = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
                  const target = norm(needle);
                  for (const candidate of document.querySelectorAll('button,[role="button"],a')) {
                    if (!/로그인|sign\s*in|log\s*in/i.test(candidate.innerText || candidate.textContent || '')) continue;
                    let parent = candidate;
                    for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
                      if (norm(parent.innerText || parent.textContent).includes(target)) {
                        candidate.scrollIntoView({block: 'center'});
                        candidate.click();
                        return true;
                      }
                    }
                  }
                  return false;
                }
                """,
                event_name,
            )
        )

    @classmethod
    def _success_visible(cls, page, event_name: str) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1800).casefold()
        except Exception:
            return False
        event = event_name.casefold()
        if event and event not in body:
            return False
        return any(token in body for token in cls.SUCCESS_TOKENS)

    def _wait_for_user_verification(self, page) -> bool:
        announced = False
        while not self.stop_event.is_set() and self._verification_visible(page):
            if not announced:
                self.log(
                    "[경고] Trip.com 사람 인증이 필요합니다. 열린 Chrome에서 직접 완료하면 자동으로 이어집니다.",
                    "warning",
                )
                announced = True
            self.stop_event.wait(0.5)
        if announced and not self.stop_event.is_set():
            self.log("[정보] Trip.com 인증 화면 통과를 확인했습니다.", "success")
        return not self.stop_event.is_set()

    def _run_hotel_flash_sale(
        self,
        page,
        metadata: dict[str, Any],
        reservation_data: dict[str, Any],
    ) -> None:
        if str(metadata.get("sale_status", "")) in {"backup_sale", "sold_out", "ended"}:
            self.log(
                "선택한 호텔 5만원 이벤트는 이미 종료되었거나 매진되었습니다. 이벤트를 갱신해주세요.",
                "error",
            )
            return
        if parse_bool_flag(reservation_data.get("devMode", False)):
            self.log(
                "[개발자 모드] 호텔 이벤트 오픈 시각 도달 · 실제 상품 화면으로 이동하지 않았습니다.",
                "success",
            )
            self.stop_event.set()
            return

        live: dict[str, Any] = {"ok": False}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.stop_event.is_set():
            live = self._live_hotel_product(page, metadata)
            if live.get("ok"):
                product = live.get("product") or {}
                status_code = int(product.get("sellStatus") or 0)
                if status_code != 1:
                    break
            page.wait_for_timeout(180)
        if self.stop_event.is_set():
            return
        if not live.get("ok"):
            self.log(
                f"호텔 이벤트 상품을 다시 확인하지 못했습니다 · {live.get('error', '응답 없음')}",
                "error",
            )
            return
        product = live.get("product") or {}
        status_code = int(product.get("sellStatus") or 0)
        status = FLASH_SALE_STATUS.get(status_code, "unknown")
        if status != "flash_sale":
            status_text = {
                "preheat": "아직 오픈 전",
                "backup_sale": "5만원 종료 후 일반 특가",
                "sold_out": "매진",
                "ended": "종료",
            }.get(status, f"알 수 없는 상태({status_code})")
            self.log(f"호텔 5만원 상품 상태가 예약 가능이 아닙니다 · {status_text}", "error")
            return

        hotel = product.get("hotelInfo") or {}
        room = product.get("roomInfo") or {}
        hotel_name = str(hotel.get("hotelName") or metadata.get("hotel_name", ""))
        room_name = str(room.get("physicalRoomName") or metadata.get("room_name", ""))
        product_url = build_hotel_detail_url(
            product,
            campaign_id=str(metadata.get("campaign_id", "")),
            page_id=str(metadata.get("page_id", "")),
        )
        self.log(
            f"[상품 확인] 5만원 판매 상태 확인 · {hotel_name} · {room_name} · "
            f"hotelId={hotel.get('hotelId', '')} · roomId={room.get('roomId', '')}",
            "success",
        )
        response = page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
        self.log(
            f"[HTTP] 호텔 특가 상품 화면 이동 · GET · status={getattr(response, 'status', '없음')}",
            "info",
        )
        if "account/signin" in str(page.url):
            self.log(
                "[경고] Trip.com 로그인이 완료되지 않았습니다. 열린 Chrome에서 로그인하면 상품 화면으로 돌아갑니다.",
                "warning",
            )
            login_deadline = time.monotonic() + 90.0
            while (
                time.monotonic() < login_deadline
                and "account/signin" in str(page.url)
                and not self.stop_event.is_set()
            ):
                page.wait_for_timeout(300)
            if "account/signin" in str(page.url):
                self.log("로그인 대기 시간이 초과되었습니다. 열린 Chrome에서 계속 진행해주세요.", "error")
                return

        click_deadline = time.monotonic() + 30.0
        clicked = {"clicked": False}
        while time.monotonic() < click_deadline and not self.stop_event.is_set():
            if self._verification_visible(page):
                if not self._wait_for_user_verification(page):
                    return
            clicked = self._click_hotel_room(page, room_name, hotel_name)
            if clicked.get("clicked"):
                break
            page.wait_for_timeout(250)
        if not clicked.get("clicked"):
            self.log(
                "정확한 특가 객실의 예약 버튼을 찾지 못했습니다. 상품 화면은 열어두었으니 직접 확인해주세요.",
                "warning",
            )
            return
        self.log(
            f"[객실 선택] 정확한 객실 예약 버튼 클릭 · {room_name} · 다음 화면 확인 중",
            "success",
        )
        page.wait_for_timeout(2500)
        current_url = str(page.url)
        if any(token in current_url.casefold() for token in ("booking", "order", "payment")):
            self.log(
                "[정보] 예약자·결제 입력 화면까지 진입했습니다. 결제 완료 전에는 예약 확정이 아닙니다.",
                "warning",
            )
        else:
            self.log(
                "[정보] 호텔 상품 화면에서 객실 선택을 완료했습니다. 열린 Chrome에서 다음 단계를 확인해주세요.",
                "warning",
            )
        self.log("[정보] 결과 화면을 열어둡니다. 확인 후 Chrome을 닫아주세요.", "info")

    def _run_flight_flash_sale(
        self,
        page,
        metadata: dict[str, Any],
        reservation_data: dict[str, Any],
    ) -> None:
        if str(metadata.get("sale_status", "")) in {"sold_out", "ended"}:
            self.log(
                "선택한 항공 초특가 이벤트는 이미 종료되었거나 매진되었습니다. 이벤트를 갱신해주세요.",
                "error",
            )
            return
        if parse_bool_flag(reservation_data.get("devMode", False)):
            self.log(
                "[개발자 모드] 항공 초특가 오픈 시각 도달 · 실제 상품 화면으로 이동하지 않았습니다.",
                "success",
            )
            self.stop_event.set()
            return

        live: dict[str, Any] = {"ok": False}
        product: dict[str, Any] = {}
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not self.stop_event.is_set():
            live = self._live_flight_product(page, metadata)
            if live.get("ok"):
                product = live.get("product") or {}
                rule_status = str(product.get("ruleStatus") or "")
                if rule_status == "2":
                    break
                book_end = int(product.get("bookEndTime") or 0)
                if book_end and int(time.time() * 1000) > book_end:
                    break
            page.wait_for_timeout(120)
        if self.stop_event.is_set():
            return
        if not live.get("ok"):
            self.log(
                f"항공 초특가 상품을 다시 확인하지 못했습니다 · {live.get('error', '응답 없음')}",
                "error",
            )
            return
        if str(product.get("ruleStatus") or "") != "2":
            self.log(
                "항공 초특가 재고가 판매 상태로 전환되지 않았습니다. 오픈 지연 또는 즉시 매진으로 판단합니다.",
                "error",
            )
            return

        product_url = build_flight_search_url(product) or str(
            metadata.get("product_url") or ""
        )
        if not product_url:
            self.log("항공 초특가 상품 링크가 응답에 없습니다.", "error")
            return
        people = max(1, int(reservation_data.get("people") or 1))
        parsed_url = urllib.parse.urlparse(product_url)
        query = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.casefold() != "adult"]
        query.append(("adult", str(people)))
        product_url = urllib.parse.urlunparse(
            parsed_url._replace(query=urllib.parse.urlencode(query))
        )
        self.log(
            "[상품 확인] 항공 초특가 판매 상태 확인 · "
            f"{product.get('dCity', '')}→{product.get('aCity', '')} · "
            f"{product.get('showOneFixedPrice') or product.get('oneFixedPrice') or '-'}원 · "
            f"stockId={product.get('stockId', '')}",
            "success",
        )
        response = page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
        self.log(
            f"[HTTP] 항공 초특가 검색 화면 이동 · GET · status={getattr(response, 'status', '없음')}",
            "info",
        )
        if not self._wait_for_user_verification(page):
            return

        live_metadata = dict(metadata)
        live_metadata.update(
            {
                "departure_city": product.get("dCity") or metadata.get("departure_city", ""),
                "arrival_city": product.get("aCity") or metadata.get("arrival_city", ""),
                "airline_name": product.get("showAirlineName")
                or product.get("airlineName")
                or metadata.get("airline_name", ""),
                "event_price": product.get("showOneFixedPrice")
                or product.get("oneFixedPrice")
                or metadata.get("event_price", ""),
            }
        )
        clicked: dict[str, Any] = {"clicked": False}
        click_deadline = time.monotonic() + 35.0
        while time.monotonic() < click_deadline and not self.stop_event.is_set():
            if self._verification_visible(page):
                if not self._wait_for_user_verification(page):
                    return
            clicked = self._click_flight_deal(page, live_metadata)
            if clicked.get("clicked"):
                break
            page.wait_for_timeout(250)
        if not clicked.get("clicked"):
            self.log(
                "정확한 항공 초특가 운임의 예매 버튼을 찾지 못했습니다. 검색 결과 화면을 열어두었으니 직접 확인해주세요.",
                "warning",
            )
            return
        self.log(
            "[운임 선택] 노선·항공사·초특가 금액이 일치하는 예매 버튼 클릭 완료",
            "success",
        )
        page.wait_for_timeout(1200)
        booking_clicked: dict[str, Any] = {"clicked": False}
        booking_deadline = time.monotonic() + 20.0
        while time.monotonic() < booking_deadline and not self.stop_event.is_set():
            current = str(page.url).casefold()
            if any(token in current for token in ("booking", "order", "passenger")):
                booking_clicked = {"clicked": True, "text": "페이지 이동"}
                break
            booking_clicked = self._click_flight_booking_action(page, live_metadata)
            if booking_clicked.get("clicked"):
                break
            page.wait_for_timeout(250)
        if booking_clicked.get("clicked"):
            self.log("[예매 진입] 선택한 초특가 운임의 예약 단계로 이동합니다.", "success")
            page.wait_for_timeout(1800)
        else:
            self.log(
                "운임 상세는 열렸지만 다음 예약 버튼을 자동으로 확인하지 못했습니다. 열린 Chrome에서 계속 진행해주세요.",
                "warning",
            )
        if self._verification_visible(page):
            if not self._wait_for_user_verification(page):
                return

        contact = self._fill_flight_contact(
            page,
            str(reservation_data.get("name") or ""),
            str(reservation_data.get("phone") or ""),
        )
        if contact.get("name") or contact.get("phone"):
            self.log(
                "[예약자 정보] 항공 예약 화면의 연락인 이름·전화번호를 입력했습니다.",
                "success",
            )
        if people == 1:
            self.log(
                "[정보] 저장된 탑승객 선택 또는 여권·생년월일 확인이 필요하면 열린 Chrome에서 완료해주세요.",
                "warning",
            )
        else:
            self.log(
                f"[정보] {people}명 탑승객의 신원 정보는 프로그램이 추측하지 않습니다. 열린 Chrome에서 탑승객을 선택해주세요.",
                "warning",
            )
        self.log(
            "[정보] 초특가 운임 선택 화면을 유지합니다. 최종 운임·탑승객·결제수단 확인 후 결제를 완료해야 발권됩니다.",
            "warning",
        )
        self.log("[정보] 결과 화면을 열어둡니다. 확인 후 Chrome을 닫아주세요.", "info")

    def make_reservation_thread(self, reservation_data: dict[str, Any]) -> None:
        metadata = self._event_metadata(reservation_data)
        action_kind = str(metadata.get("action_kind", "coupon"))
        event_name = str(metadata.get("event_name") or reservation_data.get("themeLabel", "")).strip()
        campaign_url = str(metadata.get("campaign_url") or reservation_data.get("site_url", "")).strip()
        selected_date = str(reservation_data.get("reservationDate", ""))
        allowed_dates = {str(value) for value in metadata.get("allowed_dates", [])}
        if not metadata or not event_name or not campaign_url:
            self.log("Trip.com 이벤트 정보가 없습니다. 고급 설정에서 이벤트를 먼저 갱신해주세요.", "error")
            return
        if allowed_dates and selected_date not in allowed_dates:
            self.log("선택한 날짜는 이 이벤트가 열리는 날짜가 아닙니다. 이벤트를 다시 선택해주세요.", "error")
            return
        if metadata.get("app_only"):
            self.log(
                "선택한 이벤트는 Trip.com 앱 전용입니다. PC 웹에서는 선점할 수 없어 실행하지 않습니다.",
                "error",
            )
            return
        try:
            target = self._target_datetime(reservation_data, metadata)
        except ValueError:
            self.log("Trip.com 이벤트 오픈 날짜·시간 형식이 올바르지 않습니다.", "error")
            return

        self.log(
            f"Trip.com 이벤트 준비 · {event_name} · {target.strftime('%Y-%m-%d %H:%M')}",
            "info",
        )
        clock = ServerClock(
            str(metadata.get("server_clock_url") or campaign_url),
            log=self.log,
        )
        clock.sync(announce=True)
        target_epoch = target.timestamp()
        seconds = clock.seconds_until(target_epoch)
        self.log(
            f"[정보] 이벤트 오픈까지 {max(0, int(seconds))}초 · Trip.com 서버 시간 기준",
            "info",
        )

        chrome = browser_session.start_isolated(log=self.log)
        if chrome is None:
            self.log("Trip.com 이벤트 화면을 열 Chrome 슬롯이 없습니다.", "error")
            return
        keep_open = True
        try:
            from playwright.sync_api import sync_playwright

            with self._browser_lock, sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                response = page.goto(campaign_url, wait_until="domcontentloaded", timeout=45000)
                self.log(
                    f"[HTTP] Trip.com 캠페인 화면 예열 · GET · status="
                    f"{getattr(response, 'status', '없음')}",
                    "info",
                )
                if not self._wait_for_user_verification(page):
                    return
                self.log(
                    "[정보] 이벤트 화면을 준비했습니다. 로그인이 필요하면 열린 Chrome에서 미리 로그인해주세요.",
                    "info",
                )
                try:
                    login_opened = (
                        self._open_global_login(page)
                        if action_kind in {"hotel_flash_sale", "flight_flash_sale"}
                        else self._open_login_for_event(page, event_name)
                    )
                    if login_opened:
                        self.log(
                            "[정보] Trip.com 로그인 화면을 열었습니다. 오픈 전에 로그인을 완료해주세요.",
                            "warning",
                        )
                        login_deadline = time.monotonic() + 120.0
                        while (
                            "account/signin" in str(page.url)
                            and time.monotonic() < login_deadline
                            and clock.seconds_until(target_epoch) > 5.0
                            and not self.stop_event.is_set()
                        ):
                            page.wait_for_timeout(300)
                        if "account/signin" not in str(page.url) and action_kind in {
                            "hotel_flash_sale",
                            "flight_flash_sale",
                        }:
                            page.goto(
                                campaign_url,
                                wait_until="domcontentloaded",
                                timeout=45000,
                            )
                except Exception:
                    pass

                last_notice = 0.0
                while not self.stop_event.is_set():
                    remaining = clock.seconds_until(target_epoch)
                    if remaining <= 0:
                        break
                    if time.monotonic() - last_notice >= 30.0:
                        self.log(
                            f"[정보] 이벤트 오픈까지 {max(0, int(remaining))}초 · 화면 대기 중",
                            "info",
                        )
                        last_notice = time.monotonic()
                    self.stop_event.wait(min(0.25, max(0.01, remaining)))
                if self.stop_event.is_set():
                    return

                if action_kind == "hotel_flash_sale":
                    if "account/signin" in str(page.url):
                        self.log(
                            "Trip.com 로그인이 완료되지 않아 5만원 상품 조회를 시작할 수 없습니다.",
                            "error",
                        )
                        return
                    if "sale/w/" not in str(page.url):
                        page.goto(
                            campaign_url,
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                    self._run_hotel_flash_sale(page, metadata, reservation_data)
                    return

                if action_kind == "flight_flash_sale":
                    if "account/signin" in str(page.url):
                        self.log(
                            "Trip.com 로그인이 완료되지 않아 항공 초특가 조회를 시작할 수 없습니다.",
                            "error",
                        )
                        return
                    if "sale/w/" not in str(page.url):
                        page.goto(
                            campaign_url,
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                    self._run_flight_flash_sale(page, metadata, reservation_data)
                    return

                if parse_bool_flag(reservation_data.get("devMode", False)):
                    self.log(
                        "[개발자 모드] 오픈 시각 도달 · 실제 이벤트 버튼은 클릭하지 않았습니다.",
                        "success",
                    )
                    self.stop_event.set()
                    return

                structure_id = str(metadata.get("structure_id", ""))
                # The campaign's own timer usually enables the already-rendered
                # button at the boundary. Poll that warm DOM first; a reload is
                # only the fallback, avoiding a full navigation on the hot path.
                click_deadline = time.monotonic() + 1.5
                clicked: dict[str, Any] = {"clicked": False}
                while time.monotonic() < click_deadline and not self.stop_event.is_set():
                    clicked = self._find_and_click(page, event_name, structure_id)
                    if clicked.get("clicked"):
                        break
                    page.wait_for_timeout(50)
                if not clicked.get("clicked") and not self.stop_event.is_set():
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    if not self._wait_for_user_verification(page):
                        return
                    click_deadline = time.monotonic() + 10.0
                    while time.monotonic() < click_deadline and not self.stop_event.is_set():
                        clicked = self._find_and_click(page, event_name, structure_id)
                        if clicked.get("clicked"):
                            break
                        page.wait_for_timeout(100)
                if not clicked.get("clicked"):
                    self.log(
                        "오픈된 이벤트와 일치하는 받기 버튼을 찾지 못했습니다. 열린 Chrome 화면을 확인해주세요.",
                        "error",
                    )
                    return
                self.log(
                    (
                        f"[쿠폰 처리] Trip.com 항공 할인코드 버튼 클릭 · {clicked.get('text', '받기')}"
                        if action_kind == "flight_coupon"
                        else f"[최종 제출] Trip.com 이벤트 버튼 클릭 · {clicked.get('text', '받기')}"
                    ),
                    "info",
                )
                confirmation_deadline = time.monotonic() + 15.0
                while time.monotonic() < confirmation_deadline:
                    if self._verification_visible(page):
                        if not self._wait_for_user_verification(page):
                            return
                    if self._success_visible(page, event_name):
                        success_message = (
                            "Trip.com 항공 할인코드 복사/발급 완료를 화면에서 확인했습니다. "
                            "할인 적용은 결제 시점의 남은 수량으로 최종 결정됩니다."
                            if action_kind == "flight_coupon"
                            else "Trip.com 이벤트 선점 완료를 화면에서 확인했습니다."
                        )
                        self.log(success_message, "success")
                        self.log("[정보] 결과 페이지를 열어둡니다. 확인 후 Chrome을 닫아주세요.", "info")
                        self.notify_success(
                            BookingResult(
                                True,
                                (
                                    "Trip.com 항공 할인코드 복사/발급이 완료되었습니다."
                                    if action_kind == "flight_coupon"
                                    else "Trip.com 이벤트 선점이 완료되었습니다."
                                ),
                                details={
                                    "campaign_id": metadata.get("campaign_id", ""),
                                    "play_id": metadata.get("play_id", ""),
                                    "prize_id": metadata.get("prize_id", ""),
                                },
                            )
                        )
                        return
                    page.wait_for_timeout(150)
                self.log(
                    "버튼은 클릭했지만 발급 완료 문구를 확인하지 못했습니다. 열린 Chrome에서 결과를 확인해주세요.",
                    "warning",
                )
        except Exception as exc:
            self.log(f"Trip.com 이벤트 처리 오류: {format_exception(exc)}", "error")
        finally:
            if keep_open:
                self._release_when_closed(chrome)
            else:
                chrome.close_if_launched()
                chrome.release()

    async def make_reservation_async_task(self, reservation_data, task_idx) -> None:
        raise RuntimeError("Trip.com 엔진은 단일 브라우저 작업만 지원합니다.")
