from pengucro import __version__
from pengucro.patch_notes import PATCH_NOTES, notes_for


def test_current_build_has_newest_patch_note():
    assert PATCH_NOTES
    assert PATCH_NOTES[0].version == __version__
    assert notes_for(__version__) is PATCH_NOTES[0]
    assert all(not str(note.version).strip().lower().startswith("v") for note in PATCH_NOTES)


def test_v624_patch_notes_are_short_and_exact():
    note = notes_for("6.24")

    assert note is not None
    assert note.changes == (
        "키이스케이프 1페이지 모드의 실시간 슬롯 검증 독립 시작",
        "키이스케이프 다중 실행 시 슬롯 공유 대기 및 자체 조회 폴백 개선",
        "키이스케이프 시간표 불일치 감지 시 단일 템플릿 격리 및 자동 복구",
        "키이스케이프 오픈 경계 조회 및 서버 지연 측정 보강",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v623_patch_notes_are_short_and_exact():
    note = notes_for("6.23")

    assert note is not None
    assert note.changes == (
        "키이스케이프 1페이지 모드의 단일 탭 HTTP 실시간 슬롯 검증 유지",
        "키이스케이프 실시간 슬롯 변경 시 단일 탭 내 즉시 대상 슬롯 교체",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v622_patch_notes_are_short_and_exact():
    note = notes_for("6.22")

    assert note is not None
    assert note.changes == (
        "키이스케이프 공개 시간표의 검증 슬롯 사전 저장 추가",
        "키이스케이프 검증 슬롯 제출과 실제 시간표 확인 병렬화",
        "키이스케이프 오픈 경계 첫 조회 시점의 서버 지연 보정",
        "키이스케이프 마감 응답의 이중 확인으로 오판 방지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v621_patch_notes_are_short_and_exact():
    note = notes_for("6.21")

    assert note is not None
    assert note.changes == (
        "CGV IMAX 전용 지점 선택과 미오픈 날짜의 영화·상영관·복수 희망시간 사전 설정 추가",
        "CGV 목표 영화 공개 상태에 따라 장시간 대기 감시와 회차 공개 후 고속 좌석 감시 자동 전환",
        "CGV 상영 포맷·희망시간·좌석 우선순위의 저장·복원 및 실제 회차 매칭 정확도 개선",
        "CGV 지점·날짜·영화·상영관 변경 시 이전 시간·좌석 설정이 남는 문제 수정",
    )
    assert all(len(change) <= 55 for change in note.changes)


def test_v611_patch_notes_are_short_and_exact():
    note = notes_for("6.11")

    assert note is not None
    assert note.changes == (
        "CGV 지역·지점·영화·상영관·회차 실제 데이터 선택 추가",
        "CGV 미오픈·매진 회차의 좌석 우선순위와 취소표 감시 추가",
        "CGV 좌석 조회·가격 확인·임시선점을 공식 API 우선 방식으로 고속화",
        "CGV 화면 연결 실패 시 브라우저 방식 자동 전환 추가",
        "CGV 회원 세션 재사용과 비회원 문자 인증 예매 추가",
        "CGV 실측 결과에 따른 동시 조회 4개 상한과 자동 감속 추가",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v610_patch_notes_are_short_and_exact():
    note = notes_for("6.10")

    assert note is not None
    assert note.changes == (
        "CGV 지점·영화·상영관·좌석 우선순위 예약 감시 추가",
        "CGV 미오픈 회차 감시와 좌석 임시선점 후 결제 직전 연결 추가",
        "CGV 동시 조회 3개 상한과 접근 제한 시 자동 감속 추가",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v605_patch_notes_are_short_and_exact():
    note = notes_for("6.05")

    assert note is not None
    assert note.changes == (
        "키이스케이프 금·토·일 미오픈 시간표 빠른 제출",
        "키이스케이프 서버시간 측정 정밀도 개선",
        "키이스케이프 다중 실행 간 서버시간 측정 공유",
        "키이스케이프 캡차 발급 속도 학습 및 시점 자동 조절",
        "자동 업데이트 최신 버전 확인 지연 개선",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v604_patch_notes_are_short_and_exact():
    note = notes_for("6.04")

    assert note is not None
    assert note.changes == (
        "앱과 로딩 화면의 버전 표기 간소화",
        "본문 상단의 중복 프로그램 제목 제거",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v603_patch_notes_are_short_and_exact():
    note = notes_for("6.03")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 미오픈 날짜의 전체 테마 시간표 자동 탐색",
        "둠이스케이프 평일·주말 시간표 오적용 방지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v602_patch_notes_are_short_and_exact():
    note = notes_for("6.02")

    assert note is not None
    assert note.changes == (
        "주요 예약 엔진의 단계·응답·재시도 진단 로그 개선",
        "개인정보를 가린 실행별 로그 자동 보관",
        "서명 검증·안전 복구를 포함한 자동 업데이트 추가",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v601_patch_notes_are_short_and_exact():
    note = notes_for("6.01")

    assert note is not None
    assert note.changes == (
        "상단 예약 상태 표시 디자인 개선",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v570_patch_notes_are_short_and_exact():
    note = notes_for("5.70")

    assert note is not None
    assert note.changes == (
        "네이버 오픈 시간 오계산 및 Duplicated 처리 수정",
        "둠이스케이프 서버 장애 복구 후 자동 재시도",
        "둠이스케이프 병렬 연결 예열 및 미오픈 시간표 캐시 적용",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v571_patch_notes_are_short_and_exact():
    note = notes_for("5.71")

    assert note is not None
    assert note.changes == (
        "네이버 계정 전환 시 현재 로그인 계정 자동 반영",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v572_patch_notes_are_short_and_exact():
    note = notes_for("5.72")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 트래픽 초과 시 저장된 시간표 자동 사용",
        "둠이스케이프 지점별 전체 테마 시간표 동시 저장 및 조회 안정화",
    )
    assert all(len(change) <= 45 for change in note.changes)
