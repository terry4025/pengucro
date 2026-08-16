from engines.cgv_client import (
    CGV_IMAX_SITE_NOS,
    CgvRegion,
    CgvSeat,
    CgvSeatGroup,
    CgvSite,
    build_seat_guide,
    build_seat_hold_payload,
    build_seat_price_payload,
    can_extend_contiguous_seat_group,
    can_extend_physical_seat_group,
    choose_recommended_seat_group,
    filter_imax_catalog,
    is_contiguous_seat_group,
    is_imax_site,
    parse_api_seats,
    parse_seat_groups,
    parse_dom_seats,
    parse_site_catalog,
    parse_site_list,
    recommend_cgv_seats,
    seat_layout_columns,
    seat_row_sort_key,
    schedule_items,
    select_schedule,
)


def test_parse_seat_groups_keeps_priority_and_expands_anchor():
    assert parse_seat_groups("A22,A23 | B10,B11", 2) == (
        CgvSeatGroup(("A22", "A23")),
        CgvSeatGroup(("B10", "B11")),
    )
    assert parse_seat_groups("H10", 2) == (
        CgvSeatGroup(("H10", "H11")),
        CgvSeatGroup(("H9", "H10")),
    )


def test_multi_person_groups_require_same_row_adjacent_seats():
    assert is_contiguous_seat_group(("F12", "F13"), 2) is True
    assert is_contiguous_seat_group(("F12", "F24"), 2) is False
    assert is_contiguous_seat_group(("F12", "G12"), 2) is False
    assert is_contiguous_seat_group(("F12",), 1) is True
    assert parse_seat_groups("F12,F24 | F12,F13 | G12,G13", 2) == (
        CgvSeatGroup(("F12", "F13")),
        CgvSeatGroup(("G12", "G13")),
    )


def test_partial_group_can_only_extend_into_one_future_adjacent_block():
    assert can_extend_contiguous_seat_group(("F12",), "F14", 3) is True
    assert can_extend_contiguous_seat_group(("F12",), "F15", 3) is False
    assert can_extend_contiguous_seat_group(("F12",), "G13", 3) is False


def test_parse_site_list_handles_nested_official_response():
    payload = {
        "data": {
            "regions": [
                {"regionNm": "서울", "sites": [{"siteNo": "0013", "siteNm": "용산아이파크몰"}]}
            ]
        }
    }

    assert parse_site_list(payload) == {"CGV 용산아이파크몰": "0013"}


def test_parse_current_bff_region_and_site_catalog():
    payload = {
        "data": {
            "regionInfo": [{"comCdval": "02", "comCdvalNm": "경기", "cnt": "69"}],
            "siteInfo": [{"regnGrpCd": "02", "siteNo": "0013", "siteNm": "용산아이파크몰"}],
        }
    }

    regions, sites = parse_site_catalog(payload)

    assert regions[0].name == "경기"
    assert regions[0].count == 69
    assert sites[0].site_no == "0013"
    assert sites[0].label == "CGV 용산아이파크몰"


def test_parse_dom_seats_keeps_unavailable_physical_seats_selectable_as_data():
    seats = parse_dom_seats(
        [
            {"id": "loc-a22", "label": "A22", "disabled": False},
            {"id": "loc-a23", "label": "A23", "disabled": True},
        ]
    )

    assert [seat.label for seat in seats] == ["A22", "A23"]
    assert seats[0].available is True
    assert seats[1].available is False


def test_schedule_items_finds_nested_screenings_and_selects_exact_time():
    schedule = {
        "siteNo": "0013",
        "scnYmd": "20260818",
        "scnsNo": "01",
        "scnSseq": "5",
        "scnsrtTm": "1000",
        "movNm": "오디세이",
        "expoScnsNm": "IMAX관",
        "movkndDsplEnm": "IMAX LASER 2D",
        "frSeatCnt": 624,
    }
    payload = {"data": {"movieList": [{"scheduleList": [schedule]}]}}

    assert schedule_items(payload) == [schedule]
    assert select_schedule(
        payload,
        movie="오디세이",
        show_time="10:00",
        auditorium="IMAX",
    ) == schedule


