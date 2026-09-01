from pengucro import __version__
from pengucro.patch_notes import PATCH_NOTES, notes_for


def test_current_build_has_newest_patch_note():
    assert PATCH_NOTES
    assert PATCH_NOTES[0].version == __version__
    assert notes_for(__version__) is PATCH_NOTES[0]
    assert all(not str(note.version).strip().lower().startswith("v") for note in PATCH_NOTES)


def test_v673_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.73")

    assert note is not None
    assert note.changes == (
        "네이버 상품·결제 방식별 선점 제출 시점 자동 보정",
        "네이버 오픈 경계의 서버 도착 시점과 재시도 정밀화",
        "네이버 불명확 응답의 예약내역 대조와 예약번호 복구 강화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v672_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.72")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 주문 생성 후 결제 준비 화면을 최대 3분간 자동 재확인",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v671_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.71")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 주문 생성 후 해당 프로그램의 불필요한 시간표 조회 중단",
        "둠이스케이프 서버 지연 시 생성된 주문의 결제 준비 확인 강화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v669_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.69")

    assert note is not None
    assert note.changes == (
        "네이버 동일 연결 서버 시각 기준으로 오픈 순간 제출 정밀도 개선",
        "네이버 미오픈 응답 재시도를 실제 오픈 경계에 맞춰 선점 기회 강화",
        "네이버 제출 결과 불명확 시 예약내역 확인 시간을 늘려 임시 선점 복구 강화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v668_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.68")

    assert note is not None
    assert note.changes == (
        "네이버 브라우저 실제 통신 지연 기준으로 선점 제출 시각 정밀 보정",
        "네이버 명시적 미오픈 응답에만 브라우저 내부 즉시 재시도",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v667_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.67")

    assert note is not None
    assert note.changes == (
        "키이스케이프 전체 공개 시간표 자동 저장으로 빈 캐시 빠른 제출 지원",
        "검증 시간표를 배포판에 포함해 신규 사용자 슬롯 ID 연동",
        "여러 키이스케이프 프로그램의 시각·슬롯 조회를 창별로 격리",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v657_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.57")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 빠른 조회와 지연 복구 응답을 함께 감시해 선점 재개 강화",
        "둠이스케이프 목표 날짜·인원별 제출값 검증 강화",
        "지구별 최종 예약 중복 제출 방지",
        "제로월드 제출·결제 완료 응답 대기 안정화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v656_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.56")

    assert note is not None
    assert note.changes == (
        "둠이스케이프 서버 장애 중에도 첫 정상 응답 즉시 선점 재개",
        "둠이스케이프 오픈 전 제출값 사전 준비로 주문 생성 지연 단축",
        "둠이스케이프 목표 날짜·회차 검증 강화로 잘못된 제출 방지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v653_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.53")

    assert note is not None
    assert note.changes == (
        "지구별·제로월드 연속 슬롯 조회 시 연결 재사용과 분산 스케줄링으로 탐색 가속",
        "HTTP 오류 및 과부하 응답 발생 시 자동 지연 복구로 요청 안정화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v652_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.52")

    assert note is not None
    assert note.changes == (
        "단편선 오픈 직전 주문 필드 사전 준비로 오픈 즉시 빠른 주문 생성",
        "지구별 슬롯 미오픈 상태에서 시간 선택을 미리 준비해 오픈 즉시 선점 속도 개선",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v651_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.51")

    assert note is not None
    assert note.changes == (
        "키이스케이프 전체 지점·테마의 공개 시간표 일괄 저장 기능 추가",
        "키이스케이프 요일별 시간표 저장 진행률과 미공개 상태 안내",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v650_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.50")

    assert note is not None
    assert note.changes == (
        "키이스케이프 공개 시간표를 요일별로 자동 보관해 최근 동일 요일 슬롯 빠른 제출",
        "이전 버전에서 저장한 키이스케이프 시간표를 업데이트 후에도 자동 연동",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v649_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.49")

    assert note is not None
    assert note.changes == (
        "CGV 좌석 판매 전 회차의 조기 진입 차단",
        "CGV 유사 제목 영화 번호 오인식 방지",
        "CGV 상영 날짜 게시 후 1초 감시 유지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v648_patch_notes_are_short_and_user_friendly():
    note = notes_for("6.48")

    assert note is not None
    assert note.changes == (
        "지구별 예약의 동시 시도 속도 개선",
        "지구별 인증 만료 시 자동 갱신 후 재시도 안정화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v647_patch_notes_are_short_and_exact():
    note = notes_for("6.47")

    assert note is not None
    assert note.changes == (
        "CGV 게시됐지만 좌석 판매 전인 회차를 후순위로 자동 대기",
        "CGV 영화 번호 미확보 시 예매 목록에서 자동 조회",
        "CGV 날짜 게시 후 오픈까지 중간 속도 감시 유지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v646_patch_notes_are_short_and_exact():
    note = notes_for("6.46")

    assert note is not None
    assert note.changes == (
        "CGV 미오픈 회차 판매 잠금 해제 즉시 집중 감시",
        "CGV 공개 회차 번호 변경 시 최신 회차 우선 선점",
        "CGV 영화 번호 우선 판별로 동명 영화 오선택 방지",
        "CGV 오픈 직후 화면 지연 시 자동 재조회 및 재시도",
        "CGV 장시간 감시 중 절전 방지와 연결·로그인 이상 경보",
        "CGV 만료 세션 재인증 및 감시 장애 자동 복구 강화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v645_patch_notes_are_short_and_exact():
    note = notes_for("6.45")

    assert note is not None
    assert note.changes == (
        "CGV 후순위 시간대 좌석을 화면 전환 전에 즉시 선점하도록 개선",
        "CGV 좌석 선점 경합 시 다음 좌석·시간 우선순위로 자동 전환",
        "CGV 오픈 집중 감시에서 응답 지연으로 감속된 동시 조회 수 자동 복구",
        "CGV 미오픈 감시 시작 시 참고 회차의 영화 번호와 실제 날짜 즉시 재사용",
        "CGV 공식 IMAX 지점 목록 자동 반영으로 지점 누락 및 오분류 수정",
        "CGV 고속 API 개발자 테스트 완료 후 사용한 임시선점 자동 해제",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v644_patch_notes_are_short_and_exact():
    note = notes_for("6.44")

    assert note is not None
    assert note.changes == (
        "CGV 미오픈 장시간 감시 안정성 및 서버 차단 방지 강화",
        "상영 날짜 신규 등록 감지 시 즉시 초고속 집중 감시로 전환",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v643_patch_notes_are_short_and_exact():
    note = notes_for("6.43")

    assert note is not None
    assert note.changes == (
        "CGV 미오픈 감시 중 단계별(영화/상영관/시간 등) 판정 퍼널 로그 추가",
        "미오픈 회차 미진입 원인 및 실제 시간 매핑 세부 사유 가시화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v642_patch_notes_are_short_and_exact():
    note = notes_for("6.42")

    assert note is not None
    assert note.changes == (
        "CGV 미오픈 날짜 참고시간과 실제 오픈 시간(최대 90분 오차) 자동 매핑 지원",
        "CGV 상영관·포맷 표시 변경 대응 및 미완성 부분 공개 회차 선택 방지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v641_patch_notes_are_short_and_exact():
    note = notes_for("6.41")

    assert note is not None
    assert note.changes == (
        "네이버페이 보안 키패드 4행 2열 및 숫자 4·7 인식 정확도 개선",
        "키패드 인접 버튼 텍스트 노이즈 배제 및 중심 글리프 매칭 강화",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v628_patch_notes_are_short_and_exact():
    note = notes_for("6.28")

    assert note is not None
    assert note.changes == (
        "CGV 좌석 선택창 및 좌석도 표시 영역 세로 크기 확대",
        "CGV 명당 가이드 카드 숨김 및 좌석도 시인성 개선",
        "CGV 명당 자동 선택 및 수동 좌석 우선순위 기능 유지",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v627_patch_notes_are_short_and_exact():
    note = notes_for("6.27")

    assert note is not None
    assert note.changes == (
        "버전 업데이트 후 이름·전화번호·인원수 설정 유지 안정화",
        "CGV 기존 로그인 탭과 세션 재사용 및 브라우저 단절 복구 개선",
        "CGV 미오픈 날짜에서 좌석도 없이 수동 좌석 우선순위 입력 지원",
        "CGV 수동 좌석 UI 배치 오류와 인원수 변경 회귀 오류 수정",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v626_patch_notes_are_short_and_exact():
    note = notes_for("6.26")

    assert note is not None
    assert note.changes == (
        "CGV 미오픈 회차 감시 주기를 최대 1~2초로 단축",
        "CGV 좌석 선택 후 결제 전 확인 안내창 자동 처리",
        "CGV 결제수단 진입 지연 및 중복 클릭 방지 개선",
    )
    assert all(len(change) <= 45 for change in note.changes)


def test_v625_patch_notes_are_short_and_exact():
    note = notes_for("6.25")

    assert note is not None
    assert note.changes == (
        "키이스케이프 최종 서버시각 정밀도 보존 및 보정 로그 추가",
        "키이스케이프 Chrome 예약 endpoint 연결 예열 강화",
        "키이스케이프 예약 POST의 DNS·TLS·첫응답 지연 측정 추가",
        "키이스케이프 1~3페이지에 동일 최적화 런타임 적용",
    )
    assert all(len(change) <= 45 for change in note.changes)


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
