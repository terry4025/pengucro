# Project: Sleek Minimalist Apple-Style Dark Mode UI Refactoring

## Architecture
This is a Room Escape Booking Macro program written in Python using CustomTkinter for the GUI interface.
The application consists of:
- `app.py`: Entry point of the application.
- `ui/theme.py`: UI Theme constants (colors, rounded corners, font definitions).
- `ui/main_window.py`: Core GUI Layout (title bar, navigation, status bar, engine logs).
- `ui/reservation_form.py`: Form fields input widget.
- `ui/log_panel.py`: Scrolling terminal log panel widget.
- `engines/`: Engine directory containing booking automation scripts for different escape room sites.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| 1 | Setup & Exploration | Perform read-only exploration and analysis of the codebase | None | DONE |
| 2 | Theme Refactoring | Refactor `ui/theme.py` to establish iOS-style Dark Palette and typography | None | DONE |
| 3 | Log Panel Refactoring | Refactor `ui/log_panel.py` to implement high-contrast terminal, scrollbar integration, and bold bracket tags | Milestone 2 | DONE |
| 4 | Reservation Form Refactoring | Refactor `ui/reservation_form.py` to implement uniform sizing, interactive entry focus glow | Milestone 2 | DONE |
| 5 | Main Window Refactoring | Refactor `ui/main_window.py` to reorganize traffic lights, alignment of headers, and stable time grids | Milestone 3, 4 | DONE |
| 6 | Verification & Validation | Run Python syntax/import compilation, check integrity mode, and perform E2E GUI testing | Milestone 5 | DONE |

## Interface Contracts
### `ui/theme.py` ↔ Widgets
- Widgets fetch visual configuration parameters (e.g. `fg_color=theme.SURFACE_COLOR`, `corner_radius=theme.ROUNDED_LG`) from `ui/theme.py` dynamically.

### `ui/main_window.py` ↔ engines
- Engine components post logs back via `self.log(...)`, which call `MainWindow._on_engine_log(...)` and routes to `LogPanel.append_log(...)`.
- Reservation forms supply configured fields (branch, date, time, name, etc.) via `self.form.get_reservation_data()` to trigger engines.
