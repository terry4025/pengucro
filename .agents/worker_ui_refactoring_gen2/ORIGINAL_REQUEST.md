## 2026-06-19T09:46:32Z
You are UI Implementation Specialist Generation 2. A previous implementation introduced two critical runtime bugs in `ui/main_window.py`. Your task is to apply the following fixes:

1. **Fix `_on_engine_mode_change` method in `ui/main_window.py`**:
Restore the correct logic structure for filtering the sites dropdown based on the engine mode (Naver custom sites vs standard sites). Ensure that variables `target_site` and `site_options` are defined in all execution paths to prevent UnboundLocalError/NameError.
Use the following logic:
```python
    def _on_engine_mode_change(self, mode):
        # Log mode change if not redundant
        if getattr(self, "last_logged_mode", None) != mode:
            if hasattr(self, "log_panel") and self.log_panel:
                self.log_panel.append_log(f"예약 방식이 '{mode}'(으)로 변경되었습니다.", "info")
            self.last_logged_mode = mode

        self._suppress_site_log = True

        if mode == "네이버 (Playwright)":
            site_options = [k for k, v in self.custom_sites.items() if v.get("style") == "naver"]
            if not site_options:
                target_site = "(네이버 예약을 등록하세요)"
                site_options = [target_site]
            else:
                if getattr(self, "last_naver_site", None) in site_options:
                    target_site = self.last_naver_site
                else:
                    target_site = site_options[0]
            
            self.site_var.set(target_site)
            self._on_site_change(target_site)
            self.site_dropdown.configure(values=site_options)
            
            if not self.is_sync_running:
                self.is_sync_running = True
                import threading
                t = threading.Thread(target=self._sync_server_time, name="ServerTimeSyncThread")
                t.daemon = True
                t.start()
                self._update_server_time_clock()
        else:
            site_options = self.default_site_names + [k for k, v in self.custom_sites.items() if v.get("style") != "naver"]
            if getattr(self, "last_standard_site", None) in site_options:
                target_site = self.last_standard_site
            else:
                target_site = "제로월드"
                
            self.site_var.set(target_site)
            self._on_site_change(target_site)
            self.site_dropdown.configure(values=site_options)
            
            if target_site == "키이스케이프":
                if not self.is_sync_running:
                    self.is_sync_running = True
                    import threading
                    t = threading.Thread(target=self._sync_server_time, name="ServerTimeSyncThread")
                    t.daemon = True
                    t.start()
                    self._update_server_time_clock()
            else:
                self.server_time_label.configure(text="")
                self.is_sync_running = False

        self._suppress_site_log = False
```

2. **Fix `AddSiteDialog.parse_thread` exception handler in `ui/main_window.py`**:
Convert the exception object to a local string `err_msg` inside the `except` block before capture by the lambda, ensuring it does not raise NameError when evaluated after variable cleanup:
```python
                except Exception as e:
                    err_msg = str(e)
                    self.parent.after(0, lambda: self._on_parse_error(err_msg))
```

Verify the correctness of your fixes by running:
1. `python -m pyflakes ui/main_window.py` to verify no undefined names remain.
2. `python -m py_compile app.py ui/main_window.py` to confirm compile safety.
3. `python verify_ui.py` to verify empirical checks still pass.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Write your handoff report to `c:\Users\Administrator\Downloads\제로월드\.agents\worker_ui_refactoring_gen2\handoff.md`. Message back once done.
