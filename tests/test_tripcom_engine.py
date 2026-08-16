from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engines.catalog_providers import TripComProvider, catalog_to_site_config
from engines.tripcom_client import (
    CampaignComponent,
    CampaignPage,
    FlashSaleComponent,
    TripComClient,
    TripComEvent,
    TripComError,
    TripComRateLimited,
    _flight_flash_status,
    parse_campaign_page,
)
from engines.tripcom_engine import TripComEngine
from pengucro.models import TRIPCOM_MODE
from pengucro.catalog import CatalogBranch, CatalogService, CatalogTheme, SiteCatalog


KST = ZoneInfo("Asia/Seoul")


def test_foxpage_parser_extracts_campaign_component():
    html = """
    <html><head><title>여름 핫딜</title></head><body>
      <script id="__foxpage_data__" type="application/json">
      {"structures":{"x":{"campaignId":"45669","playIds":[168975],
      "prizeType":4,"txtOutOfStock":"매일 오전 11시 오픈"}}}
      </script>
    </body></html>
    """

    page = parse_campaign_page(html, "https://kr.trip.com/sale/w/test.html")

    assert page.title == "여름 핫딜"
    assert page.components[0].campaign_id == "45669"
    assert page.components[0].play_ids == ("168975",)
    assert page.components[0].out_of_stock_text == "매일 오전 11시 오픈"


def test_foxpage_parser_extracts_hotel_flash_sale_schema():
    html = """
    <html><head><title>호텔 5만원 찬스</title></head><body>
      <input id="promo_id" value="45669"><input id="page_id" value="10651208762">
      <script id="__foxpage_data__" type="application/json">
      {"structures":[
        {"id":"panel","name":"panel","props":{"id":"hotelonepricedeal"}},
        {"id":"flash","name":"@ctrip/cloud-component-sales4-flash-sale",
         "props":{"flashSaleSchemaId":"181000","dateTabSwitchTime":43200000},
         "extension":{"parentId":"panel"}}
      ]}
      </script>
    </body></html>
    """

    page = parse_campaign_page(html, "https://kr.trip.com/sale/w/hotel.html")

    assert page.page_id == "10651208762"
    assert page.flash_sales == [
        FlashSaleComponent("45669", "181000", "flash", "hotelonepricedeal", 43200000)
    ]


def test_flash_sale_payload_projects_exact_hotel_date_and_room():
    page = parse_campaign_page(
        """
        <html><head><title>호텔 5만원 찬스</title></head><body>
        <input id="promo_id" value="45669"><input id="page_id" value="10651208762">
        <script id="__foxpage_data__">{"structures":[
          {"id":"panel","name":"panel","props":{"id":"hotelonepricedeal"}},
          {"id":"flash","name":"@ctrip/cloud-component-sales4-flash-sale",
           "props":{"flashSaleSchemaId":"181000"},"extension":{"parentId":"panel"}}
        ]}</script></body></html>
        """,
        "https://kr.trip.com/sale/w/hotel.html",
    )
    payload = {
        "groups": {
            "ResponseStatus": {"Timestamp": "/Date(1786672800000+0900)/"},
            "flashSaleGroupInfos": [
                {"pkId": 10508, "productLine": "HOTEL", "sellingStartTime": 1786672800000}
            ],
        },
        "products": {
            "1786672800000": {
                "flashSaleProductInfos": [{
                    "productLine": "HOTEL",
                    "hotelProductInfo": {
                        "pkId": 10508,
                        "hotelInfo": {
                            "hotelId": 992194, "hotelName": "웨스틴 조선 부산",
                            "hotelUniqueKey": "key", "cityId": 253, "msr": "msr",
                        },
                        "roomInfo": {
                            "roomId": 2238449183, "physicalRoomName": "디럭스 저층 해변 랜덤 배정",
                            "currency": "KRW", "checkIn": "2026-09-02", "checkOut": "2026-09-03",
                            "adultCount": 1, "roomAmount": 1, "amountShowType": 2, "price": "309412",
                        },
                        "hotelPreheatInfo": {"preheatPrice": "50000"},
                        "hotelStaticMarketInfo": {"sellingPoint": "오늘 단 하루"},
                        "sellStatus": 3, "sellStartTime": 1786672800000,
                        "sellEndTime": 1786719599000,
                    },
                }],
            }
        },
    }

    events = TripComClient._flash_hotel_events(
        page, page.flash_sales[0], payload, checked_at="2026-08-14T02:30:00+00:00"
    )

    assert len(events) == 1
    metadata = events[0].metadata()
    assert events[0].event_name.startswith("08/14 · 웨스틴 조선 부산")
    assert metadata["allowed_dates"] == ["2026-08-14"]
    assert metadata["open_time"] == "11:00"
    assert metadata["sale_status"] == "backup_sale"
    assert metadata["event_price"] == "50000"
    assert metadata["hotel_id"] == "992194"
    assert metadata["room_id"] == "2238449183"
    assert "hotelId=992194" in metadata["product_url"]


