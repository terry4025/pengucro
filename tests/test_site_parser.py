from engines.site_parser import (
    _extract_booking_url_from_text,
    extract_place_id_and_product_id,
)


def test_extracts_escaped_booking_url_from_performance_page_state():
    raw = (
        r'{"target":"https:\/\/booking.naver.com\/booking\/12\/bizes\/'
        r'123456\/items\/987654"}'
    )

    assert _extract_booking_url_from_text(raw) == (
        "https://booking.naver.com/booking/12/bizes/123456/items/987654"
    )


def test_extracts_product_id_nested_inside_map_place_path():
    url = (
        "https://map.naver.com/p/place/2089320412?"
        "placePath=%2FfestivalPerformance%2Fperformance%3FbookingProductId%3D987654"
    )

    assert extract_place_id_and_product_id(url) == ("2089320412", "987654")
