from __future__ import annotations

import io
from pathlib import Path
from PIL import Image

from engines.npay_keypad_recognizer import KeypadCell, NpayKeypadRecognizer


def test_npay_keypad_recognition_on_sample_image() -> None:
    sample_path = Path(r"C:\Users\Administrator\.gemini\antigravity\brain\6f859b83-e5c9-4d39-9883-805d23fcb21b\.user_uploaded\media_1787031981896.png")
    if not sample_path.exists():
        return

    img = Image.open(sample_path)
    cells = NpayKeypadRecognizer.recognize_keypad_image(img)

    # Must find all 10 digits (0-9)
    for digit in "0123456789":
        assert digit in cells, f"Digit {digit} not recognized"
        cell = cells[digit]
        assert isinstance(cell, KeypadCell)
        assert cell.digit == digit
        assert 1 <= cell.row <= 4
        assert 1 <= cell.col <= 3
        assert cell.confidence >= 0.70

    # Check action keys
    assert "전체삭제" in cells
    assert "지우기" in cells
    assert cells["전체삭제"].row == 4 and cells["전체삭제"].col == 1
    assert cells["지우기"].row == 4 and cells["지우기"].col == 3

    # Check specific sample layout from user screenshot
    assert cells["4"].row == 1 and cells["4"].col == 1
    assert cells["8"].row == 1 and cells["8"].col == 2
    assert cells["0"].row == 1 and cells["0"].col == 3
    assert cells["6"].row == 2 and cells["6"].col == 1
    assert cells["5"].row == 2 and cells["5"].col == 2
    assert cells["9"].row == 2 and cells["9"].col == 3
    assert cells["2"].row == 3 and cells["2"].col == 1
    assert cells["7"].row == 3 and cells["2"].col == 1
    assert cells["1"].row == 3 and cells["1"].col == 3
    assert cells["3"].row == 4 and cells["3"].col == 2
