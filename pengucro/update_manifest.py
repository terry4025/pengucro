"""Authenticated update manifest parsing.

The updater deliberately treats configuration and manifests as untrusted input.
Only a small, versioned JSON shape is accepted and every accepted manifest must
be signed with the Ed25519 key embedded in the application (or injected during
development through the documented environment variable).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse


DEFAULT_MANIFEST_URL = (
    "https://github.com/terry4025/pengucro-updates/"
    "releases/latest/download/latest.json"
)

# Only this public verification key is shipped. The matching private signing
# key stays outside the repository and application on the release machine.
EMBEDDED_PUBLIC_KEY_B64 = "tlVkpyOTjV1epBcTO3H/NzZyhZNCNOWINeNwobO2drk="
PUBLIC_KEY_ENVIRONMENT_VARIABLE = "PENGUCRO_UPDATE_PUBLIC_KEY"

DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
_EXPECTED_FIELDS = frozenset(
    {
        "schema_version",
        "release_sequence",
        "version",
        "download_url",
        "size",
        "sha256",
        "notes",
        "signature",
    }
)
_SIGNED_FIELDS = tuple(sorted(_EXPECTED_FIELDS - {"signature"}))
_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    """Base exception for update failures safe to surface as a short status."""


class UpdateConfigurationError(UpdateError):
    """The updater is disabled because its trusted configuration is invalid."""


class ManifestValidationError(UpdateError):
    """The update manifest is malformed or violates the update policy."""


class ManifestVerificationError(UpdateError):
    """The manifest signature could not be verified."""


def _decode_public_key(value: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise UpdateConfigurationError("업데이트 공개 키가 설정되지 않았습니다.")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise UpdateConfigurationError("업데이트 공개 키 형식이 올바르지 않습니다.") from exc
    if len(raw) != 32:
        raise UpdateConfigurationError("업데이트 공개 키 길이가 올바르지 않습니다.")
    return raw


def validate_https_url(url: str, allowed_hosts: frozenset[str] | set[str]) -> str:
    """Validate an HTTPS URL against a fixed hostname allow-list.

    User information and non-standard ports are rejected to avoid accidentally
    sending update requests to a lookalike or credential-bearing URL.
    """

    if not isinstance(url, str) or not url:
        raise ManifestValidationError("업데이트 URL이 비어 있습니다.")
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    normalized_hosts = {str(host).rstrip(".").lower() for host in allowed_hosts}
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManifestValidationError("업데이트 URL 포트가 올바르지 않습니다.") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or hostname not in normalized_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
    ):
        raise ManifestValidationError("허용되지 않은 업데이트 URL입니다.")
    return url


@dataclass(frozen=True)
class UpdateConfig:
    manifest_url: str
    public_key: bytes
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if len(self.public_key) != 32:
            raise UpdateConfigurationError("업데이트 공개 키 길이가 올바르지 않습니다.")
        if not self.allowed_hosts:
            raise UpdateConfigurationError("업데이트 호스트 허용 목록이 비어 있습니다.")
        try:
            validate_https_url(self.manifest_url, self.allowed_hosts)
        except ManifestValidationError as exc:
            raise UpdateConfigurationError(str(exc)) from exc
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise UpdateConfigurationError("업데이트 연결 제한 시간이 올바르지 않습니다.")

    @classmethod
    def load(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        embedded_public_key_b64: str | None = None,
        manifest_url: str = DEFAULT_MANIFEST_URL,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
    ) -> "UpdateConfig":
        """Load trusted updater configuration, failing closed when absent.

        Production builds should set ``EMBEDDED_PUBLIC_KEY_B64``. The
        environment variable is retained for local release/testing workflows;
        it cannot expand the compiled hostname allow-list.
        """

        values = os.environ if environ is None else environ
        embedded = EMBEDDED_PUBLIC_KEY_B64 if embedded_public_key_b64 is None else embedded_public_key_b64
        encoded_key = embedded.strip() or values.get(PUBLIC_KEY_ENVIRONMENT_VARIABLE, "").strip()
        return cls(
            manifest_url=manifest_url,
            public_key=_decode_public_key(encoded_key),
            allowed_hosts=frozenset(allowed_hosts),
        )


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    release_sequence: int
    version: str
    download_url: str
    size: int
    sha256: str
    notes: tuple[str, ...]

    def is_newer_than(self, current_release_sequence: int) -> bool:
        if isinstance(current_release_sequence, bool) or not isinstance(current_release_sequence, int):
            raise TypeError("current_release_sequence must be an integer")
        return self.release_sequence > current_release_sequence


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"중복된 업데이트 필드입니다: {key}")
        result[key] = value
    return result


def canonical_signed_payload(values: Mapping[str, Any]) -> bytes:
    """Return the exact deterministic bytes covered by the signature."""

    signed = {field: values[field] for field in _SIGNED_FIELDS}
    return json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_and_verify_manifest(payload: bytes, config: UpdateConfig) -> UpdateManifest:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not payload or len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("업데이트 정보 크기가 올바르지 않습니다.")
    try:
        decoded = payload.decode("utf-8")
        values = json.loads(decoded, object_pairs_hook=_strict_object)
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("업데이트 정보가 UTF-8 형식이 아닙니다.") from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError("업데이트 정보 JSON이 올바르지 않습니다.") from exc
    if not isinstance(values, dict):
        raise ManifestValidationError("업데이트 정보는 JSON 객체여야 합니다.")
    fields = frozenset(values)
    if fields != _EXPECTED_FIELDS:
        missing = sorted(_EXPECTED_FIELDS - fields)
        extra = sorted(fields - _EXPECTED_FIELDS)
        detail = []
        if missing:
            detail.append(f"누락={','.join(missing)}")
        if extra:
            detail.append(f"허용되지 않음={','.join(extra)}")
        raise ManifestValidationError("업데이트 필드가 올바르지 않습니다" + (f" ({'; '.join(detail)})" if detail else ""))

    schema_version = values["schema_version"]
    release_sequence = values["release_sequence"]
    version = values["version"]
    size = values["size"]
    sha256 = values["sha256"]
    notes = values["notes"]
    signature = values["signature"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ManifestValidationError("업데이트 스키마 버전이 올바르지 않습니다.")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("지원하지 않는 업데이트 스키마입니다.")
    if isinstance(release_sequence, bool) or not isinstance(release_sequence, int) or release_sequence <= 0:
        raise ManifestValidationError("업데이트 순번이 올바르지 않습니다.")
    if not isinstance(version, str) or not _VERSION_PATTERN.fullmatch(version):
        raise ManifestValidationError("표시 버전이 올바르지 않습니다.")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ManifestValidationError("업데이트 파일 크기가 올바르지 않습니다.")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise ManifestValidationError("업데이트 SHA-256 값이 올바르지 않습니다.")
    if (
        not isinstance(notes, list)
        or len(notes) > 12
        or any(
            not isinstance(note, str)
            or not note.strip()
            or len(note) > 120
            or note != note.strip()
            for note in notes
        )
    ):
        raise ManifestValidationError("업데이트 내역이 올바르지 않습니다.")
    validate_https_url(values["download_url"], config.allowed_hosts)
    if not isinstance(signature, str):
        raise ManifestValidationError("업데이트 서명이 올바르지 않습니다.")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ManifestValidationError("업데이트 서명 형식이 올바르지 않습니다.") from exc
    if len(signature_bytes) != 64:
        raise ManifestValidationError("업데이트 서명 길이가 올바르지 않습니다.")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ManifestVerificationError("업데이트 서명 검증 구성 요소가 없습니다.") from exc
    try:
        Ed25519PublicKey.from_public_bytes(config.public_key).verify(
            signature_bytes,
            canonical_signed_payload(values),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ManifestVerificationError("업데이트 서명을 확인할 수 없습니다.") from exc

    return UpdateManifest(
        schema_version=schema_version,
        release_sequence=release_sequence,
        version=version,
        download_url=values["download_url"],
        size=size,
        sha256=sha256,
        notes=tuple(notes),
    )
