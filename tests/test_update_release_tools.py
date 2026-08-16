from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from pengucro.update_manifest import UpdateConfig, parse_and_verify_manifest
from tools.create_update_manifest import ReleaseToolError, create_signed_manifest, main, write_manifest
from tools.generate_update_key import KeyGenerationError, generate_key_pair


def _fake_exe(path: Path, payload: bytes = b"release") -> Path:
    path.write_bytes(b"MZ" + payload)
    return path


def test_generated_key_signs_a_manifest_accepted_by_production_parser(tmp_path: Path):
    private_path, public_b64, public_path = generate_key_pair(
        private_key_path=tmp_path / "offline" / "update-private.pem",
        public_key_path=tmp_path / "update-public.txt",
    )
    assert public_path is not None
    assert private_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert public_path.read_text(encoding="ascii").strip() == public_b64

    executable = _fake_exe(tmp_path / "Pengucro-v6.02.exe")
    values = create_signed_manifest(
        executable=executable,
        version="6.02",
        release_sequence=602,
        download_url=(
            "https://github.com/terry4025/pengucro-updates/"
            "releases/download/v6.02/Pengucro-v6.02.exe"
        ),
        notes=["자동 업데이트 기능 추가", "진단 로그 개선"],
        private_key_path=private_path,
    )
    manifest_path = write_manifest(tmp_path / "latest.json", values)
    config = UpdateConfig(
        manifest_url="https://github.com/terry4025/pengucro-updates/releases/latest/download/latest.json",
        public_key=base64.b64decode(public_b64),
    )

    parsed = parse_and_verify_manifest(manifest_path.read_bytes(), config)

    assert parsed.version == "6.02"
    assert parsed.release_sequence == 602
    assert parsed.notes == ("자동 업데이트 기능 추가", "진단 로그 개선")
    assert parsed.size == executable.stat().st_size


def test_key_generation_never_overwrites_existing_private_key(tmp_path: Path):
    private_path = tmp_path / "update-private.pem"
    private_path.write_text("keep", encoding="utf-8")

    with pytest.raises(KeyGenerationError, match="덮어쓸"):
        generate_key_pair(private_key_path=private_path)

    assert private_path.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("release.zip", b"MZdata"),
        ("release.exe", b"not-a-windows-executable"),
        ("release.exe", b""),
    ],
)
def test_manifest_tool_rejects_wrong_release_artifact(tmp_path: Path, filename: str, contents: bytes):
    private_path, _, _ = generate_key_pair(private_key_path=tmp_path / "private.pem")
    artifact = tmp_path / filename
    artifact.write_bytes(contents)

    with pytest.raises(ReleaseToolError):
        create_signed_manifest(
            executable=artifact,
            version="6.02",
            release_sequence=602,
            download_url="https://github.com/owner/project/releases/download/v6.02/release.exe",
            notes=[],
            private_key_path=private_path,
        )


def test_private_key_is_not_written_into_manifest(tmp_path: Path):
    private_path, _, _ = generate_key_pair(private_key_path=tmp_path / "private.pem")
    executable = _fake_exe(tmp_path / "release.exe")
    values = create_signed_manifest(
        executable=executable,
        version="6.02",
        release_sequence=602,
        download_url="https://github.com/owner/project/releases/download/v6.02/release.exe",
        notes=[],
        private_key_path=private_path,
    )

    serialized = json.dumps(values)
    assert "PRIVATE KEY" not in serialized
    assert str(private_path) not in serialized


def test_cli_reports_invalid_version_without_a_traceback(tmp_path: Path, capsys):
    private_path, _, _ = generate_key_pair(private_key_path=tmp_path / "private.pem")
    executable = _fake_exe(tmp_path / "release.exe")

    exit_code = main(
        [
            "--exe",
            str(executable),
            "--version",
            "v6.02",
            "--release-sequence",
            "602",
            "--download-url",
            "https://github.com/owner/project/releases/download/v6.02/release.exe",
            "--private-key",
            str(private_path),
            "--output",
            str(tmp_path / "latest.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("오류:")
    assert "Traceback" not in captured.err