def test_flash_sale_payload_projects_only_real_one_price_flight():
    page = parse_campaign_page(
        """
        <html><head><title>Trip Chance</title></head><body>
        <input id="promo_id" value="45669"><input id="page_id" value="10651208762">
        <script id="__foxpage_data__">{"structures":[
          {"id":"panel","name":"panel","props":{"id":"flightonepricedeal"}},
          {"id":"flash","name":"@ctrip/cloud-component-sales4-flash-sale",
           "props":{"flashSaleSchemaId":"180797"},"extension":{"parentId":"panel"}}
        ]}</script></body></html>
        """,
        "https://kr.trip.com/sale/w/tripchance.html",
    )
    payload = {
        "groups": {
            "ResponseStatus": {"Timestamp": "/Date(1786672800000+0900)/"},
            "flashSaleGroupInfos": [
                {"pkId": 21624, "productLine": "FLIGHT", "sellingStartTime": 1786932000000}
            ],
        },
        "products": {"1786932000000": {"flashSaleProductInfos": [
            {"productLine": "FLIGHT", "flightProductInfo": {
                "pkId": 21624, "dCity": "서울", "aCity": "상하이",
                "dCode": "SEL", "aCode": "SHA", "fType": "OW",
                "salePrice": "152600", "showSalePrice": "152600",
                "oneFixedPrice": "10000", "showOneFixedPrice": "10000",
                "currency": "KRW", "airlineCode": "ZE", "airlineName": "이스타항공",
                "classType": "Economy", "ruleStatus": "3", "inventoryType": 3,
                "productId": 137691, "stockId": 557978, "activityCode": "KRTCOP0817",
                "bookStartTime": 1786932000000, "bookEndTime": 1786978799000,
                "dDate": "2026-10-27", "outboundDateStart": "2026-08-17",
                "outboundDateEnd": "2026-12-31",
                "link": "/flights/seoul-to-shanghai/tickets-sel-sha/?ddate=2026-10-27",
            }},
            {"productLine": "FLIGHT", "flightProductInfo": {
                "pkId": 99999, "inventoryType": 1, "salePrice": "90000",
                "oneFixedPrice": "", "bookStartTime": 1786932000000,
            }},
        ]}},
    }

    events = TripComClient._flash_flight_events(
        page,
        page.flash_sales[0],
        payload,
        checked_at="2026-08-14T02:30:00+00:00",
        now=datetime(2026, 8, 14, 11, 30, tzinfo=KST),
    )

    assert len(events) == 1
    metadata = events[0].metadata()
    assert events[0].event_name == "08/17 11:00 · 서울→상하이 · 10,000원 · 이스타항공"
    assert metadata["action_kind"] == "flight_flash_sale"
    assert metadata["sale_status"] == "preheat"
    assert metadata["product_id"] == "137691"
    assert metadata["stock_id"] == "557978"
    assert metadata["activity_code"] == "KRTCOP0817"
    assert metadata["departure_date"] == "2026-10-27"
    assert metadata["product_url"].startswith("https://kr.trip.com/flights/")


def test_flight_status_requires_official_live_rule_status():
    start = datetime(2026, 8, 17, 11, 0, tzinfo=KST).timestamp() * 1000
    end = datetime(2026, 8, 17, 23, 59, tzinfo=KST).timestamp() * 1000
    product = {"bookStartTime": start, "bookEndTime": end, "ruleStatus": "3"}
    assert _flight_flash_status(
        product, now_epoch=datetime(2026, 8, 17, 10, 59, tzinfo=KST).timestamp()
    ) == "preheat"
    product["ruleStatus"] = "2"
    assert _flight_flash_status(
        product, now_epoch=datetime(2026, 8, 17, 11, 0, tzinfo=KST).timestamp()
    ) == "flash_sale"


