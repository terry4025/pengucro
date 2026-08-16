"""Hard-coded release notes shown inside the desktop application.

Keep the newest release first.  These notes are intentionally bundled with the
executable so the dialog works offline and always describes that exact build.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatchNote:
    version: str
    changes: tuple[str, ...]


PATCH_NOTES: tuple[PatchNote, ...] = (
    PatchNote(
        version="6.20",
        changes=(
            "네이버 오픈 시각 예약을 Chrome 내부 타이머로 직접 제출하도록 개선",
            "네이버 실제 예약 응답 지연을 다음 예약 시점 보정에 반영",
            "네이버 오픈 직전 Python·화면 호출 지연을 제거해 선점 속도 개선",
        ),
    ),
    PatchNote(
        version="6.19",
        changes=(
            "CGV 미오픈 날짜에 최근 공개 시간표를 대기 기준으로 선택 가능",
            "CGV 선공개 회차에 최근 실제 좌석 배치를 연결하도록 개선",
            "CGV 목표 날짜 회차 공개 즉시 좌석 감시로 이어지도록 개선",
        ),
    ),
    PatchNote(
        version="6.18",
        changes=(
            "CGV 좌석 개방 감지와 동시에 가격 확인·임시선점을 연속 처리하도록 개선",
            "CGV 선점 성공 전 화면 처리와 중간 대기 제거",
            "CGV 가격 확인 경합 후 대기 없이 고속 감시 자동 재개",
        ),
    ),
    PatchNote(
        version="6.17",
        changes=(
            "CGV 좌석 감시 주기를 단축한 고속 API 교차 감시 추가",
            "CGV 좌석 개방 감지 후 화면 처리보다 임시선점을 먼저 실행하도록 개선",
            "CGV 선점 경합 시 페이지 갱신 없이 즉시 재감시하도록 개선",
        ),
    ),
    PatchNote(
        version="6.16",
        changes=(
            "CGV 실제 좌석 좌표와 통로를 반영한 좌석도 표시 개선",
            "CGV 대형 상영관 좌석도 자동 확대와 가로 이동 추가",
            "CGV 명당 유형 선택 시 인원수에 맞는 연속 좌석 자동 선택 추가",
        ),
    ),
    PatchNote(
        version="6.15",
        changes=(
            "CGV 로그인 완료 직후 좌석 조회 페이지가 종료되는 문제 수정",
            "CGV 만료된 로그인 정보 오인 방지와 페이지 이동 자동 복구 추가",
        ),
    ),
    PatchNote(
        version="6.14",
        changes=(
            "CGV 명당 가이드의 한글 글꼴과 글자 가독성 개선",
            "CGV 명당 안내 제목·요약·상세 정보의 표시 크기와 선명도 개선",
        ),
    ),
    PatchNote(
        version="6.13",
        changes=(
            "CGV 2명 이상 예매의 좌석 우선순위를 같은 열의 연속 좌석으로 제한",
            "CGV 분리 좌석 설정이 선점 대상으로 처리되는 문제 방지",
        ),
    ),
    PatchNote(
        version="6.12",
        changes=(
            "CGV 좌석도에 상영관별 명당 추천 표시 추가",
            "CGV 용산아이파크몰 IMAX 전용 좌석 가이드 추가",
            "CGV 로그인 안내 후 좌석 조회 창이 닫히는 문제 수정",
        ),
    ),
    PatchNote(
        version="6.11",
        changes=(
            "CGV 지역·지점·영화·상영관·회차 실제 데이터 선택 추가",
            "CGV 미오픈·매진 회차의 좌석 우선순위와 취소표 감시 추가",
            "CGV 좌석 조회·가격 확인·임시선점을 공식 API 우선 방식으로 고속화",
            "CGV 화면 연결 실패 시 브라우저 방식 자동 전환 추가",
            "CGV 회원 세션 재사용과 비회원 문자 인증 예매 추가",
            "CGV 실측 결과에 따른 동시 조회 4개 상한과 자동 감속 추가",
        ),
    ),
    PatchNote(
        version="6.10",
        changes=(
            "CGV 지점·영화·상영관·좌석 우선순위 예약 감시 추가",
            "CGV 미오픈 회차 감시와 좌석 임시선점 후 결제 직전 연결 추가",
            "CGV 동시 조회 3개 상한과 접근 제한 시 자동 감속 추가",
        ),
    ),
    PatchNote(
        version="6.09",
        changes=(
            "단편선 결제 화면의 예약자 이름·연락처 자동 입력 수정",
            "단편선 예약자 정보 입력 완료 검증 추가",
        ),
    ),
    PatchNote(
        version="6.08",
        changes=(
            "단편선 결제 안내문을 예약 성공으로 오인하는 문제 수정",
            "단편선 내부 주문값의 예약번호 오표시 방지",
            "단편선 실제 예약 접수와 완료 화면 확인 강화",
            "단편선 예약 성공 페이지 자동 유지",
        ),
    ),
    PatchNote(
        version="6.07",
        changes=(
            "단편선 무통장입금과 필수 동의 선택 오류 수정",
            "단편선 결제 화면 초기화 완료 후 자동 입력",
        ),
    ),
    PatchNote(
        version="6.06",
        changes=(
            "단편선 무통장입금·필수 약관·최종 결제 자동 완료",
            "단편선 실제 예약 완료 확인 후 성공 처리",
            "단편선 오류 화면의 예약 성공 오인 방지",
        ),
    ),
    PatchNote(
        version="6.05",
        changes=(
            "키이스케이프 금·토·일 미오픈 시간표 빠른 제출",
            "키이스케이프 서버시간 측정 정밀도 개선",
            "키이스케이프 다중 실행 간 서버시간 측정 공유",
            "키이스케이프 캡차 발급 속도 학습 및 시점 자동 조절",
            "자동 업데이트 최신 버전 확인 지연 개선",
        ),
    ),
    PatchNote(
        version="6.04",
        changes=(
            "앱과 로딩 화면의 버전 표기 간소화",
            "본문 상단의 중복 프로그램 제목 제거",
        ),
    ),
    PatchNote(
        version="6.03",
        changes=(
            "둠이스케이프 미오픈 날짜의 전체 테마 시간표 자동 탐색",
            "둠이스케이프 평일·주말 시간표 오적용 방지",
        ),
    ),
    PatchNote(
        version="6.02",
        changes=(
            "주요 예약 엔진의 단계·응답·재시도 진단 로그 개선",
            "개인정보를 가린 실행별 로그 자동 보관",
            "서명 검증·안전 복구를 포함한 자동 업데이트 추가",
        ),
    ),
    PatchNote(
        version="6.01",
        changes=(
            "상단 예약 상태 표시 디자인 개선",
        ),
    ),
    PatchNote(
        version="6.0",
        changes=(
            "키이스케이프 오픈 직후 예약 제출 속도 개선 및 안전한 자동 전환",
            "키이스케이프 다중 실행 시 시간표 조회 중복 감소 및 안정화",
            "키이스케이프 예약 타이밍 로그 정밀화 및 캡차 준비 안정화",
        ),
    ),
    PatchNote(
        version="5.72",
        changes=(
            "둠이스케이프 트래픽 초과 시 저장된 시간표 자동 사용",
            "둠이스케이프 지점별 전체 테마 시간표 동시 저장 및 조회 안정화",
        ),
    ),
    PatchNote(
        version="5.71",
        changes=(
            "네이버 계정 전환 시 현재 로그인 계정 자동 반영",
        ),
    ),
    PatchNote(
        version="5.70",
        changes=(
            "네이버 오픈 시간 오계산 및 Duplicated 처리 수정",
            "둠이스케이프 서버 장애 복구 후 자동 재시도",
            "둠이스케이프 병렬 연결 예열 및 미오픈 시간표 캐시 적용",
        ),
    ),
)


def notes_for(version: str) -> PatchNote | None:
    return next((note for note in PATCH_NOTES if note.version == version), None)
