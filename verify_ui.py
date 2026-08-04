import os
import sys
import time
import tempfile
import unittest

_TEST_DATA_DIR = tempfile.TemporaryDirectory(prefix="pengucro_ui_test_")
os.environ["PENGUCRO_DATA_DIR"] = _TEST_DATA_DIR.name

# Ensure the project root is in python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Prevent playing actual sounds during tests
import winsound
winsound.MessageBeep = lambda *args, **kwargs: None

import customtkinter as ctk
import ui.theme as theme
from ui.main_window import MainWindow
from ui.log_panel import LogPanel
from ui.reservation_form import ReservationForm
from pengucro.models import NAVER_MODE, STANDARD_MODE

# Monkey-patch MainWindow to avoid start-up network fetch of themes in background
MainWindow._start_jigubyeol_theme_fetcher = lambda self: None
MainWindow._start_zeroworld_theme_fetcher = lambda self: None
MainWindow._start_catalog_auto_refresh = lambda self: None

class TestUIComponents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize ctk/tkinter app context
        cls.app = MainWindow()
        # Withdraw is NOT used here so events and focus map correctly.
        # Let's update and wait for set_appwindow (10ms asynchronously) to settle.
        cls.app.update()
        time.sleep(0.2)
        cls.app.update()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        _TEST_DATA_DIR.cleanup()

    def test_1_mainwindow_title_bar(self):
        print("\n--- Verifying MainWindow Title Bar Traffic Lights & Pin ---")

        # Borderless Tk windows use a separate native parent for the taskbar.
        # Verify that both icon sizes were loaded for that Win32 window.
        self.assertEqual(
            len(self.app._native_icon_handles),
            2,
            "The taskbar window must have both large and small native icons",
        )
        self.assertTrue(all(self.app._native_icon_handles))
        print("[Pass] Native taskbar icon handles are applied to the borderless window.")
        
        # Verify title bar exists
        self.assertIsNotNone(self.app.title_bar, "Title bar should not be None")
        self.assertEqual(self.app.title_bar.cget("height"), 36, "Title bar height should be 36")
        self.assertEqual(self.app.title_bar.cget("fg_color"), theme.SURFACE_COLOR, f"Title bar fg_color should be theme.SURFACE_COLOR ({theme.SURFACE_COLOR})")
        print("[Pass] MainWindow Title Bar exists with correct height & surface color.")

        # Find dots_frame (Mac traffic light container on the left)
        self.assertIsNotNone(self.app.close_btn, "Close button should not be None")
        self.assertIsNotNone(self.app.min_btn, "Minimize button should not be None")
        self.assertIsNotNone(self.app.max_btn, "Maximize button should not be None")

        # Let's inspect the order of packaging of close, min, max buttons inside dots_frame
        dots_frame = self.app.close_btn.master
        self.assertIsNotNone(dots_frame, "dots_frame should exist as close_btn's parent")
        
        # Let's verify that dots_frame is packed on the right of title_bar
        dots_frame_info = dots_frame.pack_info()
        self.assertEqual(dots_frame_info.get("side"), "right", "dots_frame must be packed on the right")
        self.assertEqual(int(dots_frame_info.get("padx", 0)), 8, "window controls padx must be 8")
        print("[Pass] Window controls are packed on the right with correct padding.")

        # Get dots_frame children and check their order (only match CTkButton widgets)
        dots_children = [child for child in dots_frame.winfo_children() if isinstance(child, ctk.CTkButton)]
        self.assertEqual(len(dots_children), 3, "dots_frame must contain exactly 3 CTkButton children")
        
        self.assertEqual(dots_children[0], self.app.min_btn, "First control must be Minimize")
        self.assertEqual(dots_children[1], self.app.max_btn, "Second control must be Maximize")
        self.assertEqual(dots_children[2], self.app.close_btn, "Third dot must be the Close button")
        self.assertEqual(self.app.min_btn.cget("text"), "—")
        self.assertEqual(self.app.max_btn.cget("text"), "□")
        self.assertEqual(self.app.close_btn.cget("text"), "×")
        for control in dots_children:
            self.assertEqual(control.cget("width"), 30)
            self.assertEqual(control.cget("height"), 26)
        print("[Pass] Accessible minimize, maximize and close controls are verified.")

        # Verify Pin button is packed on the left
        self.assertIsNotNone(self.app.pin_btn, "Pin button should not be None")
        pin_info = self.app.pin_btn.pack_info()
        self.assertEqual(pin_info.get("side"), "left", "Pin button must be packed on the left")
        self.assertEqual(self.app.pin_btn.cget("text"), "📌", "Pin button text must be 📌")
        self.assertEqual(self.app.pin_btn.cget("fg_color"), "transparent", "Pin button fg_color must be transparent")
        self.assertEqual(self.app.pin_btn.cget("hover_color"), theme.CARD_COLOR, "Pin button hover_color must be CARD_COLOR")
        print("[Pass] Pin button is verified on the left with correct styling.")

    def test_2_log_panel(self):
        print("\n--- Verifying LogPanel Background, Scrollbars & Tag config ---")
        
        # Verify LogPanel exists
        self.assertIsNotNone(self.app.log_panel, "log_panel should not be None")
        log_panel = self.app.log_panel
        
        # Verify high-contrast terminal background
        self.assertEqual(log_panel.textbox.cget("fg_color"), "#050505", "LogPanel textbox background should be #050505")
        self.assertEqual(log_panel.textbox.cget("text_color"), theme.TEXT_BODY, "LogPanel textbox text_color should be theme.TEXT_BODY")
        self.assertEqual(log_panel.textbox.cget("font"), theme.FONT_MONO, "LogPanel textbox font should be theme.FONT_MONO")
        
        # Verify custom scrollbar parameters (accessed via inner _y_scrollbar)
        self.assertEqual(log_panel.textbox._y_scrollbar._button_color, theme.HAIRLINE_COLOR, "LogPanel scrollbar button color should be theme.HAIRLINE_COLOR")
        self.assertEqual(log_panel.textbox._y_scrollbar._button_hover_color, theme.CARD_COLOR, "LogPanel scrollbar button hover color should be theme.CARD_COLOR")
        print("[Pass] LogPanel textbox background (#050505), typography, and custom scrollbar colors are correct.")

        # Verify tag configuration setup on underlying tk.Text widget
        text_widget = log_panel.textbox._textbox
        
        # Let's inspect the tags config
        info_fg = text_widget.tag_cget("info", "foreground")
        success_fg = text_widget.tag_cget("success", "foreground")
        error_fg = text_widget.tag_cget("error", "foreground")
        warning_fg = text_widget.tag_cget("warning", "foreground")

        self.assertEqual(info_fg, theme.TEXT_PRIMARY, "Tag info foreground should be theme.TEXT_PRIMARY")
        self.assertEqual(success_fg, theme.ACCENT_GREEN, "Tag success foreground should be theme.ACCENT_GREEN")
        self.assertEqual(error_fg, theme.ACCENT_RED, "Tag error foreground should be theme.ACCENT_RED")
        self.assertEqual(warning_fg, theme.ACCENT_YELLOW, "Tag warning foreground should be theme.ACCENT_YELLOW")
        print("[Pass] LogPanel standard message tags (info, success, error, warning) configured correctly.")

        # Verify category brackets tags config
        cat_default_fg = text_widget.tag_cget("cat_default", "foreground")
        cat_captcha_fg = text_widget.tag_cget("cat_captcha", "foreground")
        cat_warning_fg = text_widget.tag_cget("cat_warning", "foreground")
        cat_error_fg = text_widget.tag_cget("cat_error", "foreground")
        cat_success_fg = text_widget.tag_cget("cat_success", "foreground")
        cat_device_fg = text_widget.tag_cget("cat_device", "foreground")

        self.assertEqual(cat_default_fg, theme.TEXT_MUTE)
        self.assertEqual(cat_captcha_fg, theme.ACCENT_BLUE)
        self.assertEqual(cat_warning_fg, theme.ACCENT_YELLOW)
        self.assertEqual(cat_error_fg, theme.ACCENT_RED)
        self.assertEqual(cat_success_fg, theme.ACCENT_GREEN)
        self.assertEqual(cat_device_fg, "#BF5AF2")
        print("[Pass] LogPanel category highlight tags (captcha, warning, error, success, device, default) configured correctly.")

        # Verify category parsing highlights empirically
        # We will mock the insert method of log_panel's underlying text widget to capture calls
        inserted_items = []
        original_insert = text_widget.insert
        
        def mock_insert(index, chars, *args):
            inserted_items.append((chars, args[0] if args else None))
            original_insert(index, chars, *args)
            
        text_widget.insert = mock_insert
        
        try:
            # Clear previous logs and test parsing
            log_panel.clear_log()
            
            # Test Captcha category
            log_panel.append_log("[YesCaptcha] 토큰 생성 완료", "success")
            self.assertEqual(inserted_items[-2], ("[YesCaptcha]", "cat_captcha"), "Category YesCaptcha should map to cat_captcha")
            self.assertEqual(inserted_items[-1], (" 토큰 생성 완료\n", "success"), "Body should map to log_type success")
            print("[Pass] Parsing category '[YesCaptcha]' maps to 'cat_captcha' correctly.")

            # Test Warning category
            log_panel.append_log("[경고] 구글 캡차 유효시간 2분 만료 예고", "warning")
            self.assertEqual(inserted_items[-2], ("[경고]", "cat_warning"), "Category 경고 should map to cat_warning")
            self.assertEqual(inserted_items[-1], (" 구글 캡차 유효시간 2분 만료 예고\n", "warning"), "Body should map to log_type warning")
            print("[Pass] Parsing category '[경고]' maps to 'cat_warning' correctly.")

            # Test Error/Failure category
            log_panel.append_log("[실패] 서버 통신 에러", "error")
            self.assertEqual(inserted_items[-2], ("[실패]", "cat_error"), "Category 실패 should map to cat_error")
            self.assertEqual(inserted_items[-1], (" 서버 통신 에러\n", "error"), "Body should map to log_type error")
            print("[Pass] Parsing category '[실패]' maps to 'cat_error' correctly.")

            log_panel.append_log("[에러] 예약 슬롯 선점 실패", "error")
            self.assertEqual(inserted_items[-2], ("[에러]", "cat_error"), "Category 에러 should map to cat_error")
            print("[Pass] Parsing category '[에러]' maps to 'cat_error' correctly.")

            # Test Success/Completion category
            log_panel.append_log("[성공] 예약 제출 완료!", "success")
            self.assertEqual(inserted_items[-2], ("[성공]", "cat_success"), "Category 성공 should map to cat_success")
            print("[Pass] Parsing category '[성공]' maps to 'cat_success' correctly.")

            log_panel.append_log("[완료] 로딩 완료", "info")
            self.assertEqual(inserted_items[-2], ("[완료]", "cat_success"), "Category 완료 should map to cat_success")
            print("[Pass] Parsing category '[완료]' maps to 'cat_success' correctly.")

            # Test Device category
            log_panel.append_log("[기기] 브라우저 세션 감지", "info")
            self.assertEqual(inserted_items[-2], ("[기기]", "cat_device"), "Category 기기 should map to cat_device")
            print("[Pass] Parsing category '[기기]' maps to 'cat_device' correctly.")

            # Test Default category
            log_panel.append_log("[시스템] 정시 예약 대기 중", "info")
            self.assertEqual(inserted_items[-2], ("[시스템]", "cat_default"), "Category 시스템 should map to cat_default")
            print("[Pass] Parsing category '[시스템]' maps to 'cat_default' correctly.")

            # Test message without brackets
            log_panel.append_log("일반 정보 로그 메시지", "info")
            self.assertEqual(inserted_items[-1], ("일반 정보 로그 메시지\n", "info"), "Normal messages without brackets should pass directly")
            print("[Pass] Normal message parsing without brackets passed directly.")

        finally:
            # Restore original insert method
            text_widget.insert = original_insert

    def test_3_reservation_form_entries(self):
        print("\n--- Verifying ReservationForm Entry Widgets and Focus Events ---")
        
        # Verify ReservationForm exists
        self.assertIsNotNone(self.app.form, "ReservationForm should not be None")
        form = self.app.form

        # Check custom_theme_checkbox so that theme_pk_entry is packed, mapped, and visible
        form.custom_theme_checkbox.select()
        form._toggle_custom_theme()
        self.app.update()

        # List of entry widgets to verify
        entries = {
            "theme_pk_entry": form.theme_pk_entry,
            "date_entry": form.date_entry,
            "time_entry": form.time_entry,
            "name_entry": form.name_entry,
            "people_entry": form.people_entry,
            "phone_entry": form.phone_entry
        }

        for name, entry in entries.items():
            print(f"Checking {name}...")
            
            # Verify font family and sizing
            font_prop = entry.cget("font")
            self.assertEqual(font_prop, theme.FONT_BODY_MD, f"{name} font should be theme.FONT_BODY_MD")
            
            # Verify border width is thin hairline (1)
            self.assertEqual(entry.cget("border_width"), 1, f"{name} border_width should be 1")
            
            # Ensure widget is normal for focus event testing
            original_state = entry.cget("state")
            entry.configure(state="normal")
            
            # 1. Reset border color to HAIRLINE_COLOR (when not focused)
            entry.configure(border_color=theme.HAIRLINE_COLOR)
            self.assertEqual(entry.cget("border_color"), theme.HAIRLINE_COLOR)
            
            # 2. Simulate FocusIn via event_generate on the inner _entry
            entry._entry.event_generate("<FocusIn>")
            self.app.update()
            
            # Verify border color changed to ACCENT_BLUE (Glow)
            self.assertEqual(
                entry.cget("border_color"), 
                theme.ACCENT_BLUE, 
                f"{name} border_color should change to ACCENT_BLUE on FocusIn"
            )
            
            # 3. Simulate FocusOut via event_generate on the inner _entry
            entry._entry.event_generate("<FocusOut>")
            self.app.update()
            
            # Verify border color restored to HAIRLINE_COLOR
            self.assertEqual(
                entry.cget("border_color"), 
                theme.HAIRLINE_COLOR, 
                f"{name} border_color should restore to HAIRLINE_COLOR on FocusOut"
            )
            
            # Restore state
            entry.configure(state=original_state)
            print(f"[Pass] {name} verified: correct font, thin hairline border, FocusIn glow highlight (#0A84FF), and FocusOut restore (#38383A).")

    def test_4_engine_mode_change_filtering(self):
        print("\n--- Verifying Engine Mode Change Filtering ---")

        def choose_mode(mode):
            # Mirror a real segmented-button click: its value changes before
            # ReservationForm receives the command callback.
            self.app.form.engine_mode_btn.set(mode)
            self.app.form._on_mode_change(mode)
            self.app.update()
        
        # Test change to Naver (Playwright) mode
        choose_mode(NAVER_MODE)
        
        # Site options should contain either Naver custom sites or the placeholder
        naver_options = self.app.site_dropdown.cget("values")
        self.assertTrue(len(naver_options) >= 1)
        if naver_options[0] == "(네이버 예약을 등록하세요)":
            print("[Pass] Correct placeholder shown when no Naver custom sites exist.")
        else:
            self.assertTrue(all(self.app.custom_sites[k].get("style") == "naver" for k in naver_options))
            print("[Pass] Correct Naver custom sites filtered.")
        self.assertEqual(self.app.form.threads_slider.cget("state"), "disabled")
        self.assertEqual(int(self.app.form.threads_slider.get()), 1)
        self.assertEqual(self.app.form.naver_threads, 1)
        self.assertEqual(
            self.app.form.yescaptcha_frame.winfo_manager(), "",
            "YesCaptcha controls must not be laid out in Naver mode",
        )
            
        # Test change to a Standard engine mode (e.g., 고속 (Async))
        choose_mode(STANDARD_MODE)
        
        std_options = self.app.site_dropdown.cget("values")
        self.assertTrue(len(std_options) >= len(self.app.default_site_names))
        self.assertTrue(any("제로월드" in opt for opt in std_options))
        self.assertEqual(float(self.app.form.threads_slider.cget("to")), 50.0)
        self.assertEqual(self.app.form.threads_slider.cget("state"), "normal")
        print("[Pass] Standard engine mode restores default site options correctly.")

        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        self.app.form.branch_var.set("다이브 건대점")
        self.app.form._on_branch_change("다이브 건대점")
        self.app.update()
        self.assertIn("다이브 건대점", self.app.form.branch_dropdown.cget("values"))
        self.assertEqual(
            set(self.app.form.theme_dropdown.cget("values")),
            {"인터뷰 (26.07.29 이전)", "인터뷰 (26.07.30 이후)", "오르골"},
        )
        print("[Pass] ZeroWorld Dive Konkuk branch and its three themes are selectable.")

    def test_5_add_site_dialog_parse_error_handling(self):
        print("\n--- Verifying AddSiteDialog Parse Error Exception Capture ---")
        
        from ui.main_window import AddSiteDialog
        
        callback_called = False
        error_passed = None
        
        def dummy_callback(result):
            pass
            
        dialog = AddSiteDialog(self.app, dummy_callback)
        dialog.update()
        
        # Overwrite _on_parse_error to capture the call
        orig_on_parse_error = dialog._on_parse_error
        def mock_on_parse_error(err_msg):
            nonlocal callback_called, error_passed
            callback_called = True
            error_passed = err_msg
            orig_on_parse_error(err_msg) # run the original as well to check safety
            
        dialog._on_parse_error = mock_on_parse_error
        
        # Trigger an exception inside parse_thread structure manually (or run the lambda direct test)
        try:
            raise ValueError("Test Parser Exception String")
        except Exception as e:
            err_msg = str(e)
            # Schedule call
            dialog.parent.after(0, lambda: dialog._on_parse_error(err_msg))
            
        # Let events process
        for _ in range(10):
            self.app.update()
            time.sleep(0.02)
            
        self.assertTrue(callback_called, "Parse error callback should have been called")
        self.assertEqual(error_passed, "Test Parser Exception String", "Callback should receive correct error message")
        
        # Verify status label is updated and buttons re-enabled
        self.assertIn("Test Parser Exception String", dialog.status_label.cget("text"))
        self.assertEqual(dialog.add_btn.cget("state"), "normal")
        self.assertEqual(dialog.cancel_btn.cget("state"), "normal")
        
        dialog._on_cancel()
        print("[Pass] AddSiteDialog exception handler runs safely, schedules lambda, and updates UI status badge.")

    def test_6_server_time_checkbox_disabled_handling(self):
        print("\n--- Verifying Server Time Checkbox Disable/Enable & Keyescape Sync Logic ---")

        # Give standard sites a distinctive value so a Keyescape round-trip
        # proves that the independent memories are not overwriting each other.
        self.app.form.standard_threads = 47
        self.app.form.threads_slider.set(47)
        self.app.form._on_threads_slider_move(47)
        
        # 1. Switch to Keyescape
        self.app.site_var.set("키이스케이프")
        self.app._on_site_change("키이스케이프")
        self.app.update()
        
        # Check if server time checkbox is enabled for Keyescape
        cb_state = self.app.form.show_server_time_checkbox.cget("state")
        self.assertEqual(cb_state, "normal", "Server time checkbox must be enabled for Keyescape")
        self.assertEqual(
            float(self.app.form.threads_slider.cget("to")),
            3.0,
            "Keyescape standby slider must be capped at three pages",
        )
        self.assertEqual(
            self.app.form.threads_slider.cget("state"),
            "normal",
            "Keyescape standby page count must be user-selectable",
        )
        self.app.form.threads_slider.set(3)
        self.app.form._on_threads_slider_move(3)
        self.assertEqual(self.app.form.keyescape_threads, 3)
        self.assertEqual(
            self.app.form.yescaptcha_frame.winfo_manager(), "grid",
            "YesCaptcha controls must be laid out for Keyescape",
        )
        
        # Checkbox select and toggle
        self.app.form.show_server_time_checkbox.select()
        self.app.form._toggle_server_time()
        self.app.update()
        time.sleep(0.5) # Allow sync thread to spawn and run
        self.app.update()
        
        # Check if sync thread is running
        self.assertTrue(self.app.is_sync_running, "Sync thread should be running for Keyescape when checked")
        
        # 2. Switch to Zero World (unsupported site)
        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        self.app.update()
        
        # Check if server time checkbox is deselected and disabled
        cb_val = self.app.form.show_server_time_checkbox.get()
        cb_state_new = self.app.form.show_server_time_checkbox.cget("state")
        self.assertEqual(cb_val, 0, "Server time checkbox must be deselected for Zero World")
        self.assertEqual(cb_state_new, "disabled", "Server time checkbox must be disabled for Zero World")
        self.assertFalse(self.app.is_sync_running, "Sync thread should be stopped for Zero World")
        self.assertEqual(
            float(self.app.form.threads_slider.cget("to")), 50.0,
            "Standard sites must restore the full 1-50 range after Keyescape",
        )
        self.assertEqual(self.app.form.threads_slider.cget("state"), "normal")
        self.assertEqual(int(self.app.form.threads_slider.get()), 47)
        self.assertEqual(
            self.app.form.yescaptcha_frame.winfo_manager(), "",
            "YesCaptcha controls must be removed outside Keyescape",
        )
        
        # Cleanup
        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        self.app.update()
        print("[Pass] Server time checkbox behaves correctly (deselected & disabled) on unsupported sites.")

    def test_6b_keyescape_naver_standard_thread_policy_round_trip(self):
        """All transition orders must reapply the complete engine policy."""
        print("\n--- Verifying Per-Engine Thread Policy Round Trip ---")
        form = self.app.form

        self.app.site_var.set("키이스케이프")
        self.app._on_site_change("키이스케이프")
        form.threads_slider.set(3)
        form._on_threads_slider_move(3)

        form.engine_mode_btn.set(NAVER_MODE)
        form._on_mode_change(NAVER_MODE)
        self.app.update()
        self.assertEqual(form.threads_slider.cget("state"), "disabled")
        self.assertEqual(int(form.threads_slider.get()), 1)
        self.assertEqual(form.naver_threads, 1)
        self.assertEqual(form.yescaptcha_frame.winfo_manager(), "")

        form.engine_mode_btn.set(STANDARD_MODE)
        form._on_mode_change(STANDARD_MODE)
        self.app.update()
        # MainWindow remembers Keyescape as the last standard site.
        self.assertEqual(self.app.site_var.get(), "키이스케이프")
        self.assertEqual(float(form.threads_slider.cget("to")), 3.0)
        self.assertEqual(int(form.threads_slider.get()), 3)
        self.assertEqual(form.yescaptcha_frame.winfo_manager(), "grid")

        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        self.app.update()
        self.assertEqual(float(form.threads_slider.cget("to")), 50.0)
        self.assertEqual(form.threads_slider.cget("state"), "normal")
        self.assertEqual(int(form.threads_slider.get()), 47)
        self.assertEqual(form.yescaptcha_frame.winfo_manager(), "")
        print("[Pass] Naver=1 locked, Keyescape=1-3, standard=1-50 with separate memory.")

    def test_7_dynamic_advanced_layout_and_original_loading_reveal(self):
        print("\n--- Verifying Dynamic Advanced Layout & Original Loading Reveal ---")

        form = self.app.form
        if form._advanced_visible:
            form._toggle_advanced()
            self.app.update()

        root_size = (self.app.winfo_width(), self.app.winfo_height())
        form_height = form.winfo_height()
        log_height = self.app.log_panel.winfo_height()
        self.assertIsInstance(form, ctk.CTkFrame)
        self.assertNotIsInstance(form, ctk.CTkScrollableFrame)

        form._toggle_advanced()
        for _ in range(5):
            self.app.update()
            time.sleep(0.02)

        self.assertGreater(
            int(form.threads_frame.grid_info()["row"]),
            int(form.advanced_toggle_btn.grid_info()["row"]),
            "Concurrent attempts must appear below the advanced toggle",
        )
        self.assertTrue(form.catalog_auto_refresh_var.get())
        self.assertTrue(form.catalog_auto_refresh_checkbox.winfo_ismapped())
        self.assertTrue(form.catalog_refresh_btn.winfo_ismapped())
        self.assertEqual(form.catalog_refresh_btn.cget("text"), "현재 사이트 갱신")
        self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), root_size)
        self.assertGreater(form.winfo_height(), form_height)
        self.assertLessEqual(self.app.log_panel.winfo_height(), log_height)

        form._toggle_advanced()
        self.app.update()
        self.assertEqual(form.winfo_height(), form_height)
        self.assertEqual(self.app.log_panel.winfo_height(), log_height)

        from ui.main_window import LoadingOverlay

        completed = []
        overlay = LoadingOverlay(
            self.app,
            lambda: completed.append(True),
        )
        overlay.place(x=0, y=36, relwidth=1, relheight=1)
        self.app.update()
        overlay._fade_out()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not completed:
            self.app.update()
            time.sleep(0.01)

        self.assertTrue(completed, "Loading overlay reveal should complete")
        self.assertEqual((self.app.winfo_width(), self.app.winfo_height()), root_size)
        self.assertEqual(form.winfo_height(), form_height)
        self.assertEqual(self.app.log_panel.winfo_height(), log_height)
        print("[Pass] Advanced content expands without a scrollbar and the original curtain reveal completes.")

    def test_8_developer_mode_lives_in_advanced_settings(self):
        """It went missing from the UI once; this pins it down.

        The checkbox used to be gridded onto the form below the advanced panel and
        only in Naver mode, so it read as a stray control and was unreachable for
        Keyescape, whose engine honours the same flag.
        """
        print("\n--- Verifying Developer Test Mode in Advanced Settings ---")
        form = self.app.form

        self.assertIs(
            form.dev_mode_checkbox.master, form.dev_mode_frame,
            "checkbox should sit in its own row frame",
        )
        self.assertIs(
            form.dev_mode_frame.master, form.advanced_frame,
            "the row must live inside the advanced panel, not on the form",
        )

        if not form._advanced_visible:
            form._toggle_advanced()
        self.app.update()
        self.assertTrue(
            form.dev_mode_checkbox.winfo_ismapped(),
            "checkbox must be visible while 고급 설정 is open",
        )

        def choose_mode(mode):
            # Mirror a real click: the segmented button carries the state and its
            # command runs afterwards.
            form.engine_mode_btn.set(mode)
            form._on_mode_change(mode)
            self.app.update()

        choose_mode(NAVER_MODE)
        self.assertFalse(
            hasattr(form, "npay_auto_pay_checkbox"),
            "Npay auto-payment must not have a separate checkbox",
        )
        self.assertEqual(form.dev_mode_checkbox.cget("state"), "normal")
        form.dev_mode_checkbox.select()
        self.assertTrue(form.developer_mode_enabled())
        form.dev_mode_checkbox.deselect()
        self.assertFalse(
            form.developer_mode_enabled(),
            "the visible unchecked state must always mean a real submission",
        )
        form.dev_mode_checkbox.select()

        # Switching to an engine that ignores devMode must clear it, or a stale
        # checkmark would suppress a real booking attempt.
        choose_mode(STANDARD_MODE)
        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        self.app.update()
        self.assertEqual(form.dev_mode_checkbox.cget("state"), "disabled")
        self.assertFalse(form.dev_mode_var.get(), "flag must not survive the switch")

        self.app.site_var.set("키이스케이프")
        self.app._on_site_change("키이스케이프")
        self.app.update()
        self.assertEqual(
            form.dev_mode_checkbox.cget("state"), "normal",
            "Keyescape drives a real browser, so it supports dev mode",
        )

        self.app.site_var.set("제로월드")
        self.app._on_site_change("제로월드")
        if form._advanced_visible:
            form._toggle_advanced()
        self.app.update()
        print("[Pass] Developer test mode sits inside 고급 설정, enables only for "
              "browser-driven engines, and clears itself elsewhere.")

if __name__ == "__main__":
    unittest.main()
