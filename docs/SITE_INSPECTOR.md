# Pengucro Site Inspector

예약 사이트 엔진 개발을 위한 독립 분석 도구입니다. URL 하나를 입력하면 전용
Chrome에서 같은 사이트의 화면과 컨트롤을 제한된 범위로 탐색하고, DOM·스크린샷·
네트워크 요청·API 후보를 로컬 보고서로 저장합니다.

## 실행

```powershell
py -3 tools\site_inspector_app.py
```

CLI에서도 실행할 수 있습니다.

```powershell
py -3 tools\site_inspector_app.py --url "https://example.com" `
  --max-pages 12 --max-states 36
```

`--max-pages`는 방문할 문서 경로 수, `--max-states`는 클릭·달력 등으로 생기는
화면 상태 수입니다. 기본 설정은 같은 등록 도메인의 형제 서브도메인도 분석합니다.
현재 호스트만 보려면 `--same-host-only`를 지정합니다.

미오픈 날짜 DOM은 기본적으로 오늘 기준 `+30일`, `+90일`, `+180일`을 탐색합니다.
CLI에서는 다음처럼 변경할 수 있습니다.

```powershell
py -3 tools\site_inspector_app.py --url "https://example.com" `
  --date-probe-offsets "30,90,180,365" --max-date-probes 16
```

결과 폴더에는 다음 파일이 생성됩니다.

- `report.md`: 사람이 읽는 요약 보고서
- `inspection.json`: 전체 화면·동작·네트워크 관측 결과
- `endpoints.json`: 중복 제거한 API 후보
- `engine_spec.json`: Pengucro 엔진 구현 검토 항목
- `engine_blueprint.json`: 엔진 구현 순서와 카탈로그·조회·제출 후보
- `site_structure.json`: 발견 경로, DOM 선택자, 폼, 상태 전이 그래프
- `related_origins.json`: 같은 등록 도메인의 티켓·여행 등 형제 서브도메인 구조
- `crawl_frontier.json`: 설정 한도 때문에 아직 방문하지 않은 동일 사이트 GET 경로
- `request_schemas.json`: 요청·응답 필드의 값 없는 타입 구조
- `generated_engine_draft.py`: 실제 제출이 비어 있는 엔진 골격
- `pages/`, `screenshots/`: 각 화면 상태의 증거
- `date-probes/`: 미래·미오픈 날짜로 조회한 DOM 증거

## 일반 예약 사이트 처리

- 사이트맵 인덱스를 재귀적으로 읽고 `shop`, `store`, `product`, `reservation` 경로를
  우선 표본화합니다. 방문 한도를 넘은 경로도 구조 인벤토리에 남깁니다.
- 쿼리 값만 다른 동일 경로는 하나로 묶되 `go`, `action`처럼 화면을 바꾸는 값은
  별도 경로로 유지합니다.
- React/Next.js 같은 SPA의 해시·JavaScript 이동과 형제 서브도메인 전환을 따라갑니다.
- 정적 HTML이 빈 SPA 껍데기이면 전용 Chrome으로 렌더링한 DOM과 조회 API를 다시
  캡처합니다. 상세 화면의 첫 `예약하기`는 입력 준비 화면까지만 열 수 있습니다.
- 날짜형·텍스트형·읽기 전용 날짜 입력, 왕복/입퇴실 날짜 쌍, 다음 달 컨트롤을
  구분합니다. 미래 날짜를 조회할 때 기존 기간 길이도 보존합니다.
- 보고서에는 쿠키·토큰뿐 아니라 암호화 파라미터와 긴 고엔트로피 값도 원문 대신
  마스킹된 구조만 저장합니다.

## 안전 경계

- GET/HEAD/OPTIONS와 명확한 조회성 POST만 허용합니다.
- 예약·결제·주문·취소·삭제·로그아웃 요청은 전송 전에 차단합니다.
- 결제·예약확정·예약하기·예매하기 등 최종 버튼은 자동으로 누르지 않습니다.
- 예약불가·매진 슬롯, 전체메뉴·슬라이드·TOP, 다시 보지 않기 같은 분석과
  무관한 동작도 건너뜁니다.
- 이름·연락처·이메일·주소·생년월일·비밀번호·쿠키·Authorization·토큰·
  카드정보는 보고서에서 마스킹합니다.
- CAPTCHA, Cloudflare 사람 확인, OTP는 우회하지 않습니다. 열린 Chrome에서
  사용자가 정상적으로 완료하면 분석이 이어집니다.
- 분석용 Chrome은 로그인 상태와 연속 실행 속도를 위해 종료하지 않고 재사용합니다.
  더 이상 필요하지 않으면 해당 Chrome 창을 직접 닫을 수 있습니다.
- 요청 기록과 차단 정책은 해당 분석 실행이 직접 만든 탭에만 적용됩니다. 같은 Chrome의
  기존 사용자 탭이나 별도 분석 탭의 네트워크는 보고서에 섞지 않습니다.
- 사이트가 개발자도구 연결을 차단하면 우회하지 않고 공개 HTML의 폼·선택값·
  엔드포인트만 정적으로 보조 분석하며 보고서에 제한 사유를 남깁니다.
- 전체 사이트 구조는 같은 등록 도메인의 형제 서브도메인까지 인벤토리화하지만, 실제 방문은
  설정한 화면 수와 탐색 깊이 안에서만 수행합니다. `site_structure.json`의
  `unvisited_routes`를 다음 분석 시작점으로 사용할 수 있습니다.
- 생성된 엔진 골격은 분석 초안입니다. 실제 사이트에서 성공 판정과 예약번호 복구를
  검증하기 전에는 배포하거나 실제 예약에 사용하지 않습니다.
