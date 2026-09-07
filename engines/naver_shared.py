"""Coordinate public Naver reads and same-account submissions on this PC.

Only public hourly schedules are cached. Profile paths, account IDs, cookies,
request bodies and reservation numbers are never written to these files.
Submission records are separate from the read budget so arming a POST never
waits for a public-read permit. A sent/uncertain record survives owner death.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date, time as clock_time, timedelta
from pathlib import Path

import requests

from engines.browser_session import _pid_alive
from pengucro.storage import _exclusive_json_lock, _write_json_unlocked, data_path


PUBLIC_READ_OPERATIONS = frozenset({"hourlySchedule", "Slot", "business", "bizItem"})
CACHE_MAX_AGE = 2.0
CACHE_MAX_ENTRIES = 64
CACHE_MAX_BYTES = 128 * 1024
SUBMISSION_MAX_ENTRIES = 2048
SUBMISSION_MAX_ALIASES = 32


class NaverReadCancelled(requests.RequestException):
    """A public-read wait was cancelled before sending the request."""


class NaverReadDeadline(TimeoutError):
    """No read permit was available before the monotonic deadline."""


class NaverSharedStateError(RuntimeError):
    """Submission ownership could not safely be established."""


@contextmanager
def _shared_json_lock(path: Path, *, timeout_seconds: float = .1,
                      deadline: float | None = None):
    """Retry transient Windows lock-file creation errors before entering only.

    Several processes can see the initial empty lock file before another
    process locks its first byte. Its sentinel write can then briefly fail.
    All retries share one deadline; failures in the protected body or lock
    cleanup must propagate because a state write may already have completed.
    """
    end = time.monotonic() + timeout_seconds
    if deadline is not None:
        end = min(end, deadline)
    with ExitStack() as stack:
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("네이버 공유 파일 잠금 대기 시간이 초과되었습니다.")
            try:
                stack.enter_context(_exclusive_json_lock(path, timeout_seconds=remaining))
            except PermissionError:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("네이버 공유 파일 잠금 대기 시간이 초과되었습니다.") from None
                time.sleep(min(.01, remaining))
                continue
            break
        # The common lock helper has its own minimum wait interval. If that
        # used the remaining budget, release the lock without entering a body.
        if time.monotonic() >= end:
            raise TimeoutError("네이버 공유 파일 잠금 대기 시간이 초과되었습니다.")
        yield


def _load(path: Path, *, strict: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid shared state")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        if strict:
            raise NaverSharedStateError("네이버 제출 보호 기록을 확인할 수 없습니다.") from exc
        return {}


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _public_cache_value(value) -> bool:
    """Reject credential fields even if a caller accidentally passes a body."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "".join(c.lower() for c in str(key) if c.isalnum())
            if (any(word in normalized for word in ("cookie", "token", "password", "secret"))
                    or normalized in {"authorization", "account", "userid", "nickname", "phone",
                                      "email", "bookingid", "reservationid", "currentdatetime"}):
                return False
            if not _public_cache_value(child):
                return False
    elif isinstance(value, list):
        return all(_public_cache_value(item) for item in value)
    return value is None or isinstance(value, (dict, list, str, bool, int, float))