def test_flight_coupon_filter_ignores_page_title_only_hotdeal_word(monkeypatch):
    page = CampaignPage(
        url="https://kr.trip.com/sale/w/hotel-5manwon.html",
        title="호텔 5만원 찬스",
        components=(
            CampaignComponent(
                campaign_id="45669",
                play_ids=("101",),
                prize_type="4",
                structure_id="coupon-1",
                out_of_stock_text="매일 오전 11시 선착순",
            ),
        ),
    )
    client = TripComClient()
    monkeypatch.setattr(client, "discover_campaign_urls", lambda max_campaigns: [page.url])
    monkeypatch.setattr(client, "load_campaign", lambda url: page)
    monkeypatch.setattr(
        client,
        "get_play_times",
        lambda campaign_id, prize_type: {
            "timeInfoList": [{
                "playId": 101,
                "startTime": "2026-08-17T11:00:00+09:00",
                "endTime": "2026-08-17T23:59:59+09:00",
            }]
        },
    )
    monkeypatch.setattr(
        client,
        "get_gifts",
        lambda campaign_id, play_id, prize_type: {
            "giftList": [{
                "prizeId": 1,
                "privateCoupon": {
                    "couponName": "항공권 8천원 할인",
                    "userProductLineId": 1,
                    "couponDescription": "선착순 발급",
                    "deductionStrategyType": 1,
                    "couponAmount": 8000,
                },
            }]
        },
    )
    monkeypatch.setattr("engines.tripcom_client.time.sleep", lambda seconds: None)

    with pytest.raises(TripComError, match="진행 중인 웹 이벤트"):
        client.discover_events(now=datetime(2026, 8, 17, 10, 0, tzinfo=KST))


def test_flight_coupon_filter_keeps_real_first_come_discount(monkeypatch):
    page = CampaignPage(
        url="https://kr.trip.com/sale/w/flight-hotdeal.html",
        title="항공 메가 세일",
        components=(
            CampaignComponent(
                campaign_id="45669",
                play_ids=("102",),
                prize_type="4",
                structure_id="coupon-2",
                out_of_stock_text="매일 오전 11시 선착순",
            ),
        ),
    )
    client = TripComClient()
    monkeypatch.setattr(client, "discover_campaign_urls", lambda max_campaigns: [page.url])
    monkeypatch.setattr(client, "load_campaign", lambda url: page)
    monkeypatch.setattr(
        client,
        "get_play_times",
        lambda campaign_id, prize_type: {
            "timeInfoList": [{
                "playId": 102,
                "startTime": "2026-08-18T11:00:00+09:00",
                "endTime": "2026-08-18T23:59:59+09:00",
            }]
        },
    )
    monkeypatch.setattr(
        client,
        "get_gifts",
        lambda campaign_id, play_id, prize_type: {
            "giftList": [{
                "prizeId": 2,
                "privateCoupon": {
                    "couponName": "항공권 선착순 20% 특가",
                    "userProductLineId": 1,
                    "couponDescription": "한정 수량",
                    "deductionStrategyType": 2,
                    "couponAmount": 20,
                    "claimCode": "claim-20",
                },
            }]
        },
    )
    monkeypatch.setattr("engines.tripcom_client.time.sleep", lambda seconds: None)

    events = client.discover_events(now=datetime(2026, 8, 18, 10, 0, tzinfo=KST))

    assert len(events) == 1
    assert events[0].action_kind == "flight_coupon"
    assert events[0].claim_code == "claim-20"


def test_tripcom_client_classifies_rate_limit():
    class Response:
        status_code = 432
        url = "https://kr.trip.com/"
        headers = {"Retry-After": "90"}
        content = b""
        text = ""

    class Session:
        headers = {}

        def request(self, *args, **kwargs):
            return Response()

    client = TripComClient(Session())
    with pytest.raises(TripComRateLimited) as caught:
        client._request("GET", "https://kr.trip.com/")
    assert caught.value.retry_after == 90