def test_select_schedule_keeps_sold_out_screening_for_cancellation_watch():
    payload = {
        "data": [
            {
                "siteNo": "0013",
                "scnYmd": "20260818",
                "scnsNo": "01",
                "scnSseq": "5",
                "scnsrtTm": "1000",
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
                "frSeatCnt": 0,
            }
        ]
    }

    assert select_schedule(payload, movie="오디세이", show_time="10:00", auditorium="IMAX") == payload["data"][0]


def test_parse_api_seats_keeps_available_and_sold_physical_seats():
    payload = {
        "data": {
            "items": [{
                "seats": [
                    {
                        "seatLocNo": "loc-a22", "seatRowNm": "A", "seatNo": "22",
                        "seatStusCd": "00", "seatSaleYn": "Y", "sbordNo": "001",
                        "seatAreaNo": "001", "szoneNo": "02001", "szoneKindCd": "02",
                        "stkndCd": "01", "seatSalfrmCd": "04",
                        "xcoordStartVal": "120", "ycoordStartVal": "80",
                        "xcoordEndVal": "140", "ycoordEndVal": "100",
                        "leftPwayYn": "Y", "rghtPwayYn": "N",
                    },
                    {
                        "seatLocNo": "loc-a23", "seatRowNm": "A", "seatNo": "23",
                        "seatStusCd": "01", "seatSaleYn": "Y",
                    },
                ]
            }]
        }
    }

    seats = parse_api_seats(payload)

    assert [seat.label for seat in seats] == ["A22", "A23"]
    assert seats[0].available is True
    assert seats[0].szone_no == "02001"
    assert seats[0].xcoord_start == "120"
    assert seats[0].left_passage is True
    assert seats[1].available is False


def test_build_official_price_and_hold_payloads():
    schedule = {
        "coCd": "A420", "siteNo": "0013", "scnYmd": "20260818",
        "scnsNo": "018", "scnSseq": "2", "movNo": "30001323",
        "prcrulDivCd": "01",
    }
    seats = (
        CgvSeat(
            "loc-a22", "A22", "A", 22, True, True, "001", "001", "02001",
            "02", "01", "04", "00", "Y",
        ),
        CgvSeat(
            "loc-a23", "A23", "A", 23, True, True, "001", "001", "02001",
            "02", "01", "04", "00", "Y",
        ),
    )

    price = build_seat_price_payload(schedule, seats, 2)
    hold = build_seat_hold_payload(schedule, seats, cust_no="member-1")

    assert price["prodBnduList"] == [{"prodBnduCd": "01", "prodBnduQty": 2}]
    assert price["seatList"][0]["seatLocNo"] == "loc-a22"
    assert hold["custNo"] == "member-1"
    assert hold["seatPrmpDataList"][1] == {
        "seatRowNm": "A", "seatNo": "23", "seatLocNo": "loc-a23",
        "sbordNo": "001", "seatAreaNo": "001", "szoneNo": "02001",
    }


def test_yongsan_imax_guide_uses_real_center_and_preference_tiers():
    seats = tuple(
        CgvSeat(f"{row}-{number}", f"{row}{number}", row, number, True)
        for row in "ABCDEFGHIJ"
        for number in range(1, 46)
    )

    guide = build_seat_guide(
        site_no="0013", auditorium="IMAX관", format_name="IMAX LASER 2D"
    )
    recommendations = recommend_cgv_seats(
        seats,
        site_no="0013",
        auditorium="IMAX관",
        format_name="IMAX LASER 2D",
    )

    assert guide.dedicated is True
    assert "용산 IMAX" in guide.title
    assert recommendations["H23"].tier == "best"
    assert recommendations["G23"].tier == "recommended"
    assert recommendations["F23"].tier == "preference"
    assert "H1" not in recommendations


