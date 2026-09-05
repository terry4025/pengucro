"""Atomic local reservation claims survive response loss and process restarts."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from pengucro.storage import get_data_dir


class OrderJournal:
    def __init__(self, data, root=None):
        identity = [str(data.get(key, "")) for key in
                    ("branch", "themePK", "reservationDate")]
        identity += [str(data.get("reservationTime", ""))[:5],
                     "".join(c for c in str(data.get("phone", "")) if c.isdigit())]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        self.path = Path(root or get_data_dir() / "dpsnnn-orders") / (digest + ".json")

    def read(self):
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("state"):
                return value
        except (OSError, ValueError):
            pass
        return {"state": "unknown"}

    def claim(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"state": "submitting", "pid": os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def update(self, state, order_code="", booking_number=""):
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"state": state, "pid": os.getpid(), "order_code": order_code,
                       "booking_number": booking_number}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def rejected(self):
        # Only an explicit server rejection permits another attempt.
        self.path.unlink(missing_ok=True)
