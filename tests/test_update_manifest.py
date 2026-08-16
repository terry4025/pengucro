from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pengucro.update_manifest import (
    ManifestValidationError,
    ManifestVerificationError,
    UpdateConfig,
    UpdateConfigurationError,
    canonical_signed_payload,
    parse_and_verify_manifest,
)


def _signed_manifest(**changes):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    values = {
        "schema_version": 1,
        "release_sequence": 602,
        "version": "6.02",
        "download_url": "https://github.com/terry4025/pengucro-updates/releases/download/v6.02/app.exe",
        "size": 123,
        "sha256": "a" * 64,
        "notes": ["자동 업데이트를 추가했습니다"],
    }
    values.update(changes)
    values["signature"] = base64.b64encode(private_key.sign(canonical_signed_payload(values))).decode("ascii")
    config = UpdateConfig(
        manifest_url="https://github.com/terry4025/pengucro-updates/releases/latest/download/latest.json",
        public_key=public_key,
    )
    return values, config


def test_signed_manifest_round_trip_uses_integer_sequence_not_display_version():
    values, config = _signed_manifest(version="6.010", release_sequence=610)

    manifest = parse_and_verify_manifest(json.dumps(values).encode("utf-8"), config)

    assert manifest.version == "6.010"
    assert manifest.release_sequence == 610
    assert manifest.notes == ("자동 업데이트를 추가했습니다",)
    assert manifest.is_newer_than(609)
    assert not manifest.is_newer_than(610)


def test_tampered_manifest_is_rejected():
    values, config = _signed_manifest()
    values["size"] += 1

    with pytest.raises(ManifestVerificationError):
        parse_and_verify_manifest(json.dumps(values).encode("utf-8"), config)


@pytest.mark.parametrize(
    "change",
    [
        {"release_sequence": True},
        {"release_sequence": 0},
        {"version": "v6.02"},
        {"size": 0},
        {"sha256": "A" * 64},
        {"notes": ["x" * 121]},
        {"notes": ["  공백"]},
        {"download_url": "http://github.com/file.exe"},
        {"download_url": "https://evil.example/file.exe"},
    ],
)
def test_invalid_signed_field_is_rejected_even_when_signature_is_valid(change):
    values, config = _signed_manifest(**change)

    with pytest.raises(ManifestValidationError):
        parse_and_verify_manifest(json.dumps(values).encode("utf-8"), config)


def test_manifest_shape_is_strict_and_rejects_duplicate_or_extra_fields():
    values, config = _signed_manifest()
    values["unexpected"] = "value"
    with pytest.raises(ManifestValidationError, match="허용되지 않음"):
        parse_and_verify_manifest(json.dumps(values).encode("utf-8"), config)

    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(ManifestValidationError, match="중복"):
        parse_and_verify_manifest(duplicate, config)


def test_configuration_fails_closed_without_or_with_invalid_public_key():
    with pytest.raises(UpdateConfigurationError, match="설정되지"):
        UpdateConfig.load(environ={}, embedded_public_key_b64="")
    with pytest.raises(UpdateConfigurationError, match="형식"):
        UpdateConfig.load(environ={"PENGUCRO_UPDATE_PUBLIC_KEY": "not-base64"}, embedded_public_key_b64="")


def test_environment_key_is_accepted_but_cannot_expand_host_allow_list():
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    encoded = base64.b64encode(public_key).decode("ascii")

    config = UpdateConfig.load(
        environ={"PENGUCRO_UPDATE_PUBLIC_KEY": encoded},
        embedded_public_key_b64="",
    )
    assert config.public_key == public_key

    with pytest.raises(UpdateConfigurationError):
        UpdateConfig.load(
            environ={"PENGUCRO_UPDATE_PUBLIC_KEY": encoded},
            embedded_public_key_b64="",
            manifest_url="https://evil.example/latest.json",
        )
