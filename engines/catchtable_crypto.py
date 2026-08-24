from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)


class CatchTableCrypto:
    """Handles CatchTable RSA encryption for time slot queries."""

    @staticmethod
    def normalize_pem(key_str: str) -> str:
        """Wrap raw base64 public key in PEM header/footer if needed."""
        cleaned = key_str.strip()
        if not cleaned.startswith("-----BEGIN"):
            return f"-----BEGIN PUBLIC KEY-----\n{cleaned}\n-----END PUBLIC KEY-----"
        return cleaned

    @classmethod
    def load_public_key(cls, key_str: str):
        """Load RSA public key from base64 string or PEM string."""
        pem_str = cls.normalize_pem(key_str)
        return serialization.load_pem_public_key(pem_str.encode("utf-8"))

    @classmethod
    def encrypt_slot_params(
        cls,
        public_key_str: str,
        *,
        shop_ref: str,
        search_date: str,
        visit_time: str = "19:00",
        person_count: int = 2,
        table_type: str = "_ALL_",
    ) -> str:
        """Encrypt slot query parameters using RSA PKCS#1 v1.5 padding.

        CatchTable client formats:
        - searchDate: "YYYY-MM-DD"
        - visitTime: "19:00"
        - tableType: "_ALL_" (or specific like "H", "R", etc.)
        """
        # Format date as YYYY-MM-DD if given as YYMMDD
        formatted_date = search_date
        if len(search_date) == 6 and search_date.isdigit():
            formatted_date = f"20{search_date[:2]}-{search_date[2:4]}-{search_date[4:]}"

        payload: dict[str, Any] = {
            "shopRef": shop_ref,
            "searchDate": formatted_date,
            "visitTime": visit_time,
            "personCount": int(person_count),
            "tableType": table_type or "_ALL_",
        }

        json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        public_key = cls.load_public_key(public_key_str)

        encrypted_bytes = public_key.encrypt(
            json_bytes,
            padding.PKCS1v15(),
        )
        return base64.b64encode(encrypted_bytes).decode("utf-8")