def test_provider_projects_event_dates_and_web_capability(monkeypatch):
    event = TripComEvent(
        event_id="45669:168975:535285",
        campaign_id="45669",
        play_id="168975",
        prize_id="535285",
        campaign_name="트립찬스",
        event_name="5만원 할인",
        campaign_url="https://kr.trip.com/sale/w/test.html",
        structure_id="coupon-1",
        prize_type="4",
        open_at="2026-08-18T11:00:01+09:00",
        close_at="2026-08-30T23:59:59+09:00",
        allowed_dates=("2026-08-18",),
        open_time="11:00",
        in_stock=True,
        claim_code="707",
        app_only=False,
        web_url="/flights",
        source_checked_at="2026-08-14T00:00:00+00:00",
    )
    monkeypatch.setattr(TripComClient, "discover_events", lambda self, max_campaigns: [event])

    catalog = TripComProvider().discover(
        {
            "name": "Trip.com 핫딜",
            "catalog_key": "builtin:tripcom",
            "url": "https://kr.trip.com/",
            "engine_options": {"max_campaigns": 1},
        },
        "2026-08-18",
    )
    projected = catalog_to_site_config(catalog)

    assert projected["branches"]["트립찬스"] == "campaign:45669"
    assert projected["themes"]["campaign:45669"]["5만원 할인"] == event.event_id
    metadata = projected["theme_metadata"]["campaign:45669"][event.event_id]
    assert metadata["allowed_dates"] == ["2026-08-18"]
    assert metadata["app_only"] is False


def test_engine_uses_original_open_time_for_already_open_one_time_event():
    target = TripComEngine._target_datetime(
        {"reservationDate": "2026-08-20", "reservationTime": "11:00"},
        {
            "open_time": "11:00",
            "open_at": "2020-01-01T11:00:00+09:00",
            "allowed_dates": ["2026-08-20"],
        },
    )
    assert target == datetime(2020, 1, 1, 11, 0, tzinfo=KST)


def test_tripcom_mode_constant_is_dedicated():
    assert TRIPCOM_MODE == "Trip.com 이벤트"


def test_catalog_reports_metadata_only_open_time_change():
    old = SiteCatalog(
        "builtin:tripcom", "Trip.com 핫딜", "tripcom", "https://kr.trip.com/",
        {"c": CatalogBranch("c", "행사", "c", themes={
            "e": CatalogTheme("e", "쿠폰", "e", {"open_time": "11:00"})
        })},
    )
    new = SiteCatalog(
        "builtin:tripcom", "Trip.com 핫딜", "tripcom", "https://kr.trip.com/",
        {"c": CatalogBranch("c", "행사", "c", themes={
            "e": CatalogTheme("e", "쿠폰", "e", {"open_time": "12:00"})
        })},
    )

    merged, applied, pending, error = CatalogService._merge_safe(object.__new__(CatalogService), old, new)

    assert not pending and not error
    assert merged.branches["c"].themes["e"].metadata["open_time"] == "12:00"
    assert any(change.kind == "metadata_updated" for change in applied)


def test_tripcom_authoritative_catalog_replaces_stale_coupon_items():
    old = SiteCatalog(
        "builtin:tripcom", "Trip.com 핫딜", "tripcom", "https://kr.trip.com/",
        {"old": CatalogBranch("old", "잘못된 쿠폰", "old", themes={
            "coupon": CatalogTheme("coupon", "일본 입장권 할인코드", "coupon", {})
        })},
    )
    new = SiteCatalog(
        "builtin:tripcom", "Trip.com 핫딜", "tripcom", "https://kr.trip.com/",
        {"hotel": CatalogBranch("hotel", "국내 럭셔리 호텔 5만원 찬스", "hotel", themes={
            "deal": CatalogTheme("deal", "08/14 · 웨스틴 조선 부산", "deal", {})
        })},
        metadata={"authoritative_dynamic_catalog": True},
    )

    merged, applied, pending, error = CatalogService._merge_safe(
        object.__new__(CatalogService), old, new
    )

    assert not pending and not error
    assert set(merged.branches) == {"hotel"}
    assert any(change.kind == "removed" and change.old_id == "old" for change in applied)
