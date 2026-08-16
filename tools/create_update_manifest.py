"""Create and sign a Pengucro update manifest.

This is a release-time tool.  The Ed25519 private key is always supplied by an
explicit filesystem path and is never copied into the application or printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pengucro.update_manifest import (  # noqa: E402
    DEFAULT_ALLOWED_HOSTS,
    MANIFEST_SCHEMA_VERSION,
    UpdateConfig,
    UpdateError,
    canonical_signed_payload,
    parse_and_verify_manifest,
    validate_https_url,
)


class ReleaseToolError(ValueError):
    """A safe, actionable release input error."""


def _existing_file(path: str, *, label: str) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_file():
        raise ReleaseToolError(f"{label} 파일을 찾을 수 없습니다: {value}")
    return value


def validate_executable(path: str | os.PathLike[str]) -> Path:
    """Resolve *path* and reject obvious non-Windows/non-release artifacts."""

    executable = _existing_file(os.fspath(path), label="실행")
    if executable.suffix.lower() != ".exe":
        raise ReleaseToolError("업데이트 파일은 .exe 형식이어야 합니다.")
    if executable.stat().st_size <= 0:
        raise ReleaseToolError("업데이트 실행 파일이 비어 있습니다.")
    with executable.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ReleaseToolError("업데이트 파일이 Windows 실행 파일이 아닙니다.")
    return executable


def load_private_key(path: str | os.PathLike[str]):
    private_path = _existing_file(os.fspath(path), label="개인 키")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ReleaseToolError("Ed25519 PEM 개인 키를 읽을 수 없습니다.") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseToolError("업데이트 서명 키는 Ed25519 개인 키여야 합니다.")
    return key


def sha256_file(path: Path, *, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(block_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_signed_manifest(
    *,
    executable: str | os.PathLike[str],
    version: str,
    release_sequence: int,
    download_url: str,
    notes: Sequence[str],
    private_key_path: str | os.PathLike[str],
) -> dict[str, object]:
    """Build a manifest and verify it once with the derived public key."""

    exe_path = validate_executable(executable)
    if isinstance(release_sequence, bool) or not isinstance(release_sequence, int):
        raise ReleaseToolError("릴리스 순번은 양의 정수여야 합니다.")
    validate_https_url(download_url, DEFAULT_ALLOWED_HOSTS)
    private_key = load_private_key(private_key_path)

    values: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_sequence": release_sequence,
        "version": version,
        "notes": list(notes),
        "download_url": download_url,
        "size": exe_path.stat().st_size,
        "sha256": sha256_file(exe_path),
    }
    values["signature"] = base64.b64encode(
        private_key.sign(canonical_signed_payload(values))
    ).decode("ascii")

    # Use the exact production parser as the final release-time validation.
    from cryptography.hazmat.primitives import serialization

    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    config = UpdateConfig(
        manifest_url="https://github.com/terry4025/pengucro-updates/releases/latest/download/latest.json",
        public_key=public_key,
    )
    parse_and_verify_manifest(
        json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        config,
    )
    return values


def write_manifest(path: str | os.PathLike[str], values: dict[str, object]) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    data = (json.dumps(values, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pengucro 업데이트 EXE의 서명된 latest.json을 생성합니다."
    )
    parser.add_argument("--exe", required=True, help="배포할 Windows EXE 경로")
    parser.add_argument("--version", required=True, help="표시 버전 (예: 6.02)")
    parser.add_argument("--release-sequence", required=True, type=int, help="항상 증가하는 정수")
    parser.add_argument("--download-url", required=True, help="허용된 HTTPS 호스트의 EXE URL")
    parser.add_argument("--private-key", required=True, help="Ed25519 PEM 개인 키 경로")
    parser.add_argument("--output", required=True, help="생성할 latest.json 경로")
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="업데이트 안내 문구 (최대 12개, 여러 번 지정 가능)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        executable = Path(args.exe).expanduser().resolve()
        private_key = Path(args.private_key).expanduser().resolve()
        output = Path(args.output).expanduser().resolve()
        if output in {executable, private_key}:
            raise ReleaseToolError("출력 경로는 실행 파일이나 개인 키 경로와 달라야 합니다.")
        values = create_signed_manifest(
            executable=executable,
            version=args.version,
            release_sequence=args.release_sequence,
            download_url=args.download_url,
            notes=args.note,
            private_key_path=private_key,
        )
        written = write_manifest(output, values)
    except (ReleaseToolError, UpdateError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    print(f"업데이트 매니페스트 생성 완료: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
