from engines.cgv_engine_priority_ladder import CgvEngine as PriorityLadderCgvEngine
from engines.cgv_engine_priority_ladder_runtime import CgvEngine
from engines.cgv_engine_visitor_dom_runtime import CgvEngine as VisitorDomCgvEngine
from engines.registry import EngineRegistry
from pengucro.models import STANDARD_MODE


def noop(*_args, **_kwargs):
    return None


def test_final_ladder_preserves_member_cust_no_in_reused_seat_url():
    engine = CgvEngine(noop)
    page = object()
    engine._priority_seed_page = page
    engine._browser_auth_data = lambda observed: {"custNo": "member-42"} if observed is page else {}
    engine._seat_url = lambda _schedule, cust_no="": f"seat-url?custNo={cust_no}"

    engine._seed_initial_payload({"siteNo": "0013"}, {"statusCode": 0})

    assert engine._initial_seat_response["url"] == "seat-url?custNo=member-42"
    assert engine._initial_seat_response["data"] == {"statusCode": 0}


def test_registry_uses_final_ladder_without_losing_visitor_runtime():
    engine = EngineRegistry.create(
        site_name="CGV",
        mode=STANDARD_MODE,
        payload={},
        custom_sites={},
        log_callback=noop,
        success_callback=noop,
    )

    assert isinstance(engine, CgvEngine)
    assert isinstance(engine, PriorityLadderCgvEngine)
    assert isinstance(engine, VisitorDomCgvEngine)
