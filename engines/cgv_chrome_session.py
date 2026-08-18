from __future__ import annotations

import threading

from engines import browser_session


class _CgvSlotOneManager:
    """Own CGV's single persistent Chrome profile on slot 1 / port 9333.

    CGV checkout and Naver Pay saved-card state live in the first Pengucro
    profile. Falling through to slot 2/3 silently switches profiles and can
    require a fresh CGV/Naver login. Keep CGV on slot 1 instead of using the
    generic first-free-slot allocator.

    Reconnects inside one booking may call ``start`` while the same process
    still owns the slot-1 lease. In that case return/restart the cached session
    rather than trying to acquire a second slot.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session = None

    @staticmethod
    def _emit(log, message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    def start(self, log=None):
        with self._lock:
            current = self._session
            if current is not None and getattr(current, "lease", None) is not None:
                port = int(getattr(current, "port", browser_session.DEFAULT_CDP_PORT))
                if browser_session.cdp_descriptor(port):
                    return current

                # Chrome itself died but this process still owns slot 1. Reuse
                # the existing lease/profile and restart exactly the same slot.
                restarted = browser_session.start_or_attach(
                    browser_session.DEFAULT_CDP_PORT,
                    log,
                    profile_path=browser_session.profile_dir(1),
                    allow_port_fallback=False,
                    lease=current.lease,
                )
                if restarted is not None:
                    self._session = restarted
                return restarted

            lease = browser_session.acquire_chrome_slot(1)
            if lease is None:
                self._emit(
                    log,
                    "[CGV] Chrome 슬롯 1(포트 9333)이 다른 Pengucro 실행에서 사용 중입니다. "
                    "CGV/N pay 로그인 프로필 보호를 위해 슬롯 2·3으로 전환하지 않습니다.",
                    "warning",
                )
                return None

            self._emit(
                log,
                "[CGV] 로그인/N pay 저장카드가 있는 Chrome 슬롯 1번을 사용합니다. (포트 9333)",
                "info",
            )
            session = browser_session.start_or_attach(
                lease.port,
                log,
                profile_path=lease.profile_path,
                allow_port_fallback=False,
                lease=lease,
            )
            if session is None:
                lease.release()
                return None
            self._session = session
            return session


_MANAGER = _CgvSlotOneManager()


def start_cgv_chrome(log=None):
    return _MANAGER.start(log=log)


class CgvBrowserSessionProxy:
    """Proxy used only by engines.cgv_engine.

    The base engine references a module-level ``browser_session`` object in a
    few reconnect paths. Replacing that one reference with this proxy pins only
    CGV to slot 1 while every other engine keeps the generic slot allocator.
    """

    def __getattr__(self, name):
        return getattr(browser_session, name)

    @staticmethod
    def start_isolated(log=None, slot_count=None):
        del slot_count
        return start_cgv_chrome(log=log)
