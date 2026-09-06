"""Offline verification of fixed, private ZeroWorld challenge corpora.

No network requests, refreshes, reservations or answer logging. A corpus has
private-challenges.json entries {"image": "001.jpg", "digest": "..."}.
The output includes only file fingerprints, pass/fail and warm-model timing.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines.zeroworld_captcha import recognize_digits, warm_ocr


async def verify(root: Path) -> dict:
    root = root.resolve()
    manifest = root / "private-challenges.json"
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    if not cases:
        raise ValueError("Empty corpus")
    await asyncio.to_thread(warm_ocr)
    results = []
    seen = set()
    for index, case in enumerate(cases, 1):
        path = (root / case["image"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Image must stay inside its corpus")
        image = path.read_bytes()
        fingerprint = hashlib.sha256(image).hexdigest()
        if fingerprint in seen:
            raise ValueError("Duplicate original image; do not count it twice")
        seen.add(fingerprint)
        started = time.perf_counter()
        answer = await recognize_digits(image, case["digest"])
        results.append({"case": index, "image_sha256": fingerprint, "passed": bool(answer),
                        "ms": round((time.perf_counter() - started) * 1000, 2)})
    times = sorted(row["ms"] for row in results)
    return {"total": len(results), "passed": sum(row["passed"] for row in results),
            "distinct_original_images": len(seen), "network_requests": 0, "refreshes": 0,
            "method": "bounded image OCR candidates validated against site challenge digest",
            "warm_model_ms": {"median": statistics.median(times),
                              "p95": times[max(0, (95 * len(times) + 99) // 100 - 1)],
                              "maximum": max(times)}, "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(verify(args.corpus))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if sys.stdout is not None:
        print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