class NaverSharedCoordinator:
    def __init__(self, directory: str | Path | None = None, read_interval: float = .2):
        self.directory = Path(directory) if directory is not None else data_path("naver-shared")
        self.directory.mkdir(parents=True, exist_ok=True)
        interval = float(read_interval)
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("read_interval must be finite and nonnegative")
        self.read_interval = interval
        self.budget_path = self.directory / "public-read-budget.json"
        self.cache_path = self.directory / "public-schedule-cache.json"
        self.submission_path = self.directory / "submission-guards.json"

    def acquire_read(self, operation: str, *, stop_event=None, deadline: float | None = None) -> float:
        """Reserve one public request; deadline uses time.monotonic().

        Call from the existing HTTP worker, not an asyncio event loop. Waiting
        releases the filesystem lock and checks cancellation at least every
        50 ms apart from bounded file-lock acquisition. Mutations are rejected.
        """
        if operation not in PUBLIC_READ_OPERATIONS:
            raise ValueError("only public Naver reads use this budget")
        started = time.monotonic()
        while True:
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                raise NaverReadCancelled("네이버 조회 대기가 중지되었습니다.")
            if deadline is not None and now >= deadline:
                raise NaverReadDeadline("네이버 조회 예산 대기 시간이 초과되었습니다.")
            try:
                with _shared_json_lock(self.budget_path, deadline=deadline):
                    now = time.monotonic()
                    if stop_event is not None and stop_event.is_set():
                        raise NaverReadCancelled("네이버 조회 대기가 중지되었습니다.")
                    if deadline is not None and now >= deadline:
                        raise NaverReadDeadline("네이버 조회 예산 대기 시간이 초과되었습니다.")
                    state = _load(self.budget_path)
                    last = float(state.get("last", 0))
                    # Monotonic clocks are shared by local processes; a reboot
                    # resets the origin. Wall-clock corrections do not affect it.
                    next_read = float(state.get("next_read", 0)) if last <= now else 0.0
                    delay = next_read - now
                    if delay <= 0:
                        _write_json_unlocked(self.budget_path, {
                            "last": now, "next_read": now + self.read_interval,
                        })
                        return time.monotonic() - started
            except NaverReadDeadline:
                raise
            except TimeoutError:
                delay = .01
            wait = min(.05, max(.001, delay))
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            if stop_event is not None:
                stop_event.wait(wait)
            else:
                time.sleep(wait)

    def get_public_read(self, operation: str, variables: dict, *, max_age: float = .15):
        """Return a fresh public schedule or None; never reuse clock samples."""
        if operation != "hourlySchedule" or not _public_cache_value(variables):
            return None
        try:
            with _shared_json_lock(self.cache_path):
                row = _load(self.cache_path).get(_digest([operation, variables]), {})
                age = time.monotonic() - float(row.get("created", 0))
                if (0 <= age <= min(CACHE_MAX_AGE, float(max_age), float(row.get("ttl", 0)))
                        and _public_cache_value(row.get("data"))):
                    return row.get("data")
        except (OSError, TimeoutError, TypeError, ValueError, AttributeError):
            pass
        return None

    def put_public_read(self, operation: str, variables: dict, data: dict, *, ttl: float = .15) -> bool:
        if operation != "hourlySchedule" or not isinstance(data, dict):
            return False
        # The HTTP timing window belongs to the producing process/request.
        public_data = {key: value for key, value in data.items() if key != "__rtt_window__"}
        if not _public_cache_value(variables) or not _public_cache_value(public_data):
            return False
        ttl = min(CACHE_MAX_AGE, max(0.0, float(ttl)))
        if not math.isfinite(ttl) or ttl <= 0:
            return False
        row = {"created": time.monotonic(), "ttl": ttl, "data": public_data}
        if len(json.dumps(row, ensure_ascii=False).encode("utf-8")) > CACHE_MAX_BYTES:
            return False
        try:
            with _shared_json_lock(self.cache_path):
                now = time.monotonic()
                current = _load(self.cache_path)
                fresh = {key: value for key, value in current.items()
                         if isinstance(value, dict)
                         and 0 <= now - float(value.get("created", 0)) <= float(value.get("ttl", 0))}
                fresh[_digest([operation, variables])] = row
                while (len(fresh) > CACHE_MAX_ENTRIES
                       or len(json.dumps(fresh, ensure_ascii=False, indent=2).encode("utf-8")) > CACHE_MAX_BYTES):
                    oldest = min(fresh, key=lambda key: fresh[key]["created"])
                    del fresh[oldest]
                _write_json_unlocked(self.cache_path, fresh)
            return True
        except (OSError, TimeoutError, TypeError, ValueError):
            return False

    @staticmethod
    def _submission_keys(*, profile_identity: str, business_id: str, biz_item_id: str,
                         date_str: str, time_str: str, account_id: str = "") -> tuple[str, ...]:
        """Tie account and profile identities to the same exact target.

        A known account never drops its profile alias. Consequently switching
        accounts in the same profile does not bypass an unresolved/confirmed
        guard for that target. Independent accounts can use separate profiles.
        """
        if not account_id and not str(profile_identity).strip():
            raise ValueError("an account or profile identity is required")
        if not all(str(part).strip() for part in (business_id, biz_item_id, date_str, time_str)):
            raise ValueError("a complete reservation target is required")
        date.fromisoformat(str(date_str))
        normalized_time = clock_time.fromisoformat(str(time_str)).isoformat(timespec="seconds")
        identities = []
        if account_id:
            identities.append(("account", str(account_id)))
        if str(profile_identity).strip():
            identities.append((
                "profile", os.path.normcase(os.path.abspath(str(profile_identity))),
            ))
        return tuple(
            _digest([identity, str(business_id), str(biz_item_id), str(date_str), normalized_time])
            for identity in identities
        )

    @staticmethod
    def _submission_key(**target) -> str:
        return NaverSharedCoordinator._submission_keys(**target)[0]

    @staticmethod
    def _matching_submissions(state: dict, keys: tuple[str, ...]) -> list[tuple[str, dict]]:
        wanted = set(keys)
        matches = {}
        # Include transitive aliases if an older state contains overlapping rows.
        # A live account guard must not disappear when a dead profile-only owner
        # is reclaimed through another alias.
        changed = True
        while changed:
            changed = False
            for key, row in state.items():
                if key in matches:
                    continue
                aliases = row.get("aliases", []) if isinstance(row, dict) else []
                if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
                    raise NaverSharedStateError("네이버 제출 보호 별칭을 확인할 수 없습니다.")
                row_keys = {key, *aliases}
                if not wanted.intersection(row_keys):
                    continue
                if not isinstance(row, dict):
                    raise NaverSharedStateError("네이버 제출 보호 기록을 확인할 수 없습니다.")
                matches[key] = row
                wanted.update(row_keys)
                changed = True
        return list(matches.items())

    def submission_state(self, **target) -> dict | None:
        keys = self._submission_keys(**target)
        with _shared_json_lock(self.submission_path):
            matches = self._matching_submissions(_load(self.submission_path, strict=True), keys)
            if not matches:
                return None
            row = matches[0][1]
            return {"state": row.get("state", "unknown"),
                    "reason": "같은 계정 또는 프로필의 동일 슬롯 제출 기록이 있습니다."}

    def try_acquire_submission(self, **target) -> SubmissionLease | None:
        keys = self._submission_keys(**target)
        key = keys[0]
        with _shared_json_lock(self.submission_path):
            state = _load(self.submission_path, strict=True)
            matches = self._matching_submissions(state, keys)
            account_key = keys[0] if target.get("account_id") else ""
            known_accounts = {
                row.get("account_alias") for _matched_key, row in matches
                if row.get("account_alias")
            }
            aliases = set(keys)
            if account_key and known_accounts and account_key not in known_accounts:
                # A changed account still conflicts in this profile, but that
                # does not establish that both accounts are the same identity.
                # Do not poison the new account's independent-profile guard.
                aliases.discard(account_key)
            for matched_key, row in matches:
                aliases.update([matched_key, *row.get("aliases", [])])
            if len(aliases) > SUBMISSION_MAX_ALIASES:
                raise NaverSharedStateError("동일 예약의 제출 보호 별칭이 가득 찼습니다.")
            if any(row.get("state") != "prepared" or _pid_alive(int(row.get("pid", 0)))
                   for _matched_key, row in matches):
                # Remember newly learned account/profile aliases even on denial.
                # Otherwise a denied known-account attempt in another profile
                # could retry from that profile with a missing account id.
                for _matched_key, row in matches:
                    row["aliases"] = sorted(aliases)
                    if account_key and not known_accounts:
                        row["account_alias"] = account_key
                _write_json_unlocked(self.submission_path, state)
                return None
            for matched_key, _row in matches:
                # Only a dead owner which never armed/submitted is reclaimable.
                del state[matched_key]
            aliases = set(keys)
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            state = {old_key: row for old_key, row in state.items()
                     if not isinstance(row, dict) or str(row.get("date", "9999")) >= cutoff}
            if len(state) >= SUBMISSION_MAX_ENTRIES and key not in state:
                raise NaverSharedStateError("네이버 제출 보호 기록이 가득 찼습니다.")
            token = uuid.uuid4().hex
            state[key] = {"state": "prepared", "pid": os.getpid(), "token": token,
                          "date": str(target["date_str"]), "created": time.time(),
                          "aliases": sorted(aliases), "account_alias": account_key}
            _write_json_unlocked(self.submission_path, state)
        return SubmissionLease(self, key, token)


