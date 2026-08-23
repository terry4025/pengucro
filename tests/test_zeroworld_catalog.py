from engines.zeroworld_catalog import (
    calendar_contains_date,
    find_target_time_slot,
    parse_theme_list,
    parse_time_slots,
    subject_for_branch,
)


def test_parse_three_branch_theme_markup_shape():
    html = """
    <a href="javascript:fun_theme_select('28','0')">
      <p class="themes__name">아이엠</p><span>75분</span>
    </a>
    <a href="javascript:fun_theme_select('36','1')">
      <p class="themes__name">사랑...하는...감?</p>
    </a>
    """
    assert parse_theme_list(html) == {"아이엠": "28", "사랑...하는...감?": "36"}


def test_parse_current_theme_markup_excludes_duration_and_people():
    html = """
    <a href="javascript:fun_theme_select('38','2')">
      <div class="choice-themes__detail">
        <p class="choice-themes__name">오르골</p>
        <p class="choice-themes__playtime">70</p>
        <p class="choice-themes__person">2-6인</p>
      </div>
    </a>
    """
    assert parse_theme_list(html) == {"오르골": "38"}


def test_dive_branch_uses_separate_subject():
    assert subject_for_branch("2") == "B"
    assert subject_for_branch("1") == "A"


def test_parse_time_slots_distinguishes_available_and_closed():
    html = """
    <a class="choice-time__time" href="javascript:fun_theme_time_select('435','0')">11:00</a>
    <a class="choice-time__time disable">21:20</a>
    """
    slots = parse_time_slots(html)
    assert [(slot.time, slot.slot_id, slot.available) for slot in slots] == [
        ("11:00", "435", True),
        ("21:20", "", False),
    ]


def test_parse_time_slots_keeps_disabled_future_buttons():
    html = """
    <button class="choice-time__time disabled" disabled>09:45</button>
    <button class="choice-time__time" onclick="fun_theme_time_select('901','1')">10:55</button>
    """
    slots = parse_time_slots(html)
    assert [(slot.time, slot.available) for slot in slots] == [
        ("09:45", False),
        ("10:55", True),
    ]


def test_fast_target_parser_keeps_count_and_closed_slot_id():
    html = """
    <a class="choice-time__time" href="javascript:fun_theme_time_select('435','0')">
      <span>11:00</span>
    </a>
    <a class="choice-time__time disabled"
       href="javascript:fun_theme_time_select('901','1')"><strong>21:20</strong></a>
    """

    slot, count = find_target_time_slot(html, "21:20")

    assert count == 2
    assert slot is not None
    assert slot.slot_id == "901"
    assert slot.available is False


def test_fast_target_parser_prefers_available_duplicate():
    html = """
    <button class="disabled" onclick="fun_theme_time_select('old')">09:45</button>
    <button onclick="fun_theme_time_select('new')">09:45</button>
    """

    slot, count = find_target_time_slot(html, "09:45")

    assert count == 2
    assert slot is not None
    assert slot.slot_id == "new"
    assert slot.available is True


def test_calendar_date_detection_matches_live_markup_contract():
    assert calendar_contains_date("javascript:fun_days_select('2026-08-01','0')", "2026-08-01")
    assert not calendar_contains_date("", "2026-08-01")
