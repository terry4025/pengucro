# UI Refactoring Analysis & Recommendations

## 1. Executive Summary
This report analyzes the current UI implementation of the Room Escape Booking Macro program across four files: `ui/theme.py`, `ui/main_window.py`, `ui/reservation_form.py`, and `ui/log_panel.py`. The objective is to design a sleek, minimalist, Apple-style dark mode UI (design system, compact layout, high-contrast typography, interactive input styling, high-contrast terminal log, and bracket-based log category highlighting) while preserving critical business logics—specifically, Google reCAPTCHA v2 warnings and 미오픈 날짜 정각 예약 제출 (9999 bypass slot ID) rules.

---

## 2. Current UI Styling Settings & Observations

Based on our read-only analysis of the current UI source files, here are the identified styling properties:

| Category | Component / Setting | Current Value / Implementation | Observations / Issues |
| :--- | :--- | :--- | :--- |
| **Colors** | Canvas (Window Background) | `#000000` (Pure Black) | Pure black is high contrast, but lacks depth when compared to Apple's dark UI. |
| | Surface (Container Card) | `#15161e` (Dark Blue-Gray) | Deep tint mismatching Apple's neutral dark gray palette. |
| | Elevated (Inputs & OptionMenu) | `#0a0b0e` (Very Dark Blue-Gray) | Too dark, making border boundaries less visible. |
| | Card (Hover & Selection) | `#1f222f` (Medium Blue-Gray) | Tends to look purple/blue rather than clean grayscale. |
| | Hairline Border | `#242735` | Low contrast against the surfaces. |
| | Accent Blue | `#57c1ff` | Muted blue, not matching the vibrant Apple system blue. |
| | Accent Red / Green / Yellow | `#ff5f56` / `#27c93f` / `#ffbd2e` | Hardcoded in `main_window.py` for titlebar traffic lights. |
| **Rounded Corners** | Scale | `SM=6`, `MD=8`, `LG=12`, `XL=16` | Corners are defined but inconsistently enforced in entry focus/border states. |
| **Fonts** | Typographic Weights | All body/mono fonts are configured as `bold`. | Heavy bolding on body, log, and entries causes text clipping and overlapping. |
| **Window Layout** | Window Dimensions | `480x860` (Fixed, borderless via `overrideredirect(True)`) | Vertical space is highly constrained, making compact layout imperative. |
| | macOS Traffic Lights | Placed on the **Right** side of the title bar. | Violates macOS design conventions (should be on the **Left**). |
| | Traffic Light Order | Left-to-right: Yellow, Green, Red. | Incorrect ordering (should be: Red, Yellow, Green). |
| | Title bar Buttons | No hover states/solid border settings. | Can look raw without `border_width=0`. |
| | Server Time Label | Dynamically packed/unpacked (`pack()` / `pack_forget()`). | Causes annoying vertical shifting (jumping) of all widgets. |
| **Form Inputs** | Text Entries | Font is not configured (defaults to Tkinter default). | Mismatched font family/sizes; lacks interactive border highlight on focus. |
| | Dropdown Menus | `corner_radius=theme.ROUNDED_MD` (8px). | Good, but could match the newly proposed Apple MD scale. |
| **Log Panel** | Textbox Background | `theme.CARD_COLOR` (`#1f222f`). | Too light, resulting in poor terminal contrast. |
| | Scrollbars | CustomTkinter default scrollbar styling. | Thumb color doesn't match the hairline border colors. |
| | Log Bracket Highlights | None. Raw string output. | Hard to parse categories like `[YesCaptcha]` or `[경고]` at a glance. |

---

## 3. Recommended Refactoring Plan

### R1: Sleek Minimalist Apple-style Dark Mode Design System (`ui/theme.py`)
Replace the current color palette with genuine Apple Dark Mode system colors (iOS/macOS System Materials) and refine the typography weights to prevent vertical crowding.

