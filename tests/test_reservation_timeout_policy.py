import inspect

from engines.dpsnnn_engine import DpsnnnEngine
from engines.jigubyeol_engine import JigubyeolEngine
from engines.keyescape_engine import KeyescapeEngine
from engines.naver_api import REQUEST_TIMEOUT as NAVER_API_REQUEST_TIMEOUT
from engines.zeroworld_gu_engine import ZeroWorldGuEngine
from engines.zeroworld_shin_engine import ZeroWorldShinEngine


def test_read_only_lookup_budgets_are_not_shortened_below_recovery_floor():
    assert JigubyeolEngine.LOOKUP_TIMEOUT >= 5
    assert ZeroWorldGuEngine.LOOKUP_TIMEOUT >= 5
    assert ZeroWorldShinEngine.LOOKUP_TIMEOUT_SECONDS >= 5
    assert DpsnnnEngine.REQUEST_TIMEOUT >= 5
    assert NAVER_API_REQUEST_TIMEOUT >= 5
    assert inspect.signature(KeyescapeEngine._post).parameters["timeout"].default >= 5


def test_non_idempotent_submit_budgets_keep_a_longer_completion_window():
    assert JigubyeolEngine.SUBMIT_TIMEOUT >= 8
    assert ZeroWorldGuEngine.SUBMIT_TIMEOUT >= 8
    assert ZeroWorldShinEngine.SUBMIT_TIMEOUT_SECONDS >= 8
    assert DpsnnnEngine.REQUEST_TIMEOUT >= 8
