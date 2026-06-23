import customtkinter as ctk
import ui.theme as theme
from data.themes import (
    ZEROWORLD_THEMES,
    JIGUBYEOL_THEMES, PHOBIADUNGEON_THEMES, SITES_CONFIG, JIGUBYEOL_THEME_ALIASES,
    KEYESCAPE_THEMES
)
from datetime import datetime, timedelta
import calendar

class ReservationForm(ctk.CTkFrame):
    def __init__(self, parent, start_callback, stop_callback, mode_callback=None):
        super().__init__(
            parent,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG
        )
        self.start_callback = start_callback
        self.stop_callback = stop_callback
        self.mode_callback = mode_callback
        self.current_site = "제로월드"
        self.custom_sites = {}
        self.config = SITES_CONFIG[self.current_site]
        
        # Thread memory states
        self.standard_threads = 30
        self.naver_threads = 5
        self.last_mode = "고속 (Async)"

        # Grid configuration for 2 columns
        self.columnconfigure((0, 1), weight=1, uniform="equal")

        # -------------------------------------------------------------
        # Row 0: Branch Selection / Day Type Selection (Dynamic)
        # -------------------------------------------------------------
        self.branch_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.branch_label = ctk.CTkLabel(self.branch_frame, text="지점", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.branch_label.pack(anchor="w", pady=(0, 1))
        
        self.branch_var = ctk.StringVar()
        self.branch_dropdown = ctk.CTkOptionMenu(
            self.branch_frame,
            variable=self.branch_var,
            command=self._on_branch_change,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_MD,
            dropdown_font=theme.FONT_BODY_MD,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            anchor="w"
        )
        self.branch_dropdown.pack(fill="x")

        self.day_type_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.day_type_label = ctk.CTkLabel(self.day_type_frame, text="요일 구분", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.day_type_label.pack(anchor="w", pady=(0, 1))
        self.day_type_var = ctk.StringVar(value="평일")
        self.day_type_segmented = ctk.CTkSegmentedButton(
            self.day_type_frame,
            values=["평일", "주말"],
            variable=self.day_type_var,
            command=self._on_day_type_change,
            fg_color=theme.ELEVATED_COLOR,
            selected_color=theme.CARD_COLOR,
            unselected_color=theme.ELEVATED_COLOR,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.day_type_segmented.pack(fill="x")

        # -------------------------------------------------------------
        # Row 1: Theme Selection (Full Width OptionMenu)
        # -------------------------------------------------------------
        self.theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        
        self.theme_label = ctk.CTkLabel(self.theme_frame, text="테마 선택", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.theme_label.pack(anchor="w", pady=(0, 1))
        
        self.theme_var = ctk.StringVar()
        self.theme_dropdown = ctk.CTkOptionMenu(
            self.theme_frame,
            variable=self.theme_var,
            fg_color=theme.ELEVATED_COLOR,
            button_color=theme.ELEVATED_COLOR,
            button_hover_color=theme.CARD_COLOR,
            dropdown_fg_color=theme.SURFACE_COLOR,
            dropdown_text_color=theme.TEXT_PRIMARY,
            dropdown_hover_color=theme.CARD_COLOR,
            text_color=theme.TEXT_PRIMARY,
            font=theme.FONT_BODY_MD,
            dropdown_font=theme.FONT_BODY_MD,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            anchor="w"
        )
        self.theme_dropdown.pack(fill="x")

        # -------------------------------------------------------------
        # Row 2: Custom Theme Entry (Full Width)
        # -------------------------------------------------------------
        self.custom_theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        
        # Container to align checkboxes horizontally
        self.checkbox_container = ctk.CTkFrame(self.custom_theme_frame, fg_color="transparent")
        self.checkbox_container.pack(fill="x", anchor="w", pady=(0, 1))

        self.custom_theme_checkbox = ctk.CTkCheckBox(
            self.checkbox_container,
            text="테마 PK 직접 입력",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            fg_color=theme.ELEVATED_COLOR,
            checkmark_color=theme.ACCENT_GREEN,
            border_color=theme.HAIRLINE_COLOR,
            command=self._toggle_custom_theme
        )
        self.custom_theme_checkbox.pack(side="left", padx=(0, 15))

        self.show_server_time_checkbox = ctk.CTkCheckBox(
            self.checkbox_container,
            text="서버 시간 표시",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            checkbox_width=14,
            checkbox_height=14,
            corner_radius=theme.ROUNDED_SM,
            fg_color=theme.ELEVATED_COLOR,
            checkmark_color=theme.ACCENT_GREEN,
            border_color=theme.HAIRLINE_COLOR,
            command=self._toggle_server_time
        )
        self.show_server_time_checkbox.pack(side="left")
        
        self.theme_pk_entry = ctk.CTkEntry(
            self.custom_theme_frame,
            placeholder_text="테마 PK 코드 (예: 27)",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=26
        )
        self.theme_pk_entry.pack(fill="x")
        self.theme_pk_entry.pack_forget()

        # -------------------------------------------------------------
        # Row 3: Date & Time (Split row)
        # -------------------------------------------------------------
        self.date_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.date_frame.grid(row=3, column=0, padx=(12, 4), pady=4, sticky="ew")
        self.date_label = ctk.CTkLabel(self.date_frame, text="날짜 (YYYY-MM-DD)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.date_label.pack(anchor="w", pady=(0, 1))
        
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.date_entry = ctk.CTkEntry(
            self.date_frame,
            placeholder_text="예: 2026-06-01",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.date_entry.insert(0, tomorrow)
        self.date_entry.pack(fill="x")
        self.date_entry.bind("<KeyRelease>", self._format_date)
        self.date_entry.bind("<FocusOut>", self._on_date_change)

        self.time_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.time_frame.grid(row=3, column=1, padx=(4, 12), pady=4, sticky="ew")
        self.time_label = ctk.CTkLabel(self.time_frame, text="시간 (HH:MM)", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.time_label.pack(anchor="w", pady=(0, 1))
        
        self.time_entry = ctk.CTkEntry(
            self.time_frame,
            placeholder_text="예: 14:00",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.time_entry.pack(fill="x")
        self.time_entry.bind("<KeyRelease>", self._format_time)

        # -------------------------------------------------------------
        # Row 4: Name & People (Split row)
        # -------------------------------------------------------------
        self.name_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.name_frame.grid(row=4, column=0, padx=(12, 4), pady=4, sticky="ew")
        self.name_label = ctk.CTkLabel(self.name_frame, text="이름", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.name_label.pack(anchor="w", pady=(0, 1))
        
        self.name_entry = ctk.CTkEntry(
            self.name_frame,
            placeholder_text="예약자명",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.name_entry.pack(fill="x")

        self.people_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.people_frame.grid(row=4, column=1, padx=(4, 12), pady=4, sticky="ew")
        self.people_label = ctk.CTkLabel(self.people_frame, text="인원 수", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.people_label.pack(anchor="w", pady=(0, 1))
        
        self.people_entry = ctk.CTkEntry(
            self.people_frame,
            placeholder_text="2",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.people_entry.insert(0, "2")
        self.people_entry.pack(fill="x")

        # -------------------------------------------------------------
        # Row 5: Phone Number (Full Width)
        # -------------------------------------------------------------
        self.phone_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.phone_frame.grid(row=5, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        self.phone_label = ctk.CTkLabel(self.phone_frame, text="전화번호", font=theme.FONT_BODY_SM, text_color=theme.TEXT_MUTE)
        self.phone_label.pack(anchor="w", pady=(0, 1))
        
        self.phone_entry = ctk.CTkEntry(
            self.phone_frame,
            placeholder_text="예: 010-1234-5678",
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            text_color=theme.TEXT_PRIMARY,
            placeholder_text_color=theme.TEXT_DISABLED,
            corner_radius=theme.ROUNDED_MD,
            height=28
        )
        self.phone_entry.pack(fill="x")
        self.phone_entry.bind("<KeyRelease>", self._format_phone)

        # -------------------------------------------------------------
        # Row 6: Threads (Full Width)
        # -------------------------------------------------------------
        self.threads_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.threads_frame.grid(row=6, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")

        # Labels container (to pack title and value side-by-side)
        self.threads_label_frame = ctk.CTkFrame(self.threads_frame, fg_color="transparent")
        self.threads_label_frame.pack(side="left")

        self.threads_title_label = ctk.CTkLabel(
            self.threads_label_frame,
            text="동시 스레드 수",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.threads_title_label.pack(side="left")

        # Dynamic value badge
        self.threads_badge = ctk.CTkFrame(
            self.threads_label_frame,
            fg_color=theme.ELEVATED_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_SM
        )
        self.threads_badge.pack(side="left", padx=(8, 0))

        self.threads_value_label = ctk.CTkLabel(
            self.threads_badge,
            text="30",
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.ACCENT_BLUE,
            height=16
        )
        self.threads_value_label.pack(padx=6, pady=1)

        self.threads_slider = ctk.CTkSlider(
            self.threads_frame,
            from_=1,
            to=100,
            number_of_steps=99,
            fg_color=theme.ELEVATED_COLOR,
            progress_color=theme.ACCENT_BLUE,
            button_color=theme.ACCENT_WHITE,
            button_hover_color=theme.TEXT_BODY,
            command=self._on_threads_slider_move,
            height=8,
            corner_radius=4,
            button_length=14,
            button_corner_radius=7
        )
        self.threads_slider.set(30)
        self.threads_slider.pack(side="right", fill="x", expand=True, padx=(12, 0))

        # -------------------------------------------------------------
        # Row 7: Booking Method (Sync/Async)
        # -------------------------------------------------------------
        self.engine_mode_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.engine_mode_frame.grid(row=7, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")

        self.engine_mode_label = ctk.CTkLabel(
            self.engine_mode_frame,
            text="예약 방식",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE
        )
        self.engine_mode_label.pack(side="left", anchor="w")

        self.engine_mode_btn = ctk.CTkSegmentedButton(
            self.engine_mode_frame,
            values=["고속 (Async)", "네이버 (Playwright)"],
            font=theme.FONT_BODY_SM,
            fg_color=theme.ELEVATED_COLOR,
            selected_color=theme.ACCENT_BLUE,
            selected_hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_MD,
            height=28,
            command=self._on_mode_change
        )
        self.engine_mode_btn.set("고속 (Async)")
        self.engine_mode_btn.pack(side="right", fill="x", expand=False)

        # -------------------------------------------------------------
        # Row 8: Developer Test Mode (Naver only)
        # -------------------------------------------------------------
        self.dev_mode_frame = ctk.CTkFrame(self, fg_color="transparent")
        # Frame placement is handled by _update_widgets_state dynamically
        
        self.dev_mode_var = ctk.BooleanVar(value=False)
        self.dev_mode_checkbox = ctk.CTkCheckBox(
            self.dev_mode_frame,
            text="개발자 테스트 모드 (화면 표시 & 최종 예약 안함)",
            variable=self.dev_mode_var,
            font=theme.FONT_BODY_SM,
            fg_color=theme.ACCENT_BLUE,
            hover_color=theme.ACCENT_BLUE,
            text_color=theme.TEXT_PRIMARY,
            corner_radius=theme.ROUNDED_SM
        )
        self.dev_mode_checkbox.pack(side="left", anchor="w")

        # Setup focus effects for entries
        self._setup_entry_focus(self.theme_pk_entry)
        self._setup_entry_focus(self.date_entry)
        self._setup_entry_focus(self.time_entry)
        self._setup_entry_focus(self.name_entry)
        self._setup_entry_focus(self.people_entry)
        self._setup_entry_focus(self.phone_entry)

        # Initialize layout
        self.set_site(self.current_site)
        self._update_widgets_state()

    def _setup_entry_focus(self, entry):
        # Configure thin Apple hairline border
        entry.configure(border_width=1, font=theme.FONT_BODY_MD)
        entry.bind("<FocusIn>", lambda e: entry.configure(border_color=theme.ACCENT_BLUE) if entry.cget("state") == "normal" else None, add="+")
        entry.bind("<FocusOut>", lambda e: entry.configure(border_color=theme.HAIRLINE_COLOR), add="+")

    def _on_mode_change(self, mode):
        # Save previous state
        if self.last_mode == "네이버 (Playwright)":
            self.naver_threads = int(self.threads_slider.get())
        else:
            self.standard_threads = int(self.threads_slider.get())

        if mode == "네이버 (Playwright)":
            # Restrict threads slider to maximum of 8 and load cached value
            self.threads_slider.configure(to=8, number_of_steps=7)
            self.threads_slider.set(self.naver_threads)
            self.threads_value_label.configure(text=str(self.naver_threads))
        else:
            self.set_site(self.current_site)
            # Restore threads slider back to 100 and load cached value
            self.threads_slider.configure(to=100, number_of_steps=99)
            self.threads_slider.set(self.standard_threads)
            self.threads_value_label.configure(text=str(self.standard_threads))
            
        self._update_widgets_state()
            
        self.last_mode = mode
        if self.mode_callback:
            self.mode_callback(mode)

    def _update_widgets_state(self):
        is_naver = (self.engine_mode_btn.get() == "네이버 (Playwright)")
        
        self.threads_frame.grid(row=6, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        if self.current_site == "키이스케이프":
            self.threads_slider.configure(state="disabled")
            self.threads_slider.set(1)
            self.threads_value_label.configure(text="1", text_color=theme.TEXT_DISABLED)
            self.threads_title_label.configure(text_color=theme.TEXT_DISABLED)
        else:
            self.threads_slider.configure(state="normal")
            self.threads_title_label.configure(text_color=theme.TEXT_MUTE)
            self.threads_value_label.configure(text_color=theme.ACCENT_BLUE)
            if is_naver:
                self.threads_slider.set(self.naver_threads)
                self.threads_value_label.configure(text=str(self.naver_threads))
            else:
                self.threads_slider.set(self.standard_threads)
                self.threads_value_label.configure(text=str(self.standard_threads))

        if is_naver:
            # Disable Naver-incompatible controls but keep them in layout to prevent vertical layout shifting
            self.branch_dropdown.configure(state="disabled")
            self.branch_label.configure(text_color=theme.TEXT_DISABLED)
            
            self.day_type_segmented.configure(state="disabled")
            self.day_type_label.configure(text_color=theme.TEXT_DISABLED)
            
            themes_dict = self.config.get("themes", {}).get("1", {})
            has_themes = len(themes_dict) > 0 and not (len(themes_dict) == 1 and list(themes_dict.keys())[0] == "기본테마")
            if has_themes:
                self.theme_dropdown.configure(state="normal")
                self.theme_label.configure(text_color=theme.TEXT_MUTE)
            else:
                self.theme_dropdown.configure(state="disabled")
                self.theme_label.configure(text_color=theme.TEXT_DISABLED)
            
            self.custom_theme_checkbox.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.theme_pk_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            
            # Disable phone entry as Naver Booking autofills phone from active logged in user session
            self.phone_entry.configure(state="disabled", text_color=theme.TEXT_DISABLED)
            self.phone_label.configure(text_color=theme.TEXT_DISABLED)
            
            # Show frame and enable Developer Mode checkbox
            self.dev_mode_frame.grid(row=8, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")
            self.dev_mode_checkbox.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.engine_mode_frame.grid(row=7, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
        else:
            # Enable standard controls
            self.branch_dropdown.configure(state="normal")
            self.branch_label.configure(text_color=theme.TEXT_MUTE)
            
            self.day_type_segmented.configure(state="normal")
            self.day_type_label.configure(text_color=theme.TEXT_MUTE)
            
            self.theme_label.configure(text_color=theme.TEXT_MUTE)
            self.custom_theme_checkbox.configure(state="normal", text_color=theme.TEXT_MUTE)
            self.theme_pk_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            
            self.phone_entry.configure(state="normal", text_color=theme.TEXT_PRIMARY)
            self.phone_label.configure(text_color=theme.TEXT_MUTE)
            
            # Hide and uncheck Developer Mode checkbox
            self.dev_mode_frame.grid_forget()
            self.dev_mode_var.set(False)
            self._toggle_custom_theme()
            self.engine_mode_frame.grid(row=7, column=0, columnspan=2, padx=12, pady=(4, 10), sticky="ew")

    def set_site(self, site_name):
        self.current_site = site_name
        if site_name in self.custom_sites:
            self.config = self.custom_sites[site_name]
            # Custom sites do not use differentiated weekdays/weekends configurations in SITES_CONFIG
            has_weekday_weekend = False
        elif site_name in SITES_CONFIG:
            self.config = SITES_CONFIG[site_name]
            has_weekday_weekend = self.config["has_weekday_weekend"]
        else:
            # Fallback for dummy values (e.g. "(네이버 예약을 등록하세요)")
            self.config = {
                "branches": {},
                "themes": {},
                "has_weekday_weekend": False
            }
            has_weekday_weekend = False

        self.branch_frame.grid_forget()
        self.day_type_frame.grid_forget()

        # In Naver mode, hide all standard-engine-only form sections entirely, but show theme selection if there are multiple themes
        is_naver = (self.engine_mode_btn.get() == "네이버 (Playwright)")
        if is_naver:
            self.custom_theme_frame.grid_forget()
            themes_dict = self.config.get("themes", {}).get("1", {})
            has_themes = len(themes_dict) > 0 and not (len(themes_dict) == 1 and list(themes_dict.keys())[0] == "기본테마")
            if has_themes:
                self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
            else:
                self.theme_frame.grid_forget()
        else:
            # Keep theme and custom theme frames always mapped in grid to prevent vertical jumping
            self.theme_frame.grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="ew")
            self.custom_theme_frame.grid(row=2, column=0, columnspan=2, padx=12, pady=4, sticky="ew")

            if has_weekday_weekend:
                # Show both branch and day type selection side by side
                self.branch_frame.grid(row=0, column=0, padx=(12, 4), pady=(10, 4), sticky="ew")
                self.day_type_frame.grid(row=0, column=1, padx=(4, 12), pady=(10, 4), sticky="ew")
                branch_options = list(self.config["branches"].keys())
                self.branch_dropdown.configure(values=branch_options)
                if branch_options:
                    prev_val = self.branch_var.get()
                    if prev_val in branch_options:
                        self.branch_var.set(prev_val)
                    else:
                        self.branch_var.set(branch_options[0])
            else:
                self.branch_frame.grid(row=0, column=0, columnspan=2, padx=12, pady=(10, 4), sticky="ew")
                branch_options = list(self.config["branches"].keys())
                self.branch_dropdown.configure(values=branch_options)
                if branch_options:
                    prev_val = self.branch_var.get()
                    if prev_val in branch_options:
                        self.branch_var.set(prev_val)
                    else:
                        self.branch_var.set(branch_options[0])

        self._update_theme_options()
        self._update_widgets_state()

    def _on_branch_change(self, value):
        self._update_theme_options()

    def _on_day_type_change(self, value):
        self._update_theme_options()

    def _toggle_custom_theme(self):
        # Only run toggle behavior if not in Naver mode to prevent overriding disabled state
        if self.engine_mode_btn.get() == "네이버 (Playwright)":
            return
        if self.custom_theme_checkbox.get() == 1:
            self.theme_dropdown.configure(state="disabled")
            self.theme_pk_entry.pack(fill="x", after=self.checkbox_container, pady=(2, 0))
        else:
            self.theme_dropdown.configure(state="normal")
            self.theme_pk_entry.pack_forget()

    def _toggle_server_time(self):
        # Call MainWindow update function if master has it
        if hasattr(self.master, "_update_server_time_sync_state"):
            self.master._update_server_time_sync_state()

    def _on_threads_slider_move(self, value):
        if self.current_site == "키이스케이프":
            return
        val = int(value)
        self.threads_value_label.configure(text=str(val))
        if self.engine_mode_btn.get() == "네이버 (Playwright)":
            self.naver_threads = val
        else:
            self.standard_threads = val

    def _on_date_change(self, event=None):
        """Auto-detect weekday/weekend from the entered date."""
        if not self.current_site.startswith("제로월드"):
            return
        date_str = self.date_entry.get().strip()
        if len(date_str) != 10:
            return
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_of_week = dt.weekday()  # 0=Mon ... 6=Sun
            if day_of_week >= 5:  # Saturday or Sunday
                self.day_type_var.set("주말")
            else:
                self.day_type_var.set("평일")
            self._update_theme_options()
        except ValueError:
            pass

    def _update_theme_options(self):
        if self.current_site in self.custom_sites:
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = self.config["themes"].get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "제로월드":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = ZEROWORLD_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "비트포비아 던전":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "3")
            themes_dict = PHOBIADUNGEON_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        elif self.current_site == "키이스케이프":
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "14")
            themes_dict = KEYESCAPE_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))
        else:
            branch_name = self.branch_var.get()
            branch_id = self.config["branches"].get(branch_name, "1")
            themes_dict = JIGUBYEOL_THEMES.get(branch_id, {})
            theme_names = sorted(list(themes_dict.keys()))

        self.theme_dropdown.configure(values=theme_names)
        if theme_names:
            self.theme_var.set(theme_names[0])
        else:
            self.theme_var.set("")

    def get_reservation_data(self):
        is_naver = (self.engine_mode_btn.get() == "네이버 (Playwright)")
        
        # Resolve Branch ID
        if is_naver:
            branch_id = "1"
        else:
            branch_name = self.branch_var.get()
            if not branch_name and self.config["branches"]:
                branch_name = list(self.config["branches"].keys())[0]
            branch_id = self.config["branches"].get(branch_name, "1")

        # Resolve Theme PK
        if is_naver:
            # For Naver Playwright mode, the themePK argument holds the actual normalized booking URL
            theme_name = self.theme_var.get()
            theme_pk = self.config.get("themes", {}).get("1", {}).get(theme_name, "")
            if not theme_pk or theme_pk == "naver":
                theme_pk = self.config.get("url", "naver")
        elif self.custom_theme_checkbox.get() == 1:
            theme_pk = self.theme_pk_entry.get().strip()
        else:
            theme_name = self.theme_var.get()
            if self.current_site in self.custom_sites:
                theme_pk = self.config["themes"].get(branch_id, {}).get(theme_name, "")
            elif self.current_site == "제로월드":
                themes_dict = ZEROWORLD_THEMES.get(branch_id, {})
                theme_pk = themes_dict.get(theme_name, "")
            elif self.current_site == "비트포비아 던전":
                theme_pk = theme_name
            elif self.current_site == "키이스케이프":
                themes_dict = KEYESCAPE_THEMES.get(branch_id, {})
                theme_info = themes_dict.get(theme_name, {})
                theme_pk = theme_info.get("info_num", "")
            else:
                themes_dict = JIGUBYEOL_THEMES.get(branch_id, {})
                theme_pk = themes_dict.get(theme_name, "")

        date = self.date_entry.get().strip()
        time_str = self.time_entry.get().strip()
        if len(time_str) == 5:
            time_str += ":00"

        name = self.name_entry.get().strip()
        phone = self.phone_entry.get().strip()
        people = self.people_entry.get().strip()
        threads = int(self.threads_slider.get())
        if self.current_site == "키이스케이프":
            threads = 1
        is_naver = (self.engine_mode_btn.get() == "네이버 (Playwright)")

        # Validation
        if not theme_pk:
            return None, "테마를 선택하거나 테마 PK를 입력해주세요.", 0, False
        if not date:
            return None, "예약 날짜를 입력해주세요.", 0, False
        if not time_str:
            return None, "예약 시간을 입력해주세요.", 0, False
        if not name:
            return None, "예약자 이름을 입력해주세요.", 0, False
        if not is_naver and not phone:
            return None, "전화번호를 입력해주세요.", 0, False

        site_url = ""
        if self.current_site == "제로월드":
            branch_name = self.branch_var.get()
            site_url = self.config["urls"].get(branch_name, self.config["url"])
        elif self.current_site in SITES_CONFIG:
            site_url = self.config.get("url", "")

        res_data = {
            'branch': branch_id,
            'reservationDate': date,
            'name': name,
            'phone': phone,
            'people': people,
            'themePK': theme_pk,
            'reservationTime': time_str,
            'paymentType': '1',
            'policy': 'true',
            'devMode': self.dev_mode_var.get(),
            'site_url': site_url
        }

        is_async = (self.engine_mode_btn.get() == "고속 (Async)")
        return res_data, None, threads, is_async

    def _format_phone(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.phone_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if digits.startswith("02"):
            if len(digits) <= 2:
                formatted = digits
            elif len(digits) <= 5:
                formatted = f"{digits[:2]}-{digits[2:]}"
            elif len(digits) <= 9:
                formatted = f"{digits[:2]}-{digits[2:5]}-{digits[5:]}"
            else:
                formatted = f"{digits[:2]}-{digits[2:6]}-{digits[6:]}"
        else:
            if len(digits) <= 3:
                formatted = digits
            elif len(digits) <= 6:
                formatted = f"{digits[:3]}-{digits[3:]}"
            elif len(digits) <= 10:
                formatted = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
            else:
                formatted = f"{digits[:3]}-{digits[3:7]}-{digits[7:11]}"
                
        current_cursor = self.phone_entry.index("insert")
        hyphens_before = text[:current_cursor].count("-")
        
        self.phone_entry.delete(0, "end")
        self.phone_entry.insert(0, formatted)
        
        hyphens_after = formatted.count("-")
        new_cursor = current_cursor + (hyphens_after - hyphens_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.phone_entry.icursor(new_cursor)

    def _format_date(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.date_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if len(digits) <= 4:
            formatted = digits
        elif len(digits) <= 6:
            formatted = f"{digits[:4]}-{digits[4:]}"
        else:
            formatted = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
            
        current_cursor = self.date_entry.index("insert")
        hyphens_before = text[:current_cursor].count("-")
        
        self.date_entry.delete(0, "end")
        self.date_entry.insert(0, formatted)
        
        hyphens_after = formatted.count("-")
        new_cursor = current_cursor + (hyphens_after - hyphens_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.date_entry.icursor(new_cursor)
        
        self._on_date_change(event)

    def _format_time(self, event=None):
        if event and event.keysym in ("BackSpace", "Delete", "Left", "Right", "Up", "Down"):
            return
            
        text = self.time_entry.get()
        digits = "".join(c for c in text if c.isdigit())
        
        formatted = ""
        if len(digits) <= 2:
            formatted = digits
        else:
            formatted = f"{digits[:2]}:{digits[2:4]}"
            
        current_cursor = self.time_entry.index("insert")
        colons_before = text[:current_cursor].count(":")
        
        self.time_entry.delete(0, "end")
        self.time_entry.insert(0, formatted)
        
        colons_after = formatted.count(":")
        new_cursor = current_cursor + (colons_after - colons_before)
        new_cursor = max(0, min(new_cursor, len(formatted)))
        self.time_entry.icursor(new_cursor)

    def load_config(self):
        import os
        import json
        config_path = "config.json"
        if not os.path.exists(config_path):
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                
            saved_site = config.get("site", "제로월드")
            if saved_site in ["제로월드 강남", "제로월드 홍대"]:
                saved_site = "제로월드"
                config["branch"] = "강남점" if "강남" in config.get("site", "") else "홍대점"
            if saved_site == self.current_site:
                if "branch" in config:
                    self.branch_var.set(config["branch"])
                if "day_type" in config:
                    self.day_type_var.set(config["day_type"])
                    
                self._update_theme_options()
                
                if "theme" in config:
                    theme_val = config["theme"]
                    if not self.current_site.startswith("제로월드"):
                        theme_val = JIGUBYEOL_THEME_ALIASES.get(theme_val, theme_val)
                    if theme_val in self.theme_dropdown.cget("values"):
                        self.theme_var.set(theme_val)
                    
                if "custom_theme" in config:
                    if config["custom_theme"]:
                        self.custom_theme_checkbox.select()
                        self.theme_dropdown.configure(state="disabled")
                        self.theme_pk_entry.pack(fill="x", after=self.custom_theme_checkbox, pady=(2, 0))
                    else:
                        self.custom_theme_checkbox.deselect()
                        self.theme_dropdown.configure(state="normal")
                        self.theme_pk_entry.pack_forget()
                        
                if "theme_pk" in config:
                    self.theme_pk_entry.delete(0, "end")
                    self.theme_pk_entry.insert(0, config["theme_pk"])
            else:
                self._update_theme_options()
                
            if "date" in config:
                self.date_entry.delete(0, "end")
                self.date_entry.insert(0, config["date"])
                
            if "time" in config:
                self.time_entry.delete(0, "end")
                self.time_entry.insert(0, config["time"])
                
            if "name" in config:
                self.name_entry.delete(0, "end")
                self.name_entry.insert(0, config["name"])
                
            if "phone" in config:
                self.phone_entry.delete(0, "end")
                self.phone_entry.insert(0, config["phone"])
                
            if "people" in config:
                self.people_entry.delete(0, "end")
                self.people_entry.insert(0, config["people"])
                
            # Parse memory threads first
            if "threads" in config:
                self.standard_threads = config["threads"]
            if "naver_threads" in config:
                self.naver_threads = config["naver_threads"]

            if "engine_mode" in config:
                mode_val = config["engine_mode"]
                if mode_val == "일반 (Sync)":
                    mode_val = "고속 (Async)"
                self.engine_mode_btn.set(mode_val)
                self._on_mode_change(mode_val)
            elif "is_async" in config:
                val = "고속 (Async)"
                self.engine_mode_btn.set(val)
                self._on_mode_change(val)

            if "show_server_time" in config:
                if config["show_server_time"]:
                    self.show_server_time_checkbox.select()
                else:
                    self.show_server_time_checkbox.deselect()
            else:
                self.show_server_time_checkbox.deselect()
        except Exception:
            pass

    def save_config(self, site_name):
        import json
        config_path = "config.json"
        try:
            # Sync active memory thread counts before saving
            if self.engine_mode_btn.get() == "네이버 (Playwright)":
                self.naver_threads = int(self.threads_slider.get())
            else:
                self.standard_threads = int(self.threads_slider.get())

            config = {
                "site": site_name,
                "branch": self.branch_var.get(),
                "day_type": self.day_type_var.get(),
                "theme": self.theme_var.get(),
                "custom_theme": bool(self.custom_theme_checkbox.get()),
                "theme_pk": self.theme_pk_entry.get().strip(),
                "date": self.date_entry.get().strip(),
                "time": self.time_entry.get().strip(),
                "name": self.name_entry.get().strip(),
                "phone": self.phone_entry.get().strip(),
                "people": self.people_entry.get().strip(),
                "threads": self.standard_threads,
                "naver_threads": self.naver_threads,
                "is_async": (self.engine_mode_btn.get() == "고속 (Async)"),
                "engine_mode": self.engine_mode_btn.get(),
                "show_server_time": bool(self.show_server_time_checkbox.get())
            }
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
