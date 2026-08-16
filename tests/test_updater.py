from __future__ import annotations

import base64
import hashlib
import json
import threading

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pengucro.update_manifest import (
    UpdateConfig,
    UpdateManifest,
    canonical_signed_payload,
)
from pengucro.updater import (
    ExecutableInstanceRegistry,
    UpdateCheckStatus,
    UpdateDownloadError,
    UpdateNetworkError,
    UpdateService,
    _safe_get,
    cleanup_stale_update_artifacts,
    download_update,
    prepare_and_launch_helper,
    read_latest_helper_status,
    try_create_update_service,
)


class FakeResponse:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), max(1, min(chunk_size, 7))):
            yield self.body[index : index + max(1, min(chunk_size, 7))]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _config_and_payload(file_body=b"new executable"):
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
        "size": len(file_body),
        "sha256": hashlib.sha256(file_body).hexdigest(),
        "notes": ["자동 업데이트를 추가했습니다"],
    }
    values["signature"] = base64.b64encode(private_key.sign(canonical_signed_payload(values))).decode("ascii")
    config = UpdateConfig(
        manifest_url="https://github.com/terry4025/pengucro-updates/releases/latest/download/latest.json",
        public_key=public_key,
    )
    manifest = UpdateManifest(
        schema_version=1,
        release_sequence=values["release_sequence"],
        version=values["version"],
        download_url=values["download_url"],
        size=values["size"],
        sha256=values["sha256"],
        notes=tuple(values["notes"]),
    )
    return config, json.dumps(values).encode("utf-8"), manifest


def test_check_now_reports_available_only_for_newer_signed_sequence():
    config, payload, _manifest = _config_and_payload()
    response = FakeResponse(payload, headers={"Content-Length": str(len(payload))})
    service = UpdateService(config, 601, session=FakeSession([response]))

    result = service.check_now()

    assert result.status is UpdateCheckStatus.AVAILABLE
    assert result.available
    assert result.manifest.version == "6.02"
    assert response.closed
    requested_url = service.session.calls[0][0]
    assert "pengucro_check=601-" in requested_url


def test_background_check_invokes_callback_without_blocking_caller():
    config, payload, _manifest = _config_and_payload()
    service = UpdateService(config, 602, session=FakeSession([FakeResponse(payload)]))
    completed = threading.Event()
    results = []

    thread = service.check_in_background(lambda result: (results.append(result), completed.set()))

    assert thread.daemon
    assert completed.wait(2)
    assert results[0].status is UpdateCheckStatus.UP_TO_DATE


def test_check_failure_is_a_fail_closed_result_and_factory_disables_without_key():
    config, _payload, _manifest = _config_and_payload()
    service = UpdateService(config, 601, session=FakeSession([FakeResponse(b"bad json")]))

    result = service.check_now()

    assert result.status is UpdateCheckStatus.ERROR
    assert not result.available
    disabled, reason = try_create_update_service(601, environ={}, embedded_public_key_b64="")
    assert disabled is None
    assert "공개 키" in reason


def test_redirect_is_validated_before_untrusted_host_is_contacted():
    config, _payload, _manifest = _config_and_payload()
    session = FakeSession([FakeResponse(status=302, headers={"Location": "https://evil.example/app.exe"})])

    with pytest.raises(UpdateNetworkError, match="허용되지"):
        _safe_get(session, config.manifest_url, config, stream=True)

    assert len(session.calls) == 1


def test_download_streams_to_same_directory_and_verifies_hash_and_size(tmp_path):
    file_body = b"MZ" + b"secure-update" * 20
    config, _payload, manifest = _config_and_payload(file_body)
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"old")
    progress = []
    response = FakeResponse(file_body, headers={"Content-Length": str(len(file_body))})

    staged = download_update(
        manifest,
        target,
        config=config,
        session=FakeSession([response]),
        progress=lambda received, total: progress.append((received, total)),
    )

    assert staged.path.parent == target.parent
    assert staged.path.read_bytes() == file_body
    assert staged.target_executable == target.resolve()
    assert progress[-1] == (len(file_body), len(file_body))
    assert not list(tmp_path.glob("*.part"))


