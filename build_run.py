"""Build driver.

The spec file name contains Hangul, and passing it through cmd.exe mangles it
under the console code page ("Spec file not found"). Invoking PyInstaller's API
from Python keeps the path as proper Unicode.
"""
import sys
from pathlib import Path

import PyInstaller.__main__

SPEC = Path(__file__).with_name("방탈출펭크로.spec")

if not SPEC.exists():
    print(f"spec not found: {SPEC}")
    sys.exit(1)

print(f"building from: {SPEC}")
PyInstaller.__main__.run(["--noconfirm", "--clean", str(SPEC)])
