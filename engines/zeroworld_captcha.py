"""Bounded local OCR for ZeroWorld's observed four/five-digit challenge."""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import threading

from PIL import Image, ImageChops, ImageOps

_models = {}
_model_lock = threading.Lock()


def parse_digest(body: str) -> str:
    value = body.strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", value):
        return value.lower()
    # Observed server warning precedes the actual digest (2026-09-06).
    # Accept only that identified notice, not arbitrary error/login HTML.
    if "Undefined index: PHPSESSID" in value and "Notice" in value:
        match = re.search(r">\s*([a-fA-F0-9]{32})\s*$", value)
        if match:
            return match.group(1).lower()
    return ""


def _get_model(beta=False):
    if beta not in _models:
        import ddddocr
        model = ddddocr.DdddOcr(show_ad=False, use_gpu=False, beta=beta)
        model.set_ranges("0123456789")
        _models[beta] = model
    return _models[beta]


def warm_ocr():
    with _model_lock:
        _get_model()
        _get_model(True)


def _decode_candidates(output, charset, width=12, *, min_length=4, max_length=5):
    """Bounded numeric CTC beam decoding of image model scores, not guesses."""
    import numpy as np
    scores = output[:, 0, :] if output.shape[1] == 1 else output[0, :, :]
    allowed = [0] + [i for i, c in enumerate(charset) if c in "0123456789" and len(c) == 1]
    scores = scores[:, allowed].astype(float)
    scores -= scores.max(axis=1, keepdims=True)
    scores -= np.log(np.exp(scores).sum(axis=1, keepdims=True))
    beam = {"": (0.0, -math.inf)}
    for row in scores:
        next_beam = {}
        def add(prefix, blank=-math.inf, nonblank=-math.inf):
            a, b = next_beam.get(prefix, (-math.inf, -math.inf))
            next_beam[prefix] = (float(np.logaddexp(a, blank)), float(np.logaddexp(b, nonblank)))
        for prefix, (blank, nonblank) in beam.items():
            total = float(np.logaddexp(blank, nonblank))
            add(prefix, blank=total + row[0])
            for i, index in enumerate(allowed[1:], 1):
                char = charset[index]
                if prefix.endswith(char):
                    add(prefix, nonblank=nonblank + row[i])
                    if len(prefix) < max_length:
                        add(prefix + char, nonblank=blank + row[i])
                elif len(prefix) < max_length:
                    add(prefix + char, nonblank=total + row[i])
        beam = dict(sorted(next_beam.items(), key=lambda x: float(np.logaddexp(*x[1])), reverse=True)[:width])
    return [value for value in beam if min_length <= len(value) <= max_length]


def _recognize(image_bytes: bytes, beta=False, beam_width=12, *, expected_length=None):
    with _model_lock:
        engine = _get_model(beta).ocr_engine
        with Image.open(io.BytesIO(image_bytes)) as image:
            tensor = engine._preprocess_image(image, png_fix=False)
        # Pinned 1.6.1 preprocesses into [0, 1]; the bundled OCR models use
        # [-1, 1]. Decode their actual [sequence, 1, classes] score tensor
        # directly, restricting digit classes before (not after) decoding.
        import numpy as np
        output = engine.session.run(None, {
            engine.session.get_inputs()[0].name: tensor * 2.0 - 1.0,
        })[0]
        charset = engine.charset_manager.get_charset()
        if output.ndim == 3:
            if expected_length is not None:
                return _decode_candidates(output, charset, beam_width,
                                          min_length=expected_length, max_length=expected_length)
            return _decode_candidates(output, charset, beam_width)
        if output.ndim != 2:
            raise ValueError("OCR 모델 출력 구조 변경")
        indices = output[0]
        characters = []
        previous = 0
        for value in indices:
            index = int(value)
            if index != previous and index != 0 and 0 <= index < len(charset):
                characters.append(charset[index])
            previous = index
        return "".join(characters)


