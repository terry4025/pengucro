import pytest

from engines.zeroworld_shin_engine import ZeroWorldShinEngine


def make_engine():
    return ZeroWorldShinEngine("", lambda *_args: None)


@pytest.mark.parametrize(
    ("branch", "subject"),
    [("1", "A"), ("2", "B"), ("4", "A"), ("5", "A")],
)
def test_current_zeroworld_branches_are_supported(branch, subject):
    context = make_engine()._build_context(
        {
            "branch": branch,
            "reservationDate": "2026-08-01",
            "reservationTime": "11:00:00",
            "themePK": "28",
            "name": "테스트",
            "phone": "01012345678",
            "people": "2",
        }
    )
    assert context.branch == branch
    assert context.subject == subject
    assert context.target_time == "11:00"
    assert context.phone == "010-1234-5678"


def test_unsupported_old_branch_is_rejected():
    with pytest.raises(ValueError, match="김포·강남·홍대·다이브 건대"):
        make_engine()._build_context({"branch": "99"})


def test_submission_acceptance_rejects_failure_alert():
    assert not make_engine()._submission_accepted(
        "<script>alert('이미 예약된 시간입니다.')</script>",
        "https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code=abc",
        [],
    )
    assert make_engine()._submission_accepted(
        '<input name="code" value="abc"><form action="rev.make.mutong.php">',
        "https://zeroworldkorea.com/layout/res/home.php?go=rev.kcp&code=abc",
        [],
    )