def test_general_seat_recommendation_adapts_to_actual_row_and_seat_count():
    seats = tuple(
        CgvSeat(f"{row}-{number}", f"{row}{number}", row, number, True)
        for row in "ABCDEFGH"
        for number in range(1, 13)
    )

    recommendations = recommend_cgv_seats(
        seats, site_no="9999", auditorium="3관", format_name="2D"
    )

    assert recommendations["F6"].tier == "best"
    assert recommendations["F7"].tier == "best"
    assert "F1" not in recommendations


def test_seat_rows_are_sorted_naturally_instead_of_api_zone_order():
    assert sorted(("E", "L", "O", "D", "N", "A"), key=seat_row_sort_key) == [
        "A", "D", "E", "L", "N", "O"
    ]

    seats = tuple(
        CgvSeat(f"{row}-{number}", f"{row}{number}", row, number, True)
        for row in ("E", "L", "O", "D", "N", "A", "B", "C", "F", "G")
        for number in range(1, 11)
    )
    recommendations = recommend_cgv_seats(
        seats, site_no="9999", auditorium="3관", format_name="2D"
    )

    assert recommendations["G5"].tier == "best"


def test_layout_uses_official_coordinates_to_preserve_wide_aisles():
    seats = (
        CgvSeat("a1", "A1", "A", 1, True, xcoord_start="100"),
        CgvSeat("a2", "A2", "A", 2, True, xcoord_start="120"),
        CgvSeat("a3", "A3", "A", 3, True, xcoord_start="180"),
    )

    assert seat_layout_columns(seats) == {"A1": 0, "A2": 1, "A3": 4}


def test_auto_best_seat_selects_people_sized_contiguous_group_and_skips_aisle():
    seats = tuple(
        CgvSeat(
            f"H-{number}", f"H{number}", "H", number, False,
            right_passage=number == 22,
        )
        for number in range(19, 27)
    )
    recommendations = {
        seat.label: type("Recommendation", (), {"tier": "best"})()
        for seat in seats
    }

    group = choose_recommended_seat_group(seats, recommendations, 2, mode="balanced")

    assert group in {("H21", "H22"), ("H23", "H24")}
    assert group != ("H22", "H23")
    assert can_extend_physical_seat_group(seats, ("H22",), "H23", 2) is False
    assert can_extend_physical_seat_group(seats, ("H23",), "H24", 2) is True


def test_auto_best_seat_can_find_next_priority_and_yongsan_preference_mode():
    seats = tuple(
        CgvSeat(f"{row}-{number}", f"{row}{number}", row, number, False)
        for row in ("F", "G", "H", "I", "J")
        for number in range(20, 26)
    )
    recommendations = recommend_cgv_seats(
        seats, site_no="0013", auditorium="IMAX관", format_name="IMAX LASER 2D"
    )

    first = choose_recommended_seat_group(seats, recommendations, 2, mode="balanced")
    second = choose_recommended_seat_group(
        seats, recommendations, 2, mode="balanced", excluded=(first or (),)
    )
    immersive = choose_recommended_seat_group(
        seats, recommendations, 2, mode="immersive"
    )

    assert first is not None and first[0].startswith("H")
    assert second is not None and second != first
    assert immersive is not None and immersive[0][0] in {"F", "G"}


def test_filter_imax_catalog_keeps_only_imax_sites_and_recomputes_region_counts():
    regions = (
        CgvRegion("01", "서울", 30),
        CgvRegion("02", "경기", 60),
        CgvRegion("03", "제주", 2),
    )
    sites = (
        CgvSite("0013", "용산아이파크몰", "01"),
        CgvSite("0001", "강남", "01"),
        CgvSite("0074", "왕십리", "01"),
        CgvSite("0181", "판교", "02"),
        CgvSite("9999", "일반영화관", "03"),
    )

    filtered_regions, filtered_sites = filter_imax_catalog(regions, sites)

    assert [site.site_no for site in filtered_sites] == ["0013", "0074", "0181"]
    assert len(filtered_regions) == 2
    assert filtered_regions[0].name == "서울"
    assert filtered_regions[0].count == 2
    assert filtered_regions[1].name == "경기"
    assert filtered_regions[1].count == 1


