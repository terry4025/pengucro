# v6.76 단편선 패치 검증

## Windows 배포 준비 검증 (2026-09-05)

- 제공 패치를 기준 소스 `a15b599`에 적용한 별도 `codex/release-v676` 작업 폴더에서 검증했다.
- Windows 전체 pytest: **1,127 passed**. 기존 릴리스 계약 테스트의 6.75 기대값을 6.76으로 갱신했다.
- `verify_ui.py`: **9 passed**, 현재 버전 패치노트 버튼과 본문 렌더링 포함.
- `MainWindow._start_booking → EngineRegistry → DpsnnnEngine.start_reservation → BaseEngine` 실제 디스패치 및 워커 실행을 모의 네트워크로 검증했다.
- 정상 접수 시 예약번호 저장, 응답 유실 시 한 번만 제출하고 재시작 재주문 차단을 확인했다.
- 일괄 예약 창의 버전·시작 버튼 렌더링과 사용자가 시작하기 전 엔진이 실행되지 않는 것을 확인했다.
- 개인 예약 JSON/ZIP은 소스 커밋과 공개 EXE에 포함하지 않는다.
- 아래 Linux 검증 기록은 최초 패치 작성 당시의 이력이다. 실제 자정 전환·실예약은 여전히 미검증이다.
- EXE 빌드·공개 배포 결과 및 빌드 소스 SHA는 GitHub 릴리스 검증 기록에서 확인한다.

기준: `a15b59983d94b2dc023b4fbb5b7d9e9f443886c9`에서 분기한 소스.
Linux/Python 3.12에서 시행했다. 릴리스 Windows EXE 검증 결과가 아니다.

## 완료

```text
python -m pytest tests/test_dpsnnn_engine.py tests/test_dpsnnn_preopen_runtime.py tests/test_patch_notes.py tests/test_update_manifest.py -q
114 passed
git diff --check: 통과
변경 Python 모듈 compileall: 통과
```

- 공개 강남 달력: 9월 12일 비활성, 9월 11일 선택 상태, 표시 날짜 및
  Shadow DOM 예약 카드 선택자 확인. 실제 예약 주문은 생성하지 않았다.
- 모의 검증: 네 목표 테마/시간별 빈 목록→게시, 동일 시간 마감/가능 혼재,
  초기 접속 타임아웃 회복, 신규 달력에서 확인된 슬롯 우선 사용.
- 모의 검증: 주문 응답 유실 시 재전송 없음, 로컬 원자적 중복방지와 재시작 차단,
  시작 재진입 차단, 한 예약 실패 후 나머지 세 예약 완료.
- 모의 검증: 무통장 최종 사전 확인의 HTTP 오류/빈 응답 거절, 올바른 접수 화면과
  실제 예약번호 필요, 타 도메인/실패 본문의 성공 오인 차단, 가격 포함 제출 버튼 탐색.
- 버전 6.76, sequence 6760001, 실행파일명, 최신 패치노트 일치.

## 미완료와 한계

- 전체 테스트 수집과 `verify_ui.py`는 Linux에 Windows `winsound` 모듈이 없어 중단됐다.
- 해당 수집 오류 파일을 제외한 넓은 회귀 검사도 환경의 네트워크 승인 취소로
  완료하지 못했다. 전체 회귀 통과로 판정하지 않는다.
- Windows GUI의 패치노트 버튼·일괄 예약 창 렌더링, Windows EXE 빌드와 smoke,
  서명 manifest 및 자동 업데이트 배포는 수행하지 않았다.
- 실제 자정 미오픈→오픈 전환, 최종 실예약·예약번호 발급·알림톡, 입금 확인은
  수행하지 않았다. 실예약 성공률/속도 수치는 측정하지 않았다.
- 전달받은 Windows 캡처 디렉터리의 원본은 이 환경에 없다.

Windows 릴리스 PC에서 `docs/AUTO_UPDATE_RELEASE.md`의 남은 검증을 완료한 뒤
버전 EXE를 먼저, 서명된 `latest.json`을 마지막에 배포해야 한다.