class SubmissionLease:
    def __init__(self, coordinator: NaverSharedCoordinator, key: str, token: str):
        self.coordinator = coordinator
        self.key = key
        self.token = token

    def _update(self, action: str) -> bool:
        path = self.coordinator.submission_path
        with _shared_json_lock(path):
            state = _load(path, strict=True)
            row = state.get(self.key)
            if not isinstance(row, dict) or row.get("token") != self.token:
                raise NaverSharedStateError("네이버 제출 보호 소유권이 변경되었습니다.")
            current = row.get("state")
            if action == "release_unsubmitted":
                if current != "prepared":
                    return False
                del state[self.key]
            elif action == "release_after_no_submission":
                if current == "confirmed":
                    return False
                del state[self.key]
            else:
                if action == "submitted" and current != "prepared":
                    raise NaverSharedStateError("이미 제출된 슬롯을 다시 전송할 수 없습니다.")
                if current == "confirmed" and action != "confirmed":
                    return False
                row["state"] = action
                row["updated"] = time.time()
            _write_json_unlocked(path, state)
            return True

    def mark_submitted(self) -> None:
        """Persist BEFORE arming a browser timer or sending a POST."""
        self._update("submitted")

    def finish(self, outcome: str) -> None:
        if outcome not in {"uncertain", "confirmed"}:
            raise ValueError("outcome must be uncertain or confirmed")
        self._update(outcome)

    def release_unsubmitted(self) -> bool:
        return self._update("release_unsubmitted")

    def release_after_no_submission(self) -> bool:
        """Release only with verified non-send or authoritative non-creation.

        A cancelled Python wait, RT47, a timeout, owner death, empty booking
        history or an expired local TTL is NOT sufficient evidence.
        """
        return self._update("release_after_no_submission")
