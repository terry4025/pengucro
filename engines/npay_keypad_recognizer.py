from __future__ import annotations

import base64
import io
from typing import Any, Mapping

from PIL import Image

_CANVAS_SIZE = 20

_DIGIT_TEMPLATES_20X20: dict[str, str] = {
    "0": "0000000111111000000000000011111111000000000001111001111000000000111000001111000000001110000001110000000011100000011100000001110000000011000000011100000000111000000111000000001110000001110000000011100000011100000000111000000111000000001110000001110000000011000000001110000001110000000011100000011100000000111100001110000000000111111111100000000000111111110000000000000011110000000000000000000000000000",
    "1": "0000000000111000000000000000011110000000000000011111100000000000001111111000000000000011001110000000000000100011100000000000000000111000000000000000001110000000000000000011100000000000000000111000000000000000001110000000000000000011100000000000000000111000000000000000001110000000000000000011100000000000000000111000000000000000001110000000000000000011100000000000000000011000000000000000000000000000",
    "2": "0000001111111000000000000111111111000000000011110001111000000000111000001110000000011100000001100000000011000000011000000000000000001110000000000000000011100000000000000001110000000000000000111000000000000000011110000000000000001111000000000000000111100000000000000011110000000000000001111000000000000000111100000000000000001111111111110000000111111111111100000000111111111110000000000000000000000000",
    "3": "0000000111111000000000000111111111000000000011110001111000000000111000000111000000001100000001110000000000000000011100000000000000000110000000000000000111100000000000011111100000000000000111111100000000000000000111100000000000000000011100000000000000000111000000000000000000111000000111000000011100000000111000000111000000001111111111100000000001111111110000000000000111110000000000000000000000000000",
    "4": "0000000000111100000000000000011111000000000000000111110000000000000011111100000000000001110111000000000000011101110000000000001110011100000000000111000111000000000001110001110000000000111000011100000000001100000111000000000111000001110000000011111111111111000000111111111111111000001111111111111110000000000000011100000000000000000111000000000000000001110000000000000000001100000000000000000000000000",
    "5": "0000111111111110000000001111111111100000000011111111111000000000110000000000000000001100000000000000000011000000000000000000110001100000000000001101111111000000000111111111111000000001111000001110000000001100000001110000000000000000011100000000000000000111000000011000000001110000000111000000011100000001111000001110000000001111111111100000000001111111110000000000000111110000000000000000000000000000",
    "6": "0000000111111000000000000111111111000000000011111001111000000000111000000111000000011100000001110000000111000000000000000001110000100000000000011101111111000000000110111111111000000001111100001111000000011110000001110000000111000000011100000001110000000011000000011100000001110000000011000000011100000000111000001111000000000111111111100000000000111111110000000000000111110000000000000000000000000000",
    "7": "000111111111111100000001111111111111000000011111111111110000000000000000011000000000000000001110000000000000000111000000000000000001110000000000000000111000000000000000001110000000000000000111000000000000000011100000000000000001110000000000000000011100000000000000001110000000000000000011100000000000000001110000000000000000011100000000000000001110000000000000000011000000000000000000000000000000000",
    "8": "0000001111111000000000000111111111000000000011110001111000000000111000001111000000011100000001110000000011000000011100000000111000001110000000000111000111100000000000111111100000000000011111111100000000001111000111100000000111000000011100000001110000000111000000011100000001110000000111000000011100000001111000000111000000001111101111100000000001111111110000000000000111110000000000000000000000000000",
    "9": "0000001111110000000000000111111111000000000011110001111000000001110000001110000000011100000001110000000111000000011100000001110000000111000000011100000001110000000111000000111100000000111100011111000000001111111110110000000000111110011100000000000000000111000000000000000001110000000111000000111000000001111000001110000000001111111111000000000001111111100000000000000111110000000000000000000000000000",
}


class KeypadCell:
    """Represents a recognized keypad button cell with geometry and metadata."""

    def __init__(
        self,
        *,
        digit: str | None,
        row: int,
        col: int,
        bbox: tuple[int, int, int, int],
        center: tuple[int, int],
        confidence: float,
        is_action: bool = False,
        action_name: str = "",
    ) -> None:
        self.digit = digit
        self.row = row
        self.col = col
        self.bbox = bbox  # (x1, y1, x2, y2)
        self.center = center  # (cx, cy)
        self.confidence = confidence
        self.is_action = is_action
        self.action_name = action_name

    @property
    def label(self) -> str:
        return self.digit if self.digit is not None else self.action_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "digit": self.digit,
            "row": self.row,
            "col": self.col,
            "bbox": list(self.bbox),
            "center": list(self.center),
            "confidence": round(self.confidence, 4),
            "is_action": self.is_action,
            "action_name": self.action_name,
            "label": self.label,
        }

    def __repr__(self) -> str:
        return f"<KeypadCell '{self.label}' row={self.row} col={self.col} conf={self.confidence:.2f}>"


