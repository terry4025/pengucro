import base64
import json
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization

from engines.catchtable_crypto import CatchTableCrypto


@pytest.fixture
def rsa_keypair():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    public_key = private_key.public_key()
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, pub_pem


def test_crypto_normalize_pem():
    raw_b64 = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"
    normalized = CatchTableCrypto.normalize_pem(raw_b64)
    assert normalized.startswith("-----BEGIN PUBLIC KEY-----")
    assert normalized.endswith("-----END PUBLIC KEY-----")


def test_crypto_encryption_and_decryption(rsa_keypair):
    private_key, pub_pem = rsa_keypair

    encrypted_b64 = CatchTableCrypto.encrypt_slot_params(
        pub_pem,
        shop_ref="test_shop_123",
        search_date="2026-08-25",
        visit_time="19:00",
        person_count=4,
        table_type="_ALL_",
    )

    assert isinstance(encrypted_b64, str)
    assert len(encrypted_b64) > 0

    # Decrypt and verify payload
    encrypted_bytes = base64.b64decode(encrypted_b64)
    decrypted_bytes = private_key.decrypt(
        encrypted_bytes,
        padding.PKCS1v15(),
    )
    payload = json.loads(decrypted_bytes.decode("utf-8"))

    assert payload["shopRef"] == "test_shop_123"
    assert payload["searchDate"] == "2026-08-25"
    assert payload["visitTime"] == "19:00"
    assert payload["personCount"] == 4
    assert payload["tableType"] == "_ALL_"


def test_crypto_date_conversion(rsa_keypair):
    private_key, pub_pem = rsa_keypair

    # Passing YYMMDD string "260825"
    encrypted_b64 = CatchTableCrypto.encrypt_slot_params(
        pub_pem,
        shop_ref="test_shop_123",
        search_date="260825",
        person_count=2,
    )

    encrypted_bytes = base64.b64decode(encrypted_b64)
    decrypted_bytes = private_key.decrypt(
        encrypted_bytes,
        padding.PKCS1v15(),
    )
    payload = json.loads(decrypted_bytes.decode("utf-8"))
    assert payload["searchDate"] == "2026-08-25"
