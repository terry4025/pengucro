# Pengucro automatic-update release guide

This file is the hand-off contract for future development sessions. Read it
before changing the updater, versioning a release, or publishing a build.

## Production channel

- Public binary repository: `https://github.com/terry4025/pengucro-updates`
- Manifest URL compiled into the app:
  `https://github.com/terry4025/pengucro-updates/releases/latest/download/latest.json`
- The repository is intentionally binary-only. Do not push the application
  source, configuration, logs, HTML captures, cookies, or user data to it.
- The update indicator is hidden while checking, when current, and after a
  background check failure. It becomes visible only when a correctly signed
  manifest has a `release_sequence` greater than the running build.

## Signing identity

- The Ed25519 public key is embedded in `pengucro/update_manifest.py`.
- The matching private key exists only on the release machine at:
  `%USERPROFILE%\.pengucro-release\update-signing-private.pem`
- The public-key backup is:
  `%USERPROFILE%\.pengucro-release\update-signing-public.txt`
- Never commit, upload, paste, log, print, or package the private key.
- Back up the private key in an encrypted offline location. Losing it means
  existing v6.02+ installations cannot trust a new signing identity; recovery
  would require another manually distributed build.
- Do not rotate the embedded public key in an ordinary update. A signing-key
  rotation needs an explicit migration release signed by the old key.

## Version contract

For every published build, all of these must describe the same release:

1. `pengucro/__init__.py` — `__version__`
2. `pengucro/__init__.py` — strictly increasing `__release_sequence__`
3. `pengucro/patch_notes.py` — newest hard-coded patch note
4. `방탈출펭크로.spec` — executable name

Display versions are never compared as floating-point numbers. Examples:

- v6.02 → sequence `602`
- v6.03 → sequence `603`
- v6.10 → sequence `610`

## Build and verification

Run from the project root:

```powershell
.\.codex-python-extract\SourceDir\python.exe -m pytest -q
.\.codex-python-extract\SourceDir\python.exe verify_ui.py
.\.codex-python-extract\SourceDir\python.exe build_run.py
```

The user-facing EXE must be the single versioned file produced under `dist`.
Check its SHA-256 and run both smoke paths:

1. `EXE --apply-update relative.json` must exit with code 10 without opening UI.
2. Normal EXE startup must keep the GUI running and create a redacted process
   log under `%LOCALAPPDATA%\Pengucro\logs`.

## Create the signed manifest

Use an ASCII release asset name so its immutable URL is predictable. Copying to
a temporary staging directory is allowed; do not add extra files to `dist`.

Example for v6.03:

```powershell
py tools\create_update_manifest.py `
  --exe "$env:TEMP\Pengucro-v6.03.exe" `
  --version 6.03 `
  --release-sequence 603 `
  --download-url "https://github.com/terry4025/pengucro-updates/releases/download/v6.03/Pengucro-v6.03.exe" `
  --private-key "$env:USERPROFILE\.pengucro-release\update-signing-private.pem" `
  --output "$env:TEMP\latest.json" `
  --note "실제로 포함된 사용자 변경사항"
```

The manifest is accepted only when its Ed25519 signature, exact byte size, and
SHA-256 all match the uploaded EXE. Never edit `latest.json` by hand after it is
signed.

## Publish order

1. Create a GitHub Release named/tagged with the exact version, e.g. `v6.03`.
2. Upload the immutable versioned EXE asset first, e.g. `Pengucro-v6.03.exe`.
3. Upload the freshly signed `latest.json` last.
4. Mark that release as Latest and verify both public download URLs without a
   GitHub login.
5. Start the previous public EXE and confirm the blue update indicator appears.
6. Download, restart, and verify that the application reports the new internal
   version. The user's existing filename and shortcut may keep their old name;
   the updater replaces its contents in place by design.

Publishing `latest.json` last prevents clients from seeing a manifest whose EXE
asset is not available yet. Never reuse an asset URL for different bytes and
never decrease or reuse a published `release_sequence` for another build.

## Runtime behavior

- The app checks after initial UI startup and only while reservation/catalog
  work is idle, so update traffic does not enter a booking critical path.
- Download requires explicit user action.
- Apply/restart requires explicit user action.
- Active booking, catalog refresh, or another process using the same EXE defers
  replacement without stopping those processes.
- The detached helper verifies the staged file again, replaces the EXE with a
  backup, starts the new build, and rolls back if its health marker is missing.

## Current bootstrap release

v6.02 is the first manually distributed build containing this updater. A v6.02
manifest is useful for validating the public channel but does not display an
update to v6.02 itself because its sequence is equal. Users on versions older
than v6.02 must first receive v6.02 (or a later updater-enabled build) manually.