class NpayKeypadRecognizer:
    """Ultra-fast, deterministic visual recognizer for Naver Pay secure keypads."""

    KEYPAD_ROWS = 4
    KEYPAD_COLS = 3

    @classmethod
    def match_glyph_bitmap(cls, glyph_img: Image.Image) -> tuple[str | None, float]:
        """Matches a cropped glyph image using IoU against 20x20 centered templates."""
        gw, gh = glyph_img.size
        if gw < 2 or gh < 5:
            return None, 0.0

        # Heuristic: digit '1' is distinctly narrow (aspect ratio < 0.5)
        if (gw / float(gh)) < 0.50:
            return "1", 1.0

        # Pad and center into 20x20
        padded = Image.new("1", (_CANVAS_SIZE, _CANVAS_SIZE), 0)
        px = max(0, (_CANVAS_SIZE - gw) // 2)
        py = max(0, (_CANVAS_SIZE - gh) // 2)

        for y in range(min(gh, _CANVAS_SIZE)):
            for x in range(min(gw, _CANVAS_SIZE)):
                p = glyph_img.getpixel((x, y))
                val = sum(p[:3]) if isinstance(p, (tuple, list)) else p
                if val > 400 or (isinstance(p, int) and p > 128):
                    if px + x < _CANVAS_SIZE and py + y < _CANVAS_SIZE:
                        padded.putpixel((px + x, py + y), 1)

        sample_chars = [
            "1" if padded.getpixel((x, y)) else "0"
            for y in range(_CANVAS_SIZE)
            for x in range(_CANVAS_SIZE)
        ]
        sample_str = "".join(sample_chars)

        best_digit = None
        best_iou = -1.0

        for digit, template_str in _DIGIT_TEMPLATES_20X20.items():
            intersection = sum(
                1
                for a, b in zip(sample_str, template_str)
                if a == "1" and b == "1"
            )
            union = sum(
                1
                for a, b in zip(sample_str, template_str)
                if a == "1" or b == "1"
            )
            iou = intersection / float(max(1, union))
            if iou > best_iou:
                best_iou = iou
                best_digit = digit

        return best_digit, best_iou

    @classmethod
    def recognize_keypad_image(cls, image: Image.Image) -> dict[str, KeypadCell]:
        """Parses a full screenshot or cropped keypad image into a mapped dictionary of KeypadCells."""
        rgb = image.convert("RGB")
        keypad_crop, offset_x, offset_y = cls._find_keypad_region(rgb)

        kw, kh = keypad_crop.size
        cell_w = kw / float(cls.KEYPAD_COLS)
        cell_h = kh / float(cls.KEYPAD_ROWS)

        result: dict[str, KeypadCell] = {}

        for r in range(cls.KEYPAD_ROWS):
            for c in range(cls.KEYPAD_COLS):
                abs_x1 = int(offset_x + c * cell_w)
                abs_y1 = int(offset_y + r * cell_h)
                abs_x2 = int(offset_x + (c + 1) * cell_w)
                abs_y2 = int(offset_y + (r + 1) * cell_h)
                cx = (abs_x1 + abs_x2) // 2
                cy = (abs_y1 + abs_y2) // 2

                if r == 3 and c == 0:
                    result["전체삭제"] = KeypadCell(
                        digit=None,
                        row=r + 1,
                        col=c + 1,
                        bbox=(abs_x1, abs_y1, abs_x2, abs_y2),
                        center=(cx, cy),
                        confidence=1.0,
                        is_action=True,
                        action_name="전체삭제",
                    )
                    continue
                if r == 3 and c == 2:
                    result["지우기"] = KeypadCell(
                        digit=None,
                        row=r + 1,
                        col=c + 1,
                        bbox=(abs_x1, abs_y1, abs_x2, abs_y2),
                        center=(cx, cy),
                        confidence=1.0,
                        is_action=True,
                        action_name="지우기",
                    )
                    continue

                box = (
                    int(c * cell_w),
                    int(r * cell_h),
                    int((c + 1) * cell_w),
                    int((r + 1) * cell_h),
                )
                cell_crop = keypad_crop.crop(box)
                cw, ch = cell_crop.size

                white_pixels = [
                    (x, y)
                    for y in range(ch)
                    for x in range(cw)
                    if sum(cell_crop.getpixel((x, y))) > 450
                ]

                if not white_pixels:
                    continue

                min_x = min(x for x, y in white_pixels)
                max_x = max(x for x, y in white_pixels)
                min_y = min(y for x, y in white_pixels)
                max_y = max(y for x, y in white_pixels)

                glyph = cell_crop.crop((min_x, min_y, max_x + 1, max_y + 1))
                digit, conf = cls.match_glyph_bitmap(glyph)

                if digit is not None:
                    result[digit] = KeypadCell(
                        digit=digit,
                        row=r + 1,
                        col=c + 1,
                        bbox=(abs_x1, abs_y1, abs_x2, abs_y2),
                        center=(cx, cy),
                        confidence=conf,
                    )

        return result

    @classmethod
    def _find_keypad_region(cls, img: Image.Image) -> tuple[Image.Image, int, int]:
        """Detects the bounding box of the green keypad in a screenshot or returns the whole image."""
        w, h = img.size
        green_rows = []
        for y in range(0, h, 2):
            row_greens = sum(
                1
                for x in range(0, w, 2)
                if img.getpixel((x, y))[1] > 110 and img.getpixel((x, y))[0] < 65
            )
            if row_greens > (w // 8):
                green_rows.append(y)

        if not green_rows or (max(green_rows) - min(green_rows)) < 80:
            return img, 0, 0

        min_y = min(green_rows)
        max_y = max(green_rows)

        green_cols = []
        for x in range(0, w, 2):
            col_greens = sum(
                1
                for y in range(min_y, max_y + 1, 4)
                if img.getpixel((x, y))[1] > 110 and img.getpixel((x, y))[0] < 65
            )
            if col_greens > ((max_y - min_y) // 8):
                green_cols.append(x)

        if not green_cols:
            return img, 0, 0

        min_x = min(green_cols)
        max_x = max(green_cols)

        return img.crop((min_x, min_y, max_x + 1, max_y + 1)), min_x, min_y
