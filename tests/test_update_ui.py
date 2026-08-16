from ui.main_window import MainWindow
from ui.update_dialog import UpdateDialog


def test_titlebar_update_indicator_has_all_user_facing_states():
    assert {
        state: style[2]
        for state, style in MainWindow._UPDATE_INDICATOR_STYLES.items()
    } == {
        "available": "업데이트",
        "downloading": "0%",
        "ready": "재시작",
        "deferred": "예약 후",
        "error": "재시도",
    }
    assert {"checking", "up_to_date", "background_error"}.issubset(
        MainWindow._HIDDEN_UPDATE_STATES
    )


def test_update_dialog_actions_are_explicit_and_safe():
    assert UpdateDialog._STATE_COPY["available"][-1] == "download"
    assert UpdateDialog._STATE_COPY["ready"][-1] == "restart"
    assert UpdateDialog._STATE_COPY["error"][-1] == "retry"
    assert UpdateDialog._STATE_COPY["downloading"][-1] == ""
    assert UpdateDialog._STATE_COPY["deferred"][-1] == ""


def test_update_size_formatting_is_human_readable():
    assert UpdateDialog._format_size(None) == ""
    assert UpdateDialog._format_size(512) == "다운로드 크기 512 B"
    assert UpdateDialog._format_size(2048) == "다운로드 크기 2 KB"
    assert UpdateDialog._format_size(2 * 1024 * 1024) == "다운로드 크기 2.0 MB"


def test_update_dialog_version_text_has_no_v_prefix():
    source = __import__("inspect").getsource(UpdateDialog.update_state)
    assert "새 버전 v" not in source
    assert "새 버전 {version_text}" in source
