# -*- coding: utf-8 -*-
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from PIL import Image

from engines.cgv_engine import CgvEngine
from engines.npay_keypad_recognizer import NpayKeypadRecognizer


def test_cgv_engine_enter_naver_pay_password_success():
    logs = []
    engine = CgvEngine(lambda msg, lvl: logs.append((msg, lvl)))

    sample_media = Path(r"C:\Users\Administrator\.gemini\antigravity\brain\6f859b83-e5c9-4d39-9883-805d23fcb21b\.user_uploaded\media_1787031981896.png")

    class FakeButton:
        def __init__(self):
            self.clicked = 0

        def is_visible(self, timeout=0):
            return True

        def click(self):
            self.clicked += 1

    class FakeKeypadLocator:
        def __init__(self):
            self.btn = FakeButton()

        def is_visible(self, timeout=0):
            return True

        def screenshot(self):
            if sample_media.exists():
                return sample_media.read_bytes()
            img = Image.new("RGB", (300, 400), (3, 168, 78))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        def locator(self, _selector):
            return SimpleNamespace(first=self.btn)

        def click(self, position=None):
            self.btn.clicked += 1

    keypad = FakeKeypadLocator()

    class FakePage:
        def locator(self, _sel):
            return SimpleNamespace(first=keypad)

        def wait_for_timeout(self, _ms):
            pass

    page = FakePage()
    res = engine._enter_naver_pay_password(page, "480659")
    assert res is True
    assert keypad.btn.clicked == 6
    assert any("입력 완료" in msg for msg, lvl in logs if lvl == "success")


def test_cgv_engine_enter_naver_pay_password_missing_digit():
    logs = []
    engine = CgvEngine(lambda msg, lvl: logs.append((msg, lvl)))

    class FakeKeypadLocator:
        def is_visible(self, timeout=0):
            return True

        def screenshot(self):
            img = Image.new("RGB", (100, 100), (0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    keypad = FakeKeypadLocator()

    class FakePage:
        def locator(self, _sel):
            return SimpleNamespace(first=keypad)

    page = FakePage()
    res = engine._enter_naver_pay_password(page, "123456")
    assert res is False
    assert any("수동 입력" in msg for msg, lvl in logs if lvl == "warning")