#### Proposed Code for `ui/theme.py`:
```python
# Sleek Minimalist Apple-style Dark Mode Design System

# Color Palette (OLED Black Canvas with iOS-style elevated surfaces)
CANVAS_COLOR = "#0A0A0C"       # Deep Space Black window background
SURFACE_COLOR = "#1C1C1E"      # Apple System Dark Gray (Form card container background)
ELEVATED_COLOR = "#2C2C2E"     # Apple Secondary Dark Gray (Input fields & OptionMenu background)
CARD_COLOR = "#3A3A3C"         # Apple Tertiary Dark Gray (Hover, focus, and selection background)
HAIRLINE_COLOR = "#38383A"     # Apple System Separator border (very thin, subtle contrast)

# Text Colors (iOS typography scale)
TEXT_PRIMARY = "#FFFFFF"       # Pure white for main text and headers
TEXT_BODY = "#E5E5EA"          # Apple primary label (off-white for reading comfort)
TEXT_MUTE = "#8E8E93"          # Apple secondary label (muted gray for secondary labels)
TEXT_DARK = "#000000"          # Black text for high-contrast white CTA buttons
TEXT_DISABLED = "#48484A"      # Apple placeholder text and disabled state

# Accent / Semantic Colors (Apple SF palette)
ACCENT_WHITE = "#FFFFFF"       # Primary white button
ACCENT_GREEN = "#30D158"       # Apple Success green (vibrant)
ACCENT_GREEN_HOVER = "#24B047" # Darker green for hover
ACCENT_RED = "#FF453A"         # Apple Destructive red (vibrant)
ACCENT_RED_HOVER = "#E03B30"   # Darker red for hover
ACCENT_YELLOW = "#FFD60A"      # Apple Alert/Warning yellow
ACCENT_YELLOW_HOVER = "#E0BC08" # Darker yellow for hover
ACCENT_BLUE = "#0A84FF"        # Apple Info blue / link color
ACCENT_BLUE_HOVER = "#0070E0"  # Darker blue for hover

# Rounded Corners Scale (Apple rounded corner hierarchy)
ROUNDED_SM = 6                 # Small controls, checkboxes
ROUNDED_MD = 10                # Buttons, input fields, dropdown buttons (classic Apple curvature)
ROUNDED_LG = 16                # Main cards, log panels, modal dialogs
ROUNDED_XL = 20                # Outer window containers (if applicable)

# Fonts (Sleek hierarchy: regular weights for text to avoid overlapping, bold for headers)
FONT_FAMILY = "Segoe UI"
FONT_DISPLAY = (FONT_FAMILY, 20, "bold") # Slightly smaller to prevent text clipping
FONT_HEADING = (FONT_FAMILY, 13, "bold")
FONT_BODY_MD = (FONT_FAMILY, 12, "normal") # Normal weight to prevent overcrowding and overlap
FONT_BODY_SM = (FONT_FAMILY, 11, "bold")   # Keep bold for small label headers
FONT_MONO = ("Consolas", 10, "normal")     # Normal weight & slightly smaller for dense logs
```

---

### R2: Main Window Layout Optimization (`ui/main_window.py`)
1. **Move macOS Traffic Light Buttons to the Left**: Position the traffic lights container on the left of the title bar and sort them as Close (Red) -> Minimize (Yellow) -> Maximize (Green).
2. **Move Pin Button to the Right**: Position the Pin (`📌`) button on the right side of the title bar to keep the traffic lights clean.
3. **Elminate Dynamic Shifting (Server Time Label)**: Keep the server time label gridded next to a compact brand logo and status badge in a horizontal row. Set its text to `""` when inactive to maintain stable layout height.

#### Refactoring Code Snippets:

