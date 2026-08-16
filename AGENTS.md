# Release patch-note rules

For every user-facing build starting with v5.70, updating the bundled patch
notes is part of completing the build.

- Update `pengucro/__init__.py`, the executable name in `방탈출펭크로.spec`, and
  the newest entry in `pengucro/patch_notes.py` to the same display version.
- Increase the integer `__release_sequence__` in `pengucro/__init__.py` for every
  released build. It must be strictly greater than every previously published
  sequence and must identify the same build as the display version, executable
  name, and newest patch-note entry. Never compare or convert display versions
  as floating-point numbers; automatic update ordering uses only the release
  sequence.
- Add the newest `PatchNote` at the beginning of `PATCH_NOTES`; never delete
  older released entries.
- Write the changes in short Korean bullet points. Include only concrete changes
  actually shipped in that version.
- Do not include planned work, promotional wording, test counts, implementation
  details, filenames, or unchanged behavior.
- Use one bullet per user-visible fix or improvement and do not repeat older
  release bullets in the newest release.
- A release build is incomplete if its version has no matching patch-note entry
  or its release sequence was not increased and synchronized for that build.
- Before handing off a build, run `tests/test_patch_notes.py` and verify that the
  in-app patch-note button opens the bundled notes for the current version.
- For every automatic-update release, read and follow
  `docs/AUTO_UPDATE_RELEASE.md`. Upload the immutable versioned EXE first and
  the signed `latest.json` last. Never publish, copy, print, or commit the
  private signing key.
