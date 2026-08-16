"""Generate the offline Ed25519 key used to sign update manifests."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Sequence


class KeyGenerationError(ValueError):
    """A safe key-generation input or filesystem error."""


def _write_new_file(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise KeyGenerationError(f"기존 파일을 덮어쓸 수 없습니다: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(path, mode)
        except OSError:
            # Windows ACLs do not map perfectly to POSIX modes.  O_EXCL still
            # prevents accidental replacement; deployment should additionally
            # protect the key directory with an account-only ACL.
            pass
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def generate_key_pair(
    *,
    private_key_path: str | os.PathLike[str],
    public_key_path: str | os.PathLike[str] | None = None,
) -> tuple[Path, str, Path | None]:
    """Generate a new private PEM and return its raw public key as base64."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_path = Path(private_key_path).expanduser().resolve()
    public_path = Path(public_key_path).expanduser().resolve() if public_key_path else None
    if public_path == private_path:
        raise KeyGenerationError("개인 키와 공개 키 출력 경로는 달라야 합니다.")
    if private_path.exists():
        raise KeyGenerationError(f"기존 파일을 덮어쓸 수 없습니다: {private_path}")
    if public_path is not None and public_path.exists():
        raise KeyGenerationError(f"기존 파일을 덮어쓸 수 없습니다: {public_path}")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_b64 = base64.b64encode(public_raw).decode("ascii")

    _write_new_file(private_path, private_pem, 0o600)
    try:
        if public_path is not None:
            _write_new_file(public_path, (public_b64 + "\n").encode("ascii"), 0o644)
    except BaseException:
        # Do not leave a private key behind when the requested public output
        # could not be completed.
        try:
            private_path.unlink()
        except OSError:
            pass
        raise
    return private_path, public_b64, public_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pengucro 업데이트 Ed25519 서명 키를 생성합니다.")
    parser.add_argument("--private-key", required=True, help="새 개인 PEM 키 경로 (기존 파일 덮어쓰기 금지)")
    parser.add_argument(
        "--public-key-output",
        help="선택: base64 공개 키를 저장할 파일. 생략하면 표준 출력으로만 표시합니다.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        private_path, public_b64, public_path = generate_key_pair(
            private_key_path=args.private_key,
            public_key_path=args.public_key_output,
        )
    except (KeyGenerationError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    print(f"개인 키 생성 완료: {private_path}", file=sys.stderr)
    if public_path is not None:
        print(f"공개 키 저장 완료: {public_path}", file=sys.stderr)
    else:
        # Only the public key is safe and expected on stdout.  This value is
        # embedded into the application before a production build.
        print(public_b64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