*Title Bar Arrangement (in `MainWindow.__init__` around lines 654-736):*
```python
        # macOS Traffic Light Buttons Container on the Left
        dots_frame = ctk.CTkFrame(self.title_bar, fg_color="transparent")
        dots_frame.pack(side="left", padx=12, pady=8)

        # Close Button (Red) - Packed first
        self.close_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=12,
            height=12,
            corner_radius=6,
            fg_color=theme.ACCENT_RED,
            hover_color=theme.ACCENT_RED_HOVER,
            border_width=0,
            command=self._on_close
        )
        self.close_btn.pack(side="left", padx=4)

        # Minimize Button (Yellow) - Packed second
        self.min_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=12,
            height=12,
            corner_radius=6,
            fg_color=theme.ACCENT_YELLOW,
            hover_color=theme.ACCENT_YELLOW_HOVER,
            border_width=0,
            command=self._on_minimize
        )
        self.min_btn.pack(side="left", padx=4)

        # Maximize Button (Green) - Packed third
        self.max_btn = ctk.CTkButton(
            dots_frame,
            text="",
            width=12,
            height=12,
            corner_radius=6,
            fg_color=theme.ACCENT_GREEN,
            hover_color=theme.ACCENT_GREEN_HOVER,
            border_width=0,
            command=self._on_maximize
        )
        self.max_btn.pack(side="left", padx=4)

        # Pin (Always on Top) Button on the Right
        self.pin_btn = ctk.CTkButton(
            self.title_bar,
            text="📌",
            width=26,
            height=22,
            corner_radius=6,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            font=(theme.FONT_FAMILY, 11),
            text_color=theme.TEXT_MUTE,
            command=self._toggle_pin
        )
        self.pin_btn.pack(side="right", padx=(0, 10))
```

*Header Frame Grid Configuration (around lines 740-775):*
```python
        # Main Header Block (Compact Horizontal Layout)
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(10, 4))
        header_frame.columnconfigure(0, weight=1)
        header_frame.columnconfigure(1, weight=1)

        # Brand Logo and Status Pill (Left-aligned)
        left_container = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_container.grid(row=0, column=0, sticky="w")

        self.brand_label = ctk.CTkLabel(
            left_container,
            text="PENGRO",
            font=(theme.FONT_FAMILY, 13, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.brand_label.pack(side="left", padx=(0, 8))

        self.status_badge = ctk.CTkLabel(
            left_container,
            text="● 대기 중",
            font=(theme.FONT_FAMILY, 10, "bold"),
            text_color=theme.TEXT_PRIMARY,
            fg_color=theme.ELEVATED_COLOR,
            corner_radius=10,
            padx=10,
            pady=2,
            height=20
        )
        self.status_badge.pack(side="left")

        # Server Time Label (Right-aligned, permanently gridded)
        self.server_time_label = ctk.CTkLabel(
            header_frame,
            text="",  # Blank when inactive, no vertical shifting!
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.ACCENT_YELLOW,
            fg_color="transparent",
            height=20
        )
        self.server_time_label.grid(row=0, column=1, sticky="e")
```

