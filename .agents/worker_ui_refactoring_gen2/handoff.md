# Handoff Report — UI Refactoring Gen 2

## 1. Observation

During our run, we observed the following issues:
* **Undefined Variables in `_on_engine_mode_change`**:
  `pyflakes` reported undefined name warnings:
  ```
  ui/main_window.py:1157:31: undefined name 'target_site'
  ui/main_window.py:1158:34: undefined name 'target_site'
  ui/main_window.py:1159:49: undefined name 'site_options'
  ```
  These variables were accessed in the `if` block's execution path but were only defined inside the `else` branch.
* **Deleted Exception Reference in `AddSiteDialog.parse_thread`**:
  `pyflakes` reported:
  ```
  ui/main_window.py:569:75: undefined name 'e'
  ```
  This is due to Python 3 deleting exception target names (e.g. `e` from `except Exception as e`) at the end of the `except` block to prevent reference cycles. The scheduled lambda reference `lambda: self._on_parse_error(str(e))` failed at evaluation time.
* **Title Bar Layout Test Failure in `verify_ui.py`**:
  Running `C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe verify_ui.py` failed initially on `test_1_mainwindow_title_bar` with:
  ```
  AssertionError: 'right' != 'left' : dots_frame must be packed on the left
  ```
  The layout in the repository had the traffic light container packed on the right (and in the order of minimize -> maximize -> close) and the pin button packed on the left, violating macOS visual conventions and causing test failure.

---

## 2. Logic Chain

1. **Conditional Dropdown Filtering**: By restructuring `_on_engine_mode_change` to initialize `site_options` and `target_site` within both execution paths, we completely prevent UnboundLocalError/NameError.
2. **Exception Scope Binding**: Binding `str(e)` to a local variable `err_msg` inside the `except` block before scheduling the callback allows the scheduled lambda closure to access the error string successfully after the `except` block exits.
3. **Attribute Lookup Resolution**: Since the prompt's updated `_on_engine_mode_change` references `self._sync_server_time`, we renamed `_sync_naver_server_time` to `_sync_server_time` and updated it to dynamically resolve targets for Naver and Keyescape, eliminating the `AttributeError`.
4. **Title Bar Alignment**: Re-aligning the `dots_frame` container to pack on the left, placing the buttons in the order close (red) -> minimize (yellow) -> maximize (green) with 12x12 dimensions, and placing the Pin button on the right resolves the layout mismatch.
5. **Empirical Verification**: Run tests (`verify_ui.py`) and static checks to verify all changes pass.

---

## 3. Caveats

* Under the `CODE_ONLY` network isolation mode constraint, we could not perform live external HTTP/HEAD network requests during testing, so time synchronization utilizes offline try-except fallback logic.

---

## 4. Conclusion

The UI runtime bugs (UnboundLocalError/NameError in mode switching and lambda-captured exception scoping) have been resolved. The macOS traffic light and pin button layouts are correctly aligned to satisfy project test specifications. All static compilation and empirical checks pass successfully.

---

## 5. Verification Method

To verify the changes:
1. Run Pyflakes on the modified file:
   ```powershell
   C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe -m pyflakes ui/main_window.py
   ```
   No undefined name warnings should be output.
2. Run compilation safety checks:
   ```powershell
   C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe -m py_compile app.py ui/main_window.py
   ```
   Command must exit with 0.
3. Run the empirical verification test suite:
   ```powershell
   C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe verify_ui.py
   ```
   All 5 tests (including the newly added unit tests for dropdown filtering and parse error exception capture) should output `OK`.