def test_download_hash_mismatch_removes_partial_file(tmp_path):
    body = b"different"
    config, _payload, manifest = _config_and_payload(b"expected")
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"old")
    response = FakeResponse(body, headers={"Content-Length": str(len(body))})

    with pytest.raises(UpdateDownloadError):
        download_update(manifest, target, config=config, session=FakeSession([response]))

    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.ready.exe"))


def test_download_rejects_disallowed_redirect_as_download_error(tmp_path):
    body = b"new"
    config, _payload, manifest = _config_and_payload(body)
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"old")
    redirect = FakeResponse(status=302, headers={"Location": "https://evil.example/app.exe"})

    with pytest.raises(UpdateDownloadError, match="허용되지"):
        download_update(manifest, target, config=config, session=FakeSession([redirect]))


def test_instance_registry_tracks_same_executable_and_removes_stale(monkeypatch, tmp_path):
    executable = tmp_path / "Pengucro.exe"
    executable.write_bytes(b"app")
    registry = ExecutableInstanceRegistry(executable, tmp_path / "registry")
    live = {321}
    monkeypatch.setattr(
        "pengucro.updater._process_matches_executable",
        lambda pid, _executable: pid in live,
    )
    lease = registry.register(pid=321)

    assert registry.active_pids() == (321,)
    live.clear()
    assert registry.active_pids() == ()
    assert not lease.record_path.exists()


def test_cleanup_removes_only_old_recognized_update_artifacts(monkeypatch, tmp_path):
    data = tmp_path / "data"
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"current")
    stale = tmp_path / ".Pengucro.exe.update-602-999-deadbeef.ready.exe"
    stale.write_bytes(b"stale")
    unrelated = tmp_path / "important.exe"
    unrelated.write_bytes(b"keep")
    helper = data / "updates" / "helpers" / "PengucroUpdater-999-deadbeef.exe"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"helper")
    plan = data / "updates" / "plans" / "r602-999-deadbeef.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("pengucro.updater._process_matches_executable", lambda *_args: False)

    removed = cleanup_stale_update_artifacts(
        data_directory=data,
        target_executable=target,
        max_age_seconds=0,
    )

    assert set(removed) == {stale, helper, plan}
    assert target.exists()
    assert unrelated.exists()


def test_read_latest_helper_status_is_bounded_and_validates_shape(tmp_path):
    status = tmp_path / "updates" / "status"
    status.mkdir(parents=True)
    (status / "r1.json").write_text(
        json.dumps({"state": "success", "code": "updated", "message": "완료"}),
        encoding="utf-8",
    )

    value = read_latest_helper_status(data_directory=tmp_path)

    assert value == {"state": "success", "code": "updated", "message": "완료"}


def test_prepare_helper_writes_plan_and_uses_reset_environment(monkeypatch, tmp_path):
    target = tmp_path / "Pengucro.exe"
    target.write_bytes(b"old")
    new = b"new executable"
    config, _payload, manifest = _config_and_payload(new)
    staged_path = tmp_path / f".{target.name}.update-{manifest.release_sequence}-999-deadbeef.ready.exe"
    staged_path.write_bytes(new)
    from pengucro.updater import StagedUpdate

    monkeypatch.setenv("PENGUCRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("pengucro.updater.sys.frozen", False, raising=False)
    monkeypatch.setattr("pengucro.updater.sys.argv", [str(tmp_path / "app.py")])
    calls = []

    class Process:
        pass

    monkeypatch.setattr(
        "pengucro.updater.subprocess.Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or Process(),
    )
    registry = ExecutableInstanceRegistry(target, tmp_path / "instances")

    prepared = prepare_and_launch_helper(
        StagedUpdate(manifest, staged_path, target.resolve()),
        registry=registry,
        parent_pid=999,
        restart_args=("--normal",),
    )

    plan = json.loads(prepared.plan_path.read_text(encoding="utf-8"))
    assert plan["target_path"] == str(target.resolve())
    assert plan["staged_path"] == str(staged_path.resolve())
    assert plan["release_sequence"] == manifest.release_sequence
    assert plan["launch_args"] == ["--normal"]
    assert calls[0][0][-2:] == ["--apply-update", str(prepared.plan_path)]
    assert calls[0][1]["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