async def recognize_digits(image_bytes: bytes, expected_digest: str = "") -> str:
    """Read pixels, then validate an OCR candidate; never enumerate guesses."""
    if len(image_bytes) > 1024 * 1024:
        raise ValueError("인증 이미지 크기 초과")
    digest = expected_digest.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{32}", digest):
        raise ValueError("인증 응답 구조 변경")
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width > 1000 or source.height > 1000:
            raise ValueError("인증 이미지 크기 초과")
        rgb = source.convert("RGB")
    original = rgb
    # The site displays the 120x60 JPEG in a 120x50 image element. Match the
    # verified browser geometry when processing the HTTP image in the engine.
    if rgb.size == (120, 60):
        rgb = rgb.resize((120, 50), Image.Resampling.BICUBIC)
        buffer = io.BytesIO()
        rgb.save(buffer, "PNG")
        image_bytes = buffer.getvalue()
    background = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    mask = ImageChops.difference(rgb, background).convert("L").point(lambda p: 255 if p > 35 else 0)
    bbox = mask.getbbox()
    variants = [image_bytes]
    if bbox:
        crop = ImageOps.expand(rgb.crop(bbox), border=8, fill=rgb.getpixel((0, 0)))
        for variant in (crop, ImageOps.expand(ImageOps.invert(mask.crop(bbox)), border=8, fill=255)):
            buffer = io.BytesIO()
            variant.save(buffer, "PNG")
            variants.append(buffer.getvalue())
    passes = [(raw, beta, 12) for beta in (False, True) for raw in variants]
    # Overlapping glyphs can merge at the default aspect ratio. A bounded
    # wider-image fallback separates the model timesteps without refreshing
    # the challenge or expanding into exhaustive digit enumeration.
    if original.size == (120, 60):
        buffer = io.BytesIO()
        original.resize((120, 40), Image.Resampling.BICUBIC).save(buffer, "PNG")
        passes.extend((buffer.getvalue(), beta, 32) for beta in (False, True))
        # Rare overlapping/tilted glyphs: bounded image-only fallback passes.
        # Keep these after the inexpensive path; validate every candidate with
        # the challenge digest before it can be entered into a reservation.
        background = Image.new("RGB", original.size, original.getpixel((0, 0)))
        difference = ImageChops.difference(original, background).convert("L")
        binary = ImageOps.invert(difference.point(lambda p: 255 if p > 95 else 0))
        fallback = [
            (original, 35, False, 32),
            (original, 35, True, 32),
            (original.rotate(10, resample=Image.Resampling.BICUBIC,
                             fillcolor=original.getpixel((0, 0))), 35, True, 32),
            (binary, 35, True, 32),
            (binary, 40, True, 128),
            (original, 40, False, 128),
            (original, 40, True, 128),
        ]
        # Dense ink projections exclude long, thin noise lines from the crop.
        import numpy as np
        ink = np.asarray(difference) > 35
        columns, rows = ink.sum(axis=0), ink.sum(axis=1)
        for density, height in ((0.2, 35), (0.35, 30)):
            xs = np.flatnonzero(columns > columns.max() * density)
            ys = np.flatnonzero(rows > rows.max() * density)
            if len(xs) and len(ys):
                box = (max(0, int(xs[0]) - 2), max(0, int(ys[0]) - 2),
                       min(original.width, int(xs[-1]) + 3), min(original.height, int(ys[-1]) + 3))
                crop = ImageOps.expand(original.crop(box), border=5, fill=original.getpixel((0, 0)))
                fallback.append((crop, height, True, 128))
        # Narrow, heavily overlapping digits need more horizontal model steps.
        # Keep this behind existing passes so ordinary images stay on the fast path.
        fallback.extend((original, 30, beta, 128) for beta in (False, True))
        for variant, height, beta, width in fallback:
            buffer = io.BytesIO()
            variant.resize((120, height), Image.Resampling.BICUBIC).save(buffer, "PNG")
            passes.append((buffer.getvalue(), beta, width))
    for raw, beta, width in passes:
        candidates = await asyncio.to_thread(_recognize, raw, beta, width)
        for text in candidates if isinstance(candidates, list) else [candidates]:
            text = re.sub(r"\s+", "", text)
            if digest:
                text = text.translate(str.maketrans({"i": "1", "I": "1", "l": "1", "O": "0", "o": "0"}))
            if not re.fullmatch(r"[0-9]{4,5}", text):
                continue
            if not digest or hashlib.md5(text.encode("ascii")).hexdigest() == digest:
                return text
    return ""