*Server Time Label Visibility Management:*
- In `_on_site_change` (lines 1063-1074) and `_on_engine_mode_change` (lines 1153-1184), replace references to `self.server_time_label.pack(...)` with nothing (since it's already gridded).
- Replace references to `self.server_time_label.pack_forget()` with `self.server_time_label.configure(text="")`.

*Ensure Loading Screen particle RGB matches theme.py (lines 196-200):*
If `ACCENT_BLUE` is `#0A84FF` (RGB: `(10, 132, 255)`), update lines 197-199:
```python
                r_val = int(p['alpha'] * 10)
                g_val = int(p['alpha'] * 132)
                b_val = int(p['alpha'] * 255)
```

---

### R3: Reservation Form Widget Styling (`ui/reservation_form.py`)
1. **Consistent Fonts**: Pass `font=theme.FONT_BODY_MD` (the new regular weight font) to all `CTkEntry` widgets to align their text size and family with OptionMenus.
2. **Interactive Focus Highlight on Entry Fields**: Add border focus animations so inputs glow blue when focused.
3. **Corner Radius Sync**: Ensure `corner_radius=theme.ROUNDED_MD` is set on OptionMenus, SegmentedButtons, and Entries, and `corner_radius=theme.ROUNDED_LG` on the outer form frame.

#### Refactoring Plan:

*Entry Field Binding Helper in `ReservationForm.__init__`:*
```python
    def _setup_entry_focus(self, entry):
        # Configure thin Apple hairline border
        entry.configure(border_width=1, font=theme.FONT_BODY_MD)
        entry.bind("<FocusIn>", lambda e: entry.configure(border_color=theme.ACCENT_BLUE))
        entry.bind("<FocusOut>", lambda e: entry.configure(border_color=theme.HAIRLINE_COLOR))
```

*Call this helper for every Entry:*
```python
        # Applying style to inputs
        self._setup_entry_focus(self.theme_pk_entry)
        self._setup_entry_focus(self.date_entry)
        self._setup_entry_focus(self.time_entry)
        self._setup_entry_focus(self.name_entry)
        self._setup_entry_focus(self.people_entry)
        self._setup_entry_focus(self.phone_entry)
```

*Standardize Spacing:*
- Change segmented buttons and dropdown corner radiuses to `theme.ROUNDED_MD` (10px).
- Set consistent row margins: Use `pady=4` for typical rows, `pady=(10, 4)` for row 0, and `pady=(4, 10)` for row 7.

---

### R4: Log Panel Refinement (`ui/log_panel.py`)
1. **Terminal Background**: Set the textbox `fg_color` to `#050505` (deep high-contrast black).
2. **Scrollbar Synchronization**: Configure CustomTkinter's scrollbar parameters in `CTkTextbox` directly:
   `scrollbar_button_color=theme.HAIRLINE_COLOR` and `scrollbar_button_hover_color=theme.CARD_COLOR`.
3. **Clean Category Brackets Highlighting**: Parse log lines that begin with `[` and end with `]`. Extract the bracket category and render it in a bold semantic color based on its category keywords (e.g. blue for `[YesCaptcha]`, yellow for `[경고]`, purple for `[1번 기기]`).

#### Proposed Code for `ui/log_panel.py`:
```python
import customtkinter as ctk
import ui.theme as theme

class LogPanel(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(
            parent,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG
        )

        # Header Row
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=15, pady=(10, 5))

        # Title
        self.header_label = ctk.CTkLabel(
            header_frame,
            text="● TERMINAL LOGS",
            font=(theme.FONT_FAMILY, 11, "bold"),
            text_color=theme.TEXT_PRIMARY
        )
        self.header_label.pack(side="left")

        # Clear Button
        self.clear_btn = ctk.CTkButton(
            header_frame,
            text="Clear",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            width=50,
            height=20,
            corner_radius=theme.ROUNDED_SM,
            command=self.clear_log
        )
        self.clear_btn.pack(side="right")

        # Copy Button
        self.copy_btn = ctk.CTkButton(
            header_frame,
            text="Copy",
            font=theme.FONT_BODY_SM,
            text_color=theme.TEXT_MUTE,
            fg_color="transparent",
            hover_color=theme.CARD_COLOR,
            width=50,
            height=20,
            corner_radius=theme.ROUNDED_SM,
            command=self.copy_log
        )
        self.copy_btn.pack(side="right", padx=(0, 4))

        # Scrollable Text Area with high contrast terminal BG and Apple-style scrollbar
        self.textbox = ctk.CTkTextbox(
            self,
            fg_color="#050505",       # Deeper pitch black for high contrast
            text_color=theme.TEXT_BODY,
            font=theme.FONT_MONO,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_MD,
            scrollbar_button_color=theme.HAIRLINE_COLOR,
            scrollbar_button_hover_color=theme.CARD_COLOR
        )
        self.textbox.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        # Configure tags for standard log content
        self.textbox.configure(state="normal")
        self.textbox._textbox.tag_config("info", foreground=theme.TEXT_PRIMARY)
        self.textbox._textbox.tag_config("success", foreground=theme.ACCENT_GREEN)
        self.textbox._textbox.tag_config("error", foreground=theme.ACCENT_RED)
        self.textbox._textbox.tag_config("warning", foreground=theme.ACCENT_YELLOW)
        
        # Configure tags for category brackets (bold Segoe UI fonts to pop out)
        self.textbox._textbox.tag_config("cat_default", foreground=theme.TEXT_MUTE, font=(theme.FONT_FAMILY, 10, "bold"))
        self.textbox._textbox.tag_config("cat_captcha", foreground=theme.ACCENT_BLUE, font=(theme.FONT_FAMILY, 10, "bold"))
        self.textbox._textbox.tag_config("cat_warning", foreground=theme.ACCENT_YELLOW, font=(theme.FONT_FAMILY, 10, "bold"))
        self.textbox._textbox.tag_config("cat_error", foreground=theme.ACCENT_RED, font=(theme.FONT_FAMILY, 10, "bold"))
        self.textbox._textbox.tag_config("cat_success", foreground=theme.ACCENT_GREEN, font=(theme.FONT_FAMILY, 10, "bold"))
        self.textbox._textbox.tag_config("cat_device", foreground="#BF5AF2", font=(theme.FONT_FAMILY, 10, "bold")) # iOS system purple
        
        self.textbox.configure(state="disabled")

    def append_log(self, message, log_type="info"):
        self.append_logs_batch([(message, log_type)])

    def append_logs_batch(self, logs_list):
        self.textbox.configure(state="normal")
        for message, log_type in logs_list:
            if message.startswith("[") and "]" in message:
                idx = message.find("]")
                bracket = message[:idx+1]
                body = message[idx+1:]
                
                # Determine tag based on category content
                tag = "cat_default"
                if "YesCaptcha" in bracket:
                    tag = "cat_captcha"
                elif "경고" in bracket:
                    tag = "cat_warning"
                elif "에러" in bracket or "실패" in bracket:
                    tag = "cat_error"
                elif "성공" in bracket or "완료" in bracket:
                    tag = "cat_success"
                elif "기기" in bracket:
                    tag = "cat_device"
                
                # Insert bracket with category highlight tag, body with standard message tag
                self.textbox.insert("end", bracket, tag)
                self.textbox.insert("end", body + "\n", log_type)
            else:
                self.textbox.insert("end", message + "\n", log_type)
            
        try:
            line_count = int(self.textbox._textbox.index('end-1c').split('.')[0])
            if line_count > 300:
                self.textbox.delete("1.0", f"{line_count - 300}.0")
        except Exception:
            pass
            
        self.textbox.see("end")
        self.textbox.configure(state="disabled")

    def clear_log(self):
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.configure(state="disabled")

    def copy_log(self):
        content = self.textbox.get("1.0", "end").strip()
        if content:
            self.winfo_toplevel().clipboard_clear()
            self.winfo_toplevel().clipboard_append(content)
            # Brief visual feedback
            self.copy_btn.configure(text="Copied!", text_color=theme.ACCENT_GREEN)
            self.after(1500, lambda: self.copy_btn.configure(text="Copy", text_color=theme.TEXT_MUTE))
```

---

## 4. Business Logic Preservation & Verification

The refactoring plan has been cross-checked with the project rules defined in `AGENTS.md` and the existing engines to ensure zero functional disruption:

### Rule 1: Google reCAPTCHA v2 Warnings (2-Minute Expiry)
* **Rule**: Log the warning string `[경고] 구글 캡차 인증은 완료 후 2분이 지나면 자동 초기화(만료)됩니다. 예약 오픈시간 1~2분 전에 완료하시는 것이 가장 안전합니다.` with Warning level.
* **Refactoring Impact & Preservation**: The engine (`engines/keyescape_engine.py`) issues this warning via `self.log("[경고] ...", "warning")`. 
  - Our revised `LogPanel.append_logs_batch` will capture this log line.
  - Because it starts with `[경고]`, our bracket parser will identify it and assign the `"cat_warning"` tag to `[경고]` (rendering it in **bold Apple Warning Yellow `#FFD60A`**).
  - The remaining message body will be rendered with the standard `warning` tag (also in **Apple Warning Yellow**).
  - The warning will be highly eye-catching and fully preserved without modification of the underlying logging API.

### Rule 2: 미오픈 날짜 정각 예약 제출 (9999 Bypass Mode)
* **Rule**: When slot is `9999`, do not submit immediately. Monitor backend API at ~0.15s intervals, and once the real slot ID opens, inject it dynamically via Javascript and submit.
* **Refactoring Impact & Preservation**: This bypass and injection logic resides purely inside the reservation engine (`engines/keyescape_engine.py`) and standard Playwright browser automation layers.
  - The UI only acts as the data provider by reading fields through `self.form.get_reservation_data()`.
  - Our refactoring plan strictly preserves all internal variable names (e.g. `self.theme_pk_entry`, `self.custom_theme_checkbox`, `self.day_type_var`), class names, and layout toggle methods (`_toggle_custom_theme()`).
  - Thus, the form will correctly compile the dictionary with parameters like `themePK = "9999"` when custom entry is enabled and bypass is triggered. No business logic boundaries will be crossed or altered.
