# Implementation Plan: Sleek Minimalist Apple-Style Dark Mode UI Refactoring

This plan outlines the steps required to execute the UI improvements requested by the user, adhering strictly to the Project Orchestrator guidelines and safety rules.

## Plan Steps

### Phase 1: Exploration and Analysis
- **Step 1.1**: Spawn an UI Explorer to inspect `ui/theme.py`, `ui/main_window.py`, `ui/reservation_form.py`, and `ui/log_panel.py`.
- **Step 1.2**: Receive the Explorer's analysis and identify specific changes needed for R1, R2, R3, R4.
- **Step 1.3**: Define the interface layout boundaries and spacing contracts.

### Phase 2: Design System & Theme Alignment (R1)
- **Step 2.1**: Update `ui/theme.py` to establish consistent rounded corner variables, refined Apple-style dark mode canvas/surface/accent colors, and readable high-contrast typography hierarchy.

### Phase 3: Main Window Layout Optimization (R2)
- **Step 3.1**: Adjust margins, paddings, and card container layouts in `ui/main_window.py`.
- **Step 3.2**: Refine custom titlebar, making sure 맥 스타일 신호등 (Apple-style traffic light) close/minimize/maximize buttons are aligned properly.
- **Step 3.3**: Elevate server time display and status badge readability.

### Phase 4: Reservation Form Styling (R3)
- **Step 4.1**: Refine input fields (OptionMenu, Entry) background, border width, hover, and focus states in `ui/reservation_form.py`.
- **Step 4.2**: Adjust font sizing and text layouts to prevent text overlaps.

### Phase 5: Terminal Log Panel Improvements (R4)
- **Step 5.1**: Stylize scrollbars, panel background contrast, and mono font settings in `ui/log_panel.py`.
- **Step 5.2**: Update tag configurations to draw clean, high-contrast text tags for different log levels (success, warning, info, error).

### Phase 6: Verification & Testing
- **Step 6.1**: Run validation tests using worker/reviewer/challenger/auditor cycles.
- **Step 6.2**: Verify that existing application logic, especially reCAPTCHA warning and 미오픈 날짜 정각 예약 제출 rules, are entirely preserved.
- **Step 6.3**: Verify successful build and execution.
