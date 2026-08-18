from ui.cgv_booking_dialog_controls import CgvBookingDialog
from ui.cgv_booking_dialog_runtime import CgvBookingDialog as RuntimeCgvBookingDialog


class _FakeWidget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.kwargs = dict(kwargs)
        self.pack_calls = []
        self.forgotten = False

    def pack(self, **kwargs):
        self.pack_calls.append(dict(kwargs))

    def pack_forget(self):
        self.forgotten = True


def test_final_selector_extends_existing_runtime():
    assert issubclass(CgvBookingDialog, RuntimeCgvBookingDialog)


def test_seat_map_priority_button_moves_into_always_visible_manual_row(monkeypatch):
    import ui.cgv_booking_dialog_controls as controls

    monkeypatch.setattr(
        RuntimeCgvBookingDialog,
        "_install_manual_seat_controls",
        lambda _self: None,
    )
    monkeypatch.setattr(controls.ctk, "CTkButton", _FakeWidget)

    dialog = object.__new__(CgvBookingDialog)
    old_actions = _FakeWidget()
    old_priority_button = _FakeWidget(old_actions)
    input_row = _FakeWidget()
    utility_row = _FakeWidget()

    dialog.add_priority_button = old_priority_button
    dialog.manual_seat_entry = _FakeWidget(input_row)
    dialog.remove_last_priority_button = _FakeWidget(utility_row)
    dialog.current_seats = set()
    dialog.priority_groups = []
    dialog.people = 2

    dialog._install_manual_seat_controls()

    assert old_priority_button.forgotten is True
    assert old_actions.forgotten is True
    assert dialog.seat_map_add_button.master is input_row
    assert dialog.seat_map_add_button.kwargs["text"] == "좌석도 선택 추가"
    assert dialog.seat_map_add_button.kwargs["state"] == "disabled"
    assert dialog.add_priority_button is dialog.seat_map_add_button
    assert dialog.clear_all_priority_button.master is utility_row
    assert dialog.clear_all_priority_button.kwargs["text"] == "전체 초기화"