def test_is_imax_site_detects_by_site_no_and_metadata():
    assert is_imax_site(CgvSite("0013", "용산아이파크몰", "01")) is True
    assert is_imax_site(CgvSite("0001", "강남", "01")) is False
    assert is_imax_site({"siteNo": "0074", "siteNm": "왕십리"}) is True
    assert is_imax_site({"siteNo": "9999", "siteNm": "테스트", "hallNm": "IMAX 3D"}) is True


def test_select_schedule_prioritizes_preferred_times_in_order():
    payload = {
        "data": [
            {"movNm": "오디세이", "expoScnsNm": "IMAX관", "scnsrtTm": "1000", "scnsNo": "1", "scnSseq": "1"},
            {"movNm": "오디세이", "expoScnsNm": "IMAX관", "scnsrtTm": "1400", "scnsNo": "2", "scnSseq": "2"},
            {"movNm": "오디세이", "expoScnsNm": "IMAX관", "scnsrtTm": "1800", "scnsNo": "3", "scnSseq": "3"},
            {"movNm": "오디세이", "expoScnsNm": "IMAX관", "scnsrtTm": "2130", "scnsNo": "4", "scnSseq": "4"},
        ]
    }

    # 1st preference is 18:00
    chosen = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        preferred_times=["18:00", "14:00"],
    )
    assert chosen is not None
    assert chosen["scnsrtTm"] == "1800"

    # 1st preference is 12:00 (not available), 2nd is 21:30 (available)
    chosen2 = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        preferred_times=["12:00", "21:30"],
    )
    assert chosen2 is not None
    assert chosen2["scnsrtTm"] == "2130"

    # Backward compatibility with single show_time
    chosen3 = select_schedule(
        payload,
        movie="오디세이",
        show_time="10:00",
        auditorium="IMAX",
    )
    assert chosen3 is not None
    assert chosen3["scnsrtTm"] == "1000"


def test_select_schedule_matches_explicit_formats_and_preferred_times():
    payload = {
        "data": [
            {
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
                "movkndDsplEnm": "IMAX LASER 2D",
                "scnsrtTm": "1400",
                "scnsNo": "1",
                "scnSseq": "1",
            },
            {
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
                "movkndDsplEnm": "IMAX 3D",
                "scnsrtTm": "1400",
                "scnsNo": "2",
                "scnSseq": "2",
            },
            {
                "movNm": "오디세이",
                "expoScnsNm": "IMAX관",
                "movkndDsplEnm": "IMAX 3D",
                "scnsrtTm": "1730",
                "scnsNo": "3",
                "scnSseq": "3",
            },
        ]
    }

    # Test A: format = "IMAX LASER 2D" selects the 2D schedule at 14:00
    chosen_2d = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        format_name="IMAX LASER 2D",
        preferred_times=["14:00"],
    )
    assert chosen_2d is not None
    assert chosen_2d["scnSseq"] == "1"
    assert chosen_2d["movkndDsplEnm"] == "IMAX LASER 2D"

    # Test B: format = "IMAX 3D" selects the 3D schedule at 14:00
    chosen_3d = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        format_name="IMAX 3D",
        preferred_times=["14:00"],
    )
    assert chosen_3d is not None
    assert chosen_3d["scnSseq"] == "2"
    assert chosen_3d["movkndDsplEnm"] == "IMAX 3D"

    # Test C: format = "" (legacy) matches first matching by time
    chosen_legacy = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        format_name="",
        preferred_times=["14:00"],
    )
    assert chosen_legacy is not None

    # Test D: preferred times with format filter: 17:30 (3D) -> 14:00 (2D) with format="IMAX LASER 2D"
    chosen_pref = select_schedule(
        payload,
        movie="오디세이",
        auditorium="IMAX",
        format_name="IMAX LASER 2D",
        preferred_times=["17:30", "14:00"],
    )
    # 17:30 only exists in 3D, so 14:00 2D should be selected
    assert chosen_pref is not None
    assert chosen_pref["scnsrtTm"] == "1400"
    assert chosen_pref["scnSseq"] == "1"


