# 방탈출 펭크로

현재 버전: **v5.27**

v5.27에서는 신뢰도를 신규 커스텀 사이트 등록 시에만 보여주는 참고 지표로 변경했습니다. 기존 사이트 갱신은 확정된 엔진을 그대로 사용하며, 신규 사이트는 점수순 후보 중 실제 카탈로그 조회 검증을 통과한 엔진으로 자동 등록합니다.

v5.26에서는 사이트 정보 자동 갱신 중 일시적인 DNS·네트워크 장애가 발생하면 한 번 재시도하고, 계속 실패해도 기존 정상 카탈로그를 유지하도록 개선했습니다.

v5.25에서는 예약 중지·완료 후 GUI 초기화가 반복되어 화면이 깜빡이던 문제와 상태 타이머 중복 가능성을 수정했습니다.

Windows에서 여러 방탈출 예약 페이지를 한 화면에서 감시하고 예약하는 데스크톱 도구입니다.

## 지원 사이트

- 제로월드 신 사이트: 김포본점, 강남점, 홍대점, 다이브 건대점
- 지구별방탈출
- 키이스케이프
- 둠이스케이프
- 네이버 예약 URL로 등록한 커스텀 사이트

구 제로월드 사이트는 더 이상 제품 경로에서 사용하지 않습니다.

## 설치 및 실행

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
py app.py
```

네이버·키이스케이프 자동화는 설치된 Chrome 또는 Edge를 우선 사용하고, 없으면 Playwright Chromium을 사용합니다.

## 개인정보와 보안

- 설정과 예약 기록은 `%LOCALAPPDATA%\Pengucro`에 저장됩니다.
- 이름, 전화번호, 네이버 로그인 쿠키, YesCaptcha API 키는 Windows DPAPI로 현재 Windows 사용자에게 암호화됩니다.
- 이름과 전화번호 저장은 GUI의 `고급 설정`에서 끌 수 있습니다.
- 이전 실행 폴더의 `config.json`, `custom_sites.json`은 호환을 위해 새 저장소로 복사됩니다.
- 검증된 사이트 카탈로그는 `%LOCALAPPDATA%\Pengucro\site_catalog.json`에 원자적으로 저장되고 이전 정상본은 `site_catalog.backup.json`으로 보존됩니다.
- `naver_cookies.json`은 최초 사용 시 암호화 저장소로 이전한 뒤 평문 파일을 제거합니다.
- 과거 소스에 포함됐던 YesCaptcha 키는 노출된 것으로 간주하고 반드시 서비스에서 폐기·재발급해야 합니다.

환경 변수 `PENGUCRO_YESCAPTCHA_API_KEY`를 사용하면 GUI에 저장된 API 키보다 우선합니다.

## GUI 사용 팁

- 날짜 달력과 제로월드 실시간 시간 조회 버튼으로 예약 값을 선택할 수 있습니다.
- `Ctrl+Enter`: 예약 시작/중지, `Esc`: 실행 중지, `Ctrl+L`: 로그 지우기
- 동시 시도 수, 개인정보 기억, 캡차 키는 `고급 설정`에서 관리합니다.
- `시작 시 사이트 정보 자동 갱신`은 마지막 정상 갱신 후 12시간이 지난 사이트만 조회합니다.
- `현재 사이트 갱신`은 선택한 사이트의 신규 지점·테마와 이름 변경을 즉시 반영합니다. 삭제와 ID 변경은 변경 배지에서 사용자가 확인해야 적용됩니다.
- 커스텀 사이트 등록 시 알려진 예약 엔진의 지문과 카탈로그 조회 결과를 검사하며, 사용자가 추천 엔진을 확인해야 저장됩니다.

## 테스트

```powershell
pip install -r requirements-dev.txt
pytest -q
py verify_ui.py
```

테스트는 실제 예약 제출 API를 호출하지 않습니다. 라이브 사이트 검증은 달력·테마·시간 조회까지만 수행해야 합니다.

## 빌드

```powershell
pyinstaller --clean 방탈출펭크로.spec
```

배포 전 대상 PC 또는 빌드 환경에서 Playwright 브라우저 설치 여부를 확인하세요.

## 구조

- `ui/`: 화면과 사용자 입력
- `pengucro/`: 예약 요청·결과 모델, 보안 저장소
- `engines/registry.py`: 사이트별 엔진 생성
- `engines/base_engine.py`: 공통 시작·중지·로그·성공 생명주기
- `engines/zeroworld_catalog.py`: 신 제로월드 테마·시간 파서
- `engines/catalog_providers.py`: 엔진 지문 감지와 사이트별 지점·테마 탐색
- `pengucro/catalog.py`: TTL, 변경 비교, 안전 캐시 및 검토 대기 변경 관리
- `engines/*_engine.py`: 사이트별 예약 어댑터
