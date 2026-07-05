# 방탈출 예약 매크로 개발 및 AI 협업 가이드 (Development Guide)

이 문서는 이 프로젝트의 예약 엔진을 수정하거나 새로운 사이트 엔진을 추가할 때 개발자 및 AI 에이전트가 반드시 준수해야 하는 설계 표준, 트랩, 구현 템플릿 및 주의사항을 기술합니다.

---

## 1. 예약 엔진 핵심 설계 규칙

### 🚫 중복 예약 방지 및 즉시 중단 (Double Booking Prevention)
* **상황**: 대부분의 방탈출 예약 사이트는 단시간 내에 동일인 명의의 중복 예약을 허용하거나, 동일 타임슬롯에 복수의 예약 요청이 동시에 도달할 시 중복 결제/예약 오류가 발생할 수 있습니다.
* **해결 원칙**:
  * 한 태스크가 예약을 선점하거나 최종 완료하는 즉시 **`self.stop_event.set()`**을 호출하여 전체 프로세스/태스크에 즉시 정지 신호를 전파해야 합니다.
  * 모든 동기/비동기 루프는 네트워크 요청(조회, 입력값 추출, 제출 등) 직전/직후에 **`self.stop_event.is_set()`** 상태를 확인하고, 참일 경우 로그 스팸 없이 즉각 루프를 탈출(`break` 또는 `return`)해야 합니다.

### 🚫 세션 풀링(Session Pooling) 및 안전한 인덱싱
* **상황**: 다중 프로세스(Process) 및 다중 비동기 태스크(Task) 환경에서 전역 태스크 인덱스(`task_idx`)가 0부터 N까지 증가합니다.
* **해결 원칙**:
  * 세션을 참조할 때 `self.session_pool[task_idx]` 형식을 직접 사용하면 인덱스 초과(`IndexError`)가 발생해 세션 재사용에 실패합니다.
  * 반드시 세션 풀에서 세션을 조회할 때 **`local_idx = task_idx % len(self.session_pool)`** 형태로 안전하게 순환(Modulo) 매핑하여 인덱스 범위를 초과하지 않도록 보호하십시오.

---

## 2. 주요 데이터 처리 트랩 (Traps & Solutions)

### 📞 전화번호 포맷팅 (Phone Formatting)
* **상황**: 사용자가 전화번호를 입력할 때 하이픈 유무가 불확실하며, 사이트마다 요구하는 패킷 포맷이 다릅니다.
  * **신 제로월드**: 하이픈이 필수적입니다. 하이픈 없이 저장되면 사이트 마스크 기능 때문에 마이페이지 일반 조회가 불가능해집니다.
  * **키이스케이프/둠이스케이프**: 전화번호를 3개 필드(`mobile1`, `mobile2`, `mobile3`)로 나누어 전송해야 합니다.
* **해결 원칙**:
  * 입력값에 하이픈이 있을 것이라 가정하고 `split('-')`을 수행하지 마십시오.
  * 항상 숫자만 남겨둔 후 길이에 따라 포맷팅/슬라이싱해야 합니다:
  ```python
  phone_digits = "".join(c for c in phone if c.isdigit())
  if len(phone_digits) == 11:
      # 010-1234-5678 대응
      m1, m2, m3 = phone_digits[0:3], phone_digits[3:7], phone_digits[7:11]
  elif len(phone_digits) == 10:
      # 010-123-4567 대응
      m1, m2, m3 = phone_digits[0:3], phone_digits[3:6], phone_digits[6:10]
  ```
  * 단일 전송이 필요한 경우 `f"{m1}-{m2}-{m3}"`로 복원하여 전송하십시오.

### 🔤 인코딩 설정 (Encoding Type)
* **상황**: 한국 예약 사이트는 UTF-8과 EUC-KR(CP949) 혼용이 매우 심합니다. 잘못 디코딩할 시 테마명이 깨져 감시 대상 매칭에 실패합니다.
* **검증된 정책**:
  * **둠이스케이프, 지구별**: UTF-8 사이트이므로 `decode('utf-8', errors='ignore')`를 적용합니다.
  * **신 제로월드**: 하이브리드 인코딩 대응을 위해 다음과 같이 이중 방어 코드를 구현하십시오:
  ```python
  try:
      html_text = bytes_data.decode('utf-8')
  except UnicodeDecodeError:
      html_text = bytes_data.decode('cp949', errors='ignore')
  ```

---

## 3. 고급 매크로 구현 표준

### 🛡️ 구글 reCAPTCHA v2 처리 규칙
1. **토큰 유효 기한 경고**: 구글 캡차 토큰의 유효 시간은 2분입니다. 예약 2단계 대기 진입 시 로그에 경고(Warning) 등급으로 반드시 노출해야 합니다:
   > `[경고] 구글 캡차 인증은 완료 후 2분이 지나면 자동 초기화(만료)됩니다. 예약 오픈시간 1~2분 전에 완료하시는 것이 가장 안전합니다.`
2. **하이브리드 바이패스 구조**:
   * API 자동 해결(YesCaptcha 등) 태스크를 백그라운드에서 실행함과 동시에, 브라우저 상의 수동 캡차 체크박스 클릭 여부도 실시간 모니터링합니다.
   * 둘 중 **더 먼저 완료되는 토큰**을 주입하여 대기/인증 상태를 즉시 통과시킵니다.

### ⏱️ 미오픈 날짜(정각 감지) 및 동적 ID 치환
1. **조기 제출 방지**: 아직 오픈되지 않은 날짜에 진입하기 위해 가짜 슬롯 값(`9999` 등)을 임시 주입한 경우, 캡차가 해결되더라도 즉시 예약 제출을 시도해선 안 됩니다.
2. **실시간 백엔드 감시**: 0.15초 내외의 빠른 주기로 백엔드 API를 조회하며 실제 시간 슬롯이 활성화되는지 감시합니다.
3. **동적 ID 치환**: 타임슬롯 오픈이 감지되는 즉시, 실제 활성화된 Slot ID를 브라우저 폼 필드(`themeTimeNum` 등)에 자바스크립트로 동적 대체(Inject)한 뒤 예약을 최종 제출합니다.

---

## 4. 새로운 예약 엔진 추가 절차 (Step-by-Step)

새로운 사이트 지원을 추가할 경우 다음 순서대로 클래스를 정의하고 연동해 주십시오:

### 1단계: 엔진 클래스 구현 (`engines/` 디렉토리)
`BaseEngine`을 상속받는 `engines/newsite_engine.py` 파일을 생성합니다.

```python
import asyncio
from engines.base_engine import BaseEngine

class NewSiteEngine(BaseEngine):
    def __init__(self, site_url, log_callback, success_callback=None):
        super().__init__(log_callback, success_callback)
        self.site_url = site_url

    def make_reservation_thread(self, reservation_data):
        # [동기 모드] 단일 스레드 기반 순차 예약 로직 구현
        pass

    async def make_reservation_async_task(self, reservation_data, task_idx):
        # [비동기 모드] 초고속 멀티 비동기 태스크 예약 로직 구현
        # 1. stop_event.is_set() 확인
        # 2. session_pool에서 Modulo 연산으로 세션 추출
        # 3. HTTP 요청 및 응답 인코딩 처리
        # 4. 성공 시 stop_event.set() 및 success_callback() 호출
        pass

    async def pre_fetch_sessions_async(self, num_sessions, reservation_data):
        # [선택] 사전 세션 연결 및 CSRF 토큰 풀링
        pass
```

### 2단계: 프로세스 런타임에 등록 (`engines/base_engine.py`)
`child_process_run` 함수 내의 임포트 영역과 `classes` 딕셔너리에 새 엔진을 추가합니다.
```python
        from engines.zeroworld_shin_engine import ZeroWorldShinEngine
        from engines.zeroworld_gu_engine import ZeroWorldGuEngine
        from engines.jigubyeol_engine import JigubyeolEngine
        from engines.keyescape_engine import KeyescapeEngine
        from engines.doomescape_engine import DoomEscapeEngine
        from engines.newsite_engine import NewSiteEngine  # 예시
```

### 3단계: UI 레이어 연동 (`ui/reservation_form.py`)
새 사이트에 대한 지점명, 테마 목록, 사이트 맵핑 구조를 UI 폼에 바인딩합니다.
* 사용자가 새 웹사이트를 추가할 때 `config.json`과 사이트 파서(`site_parser.py`)를 통해 지점 및 테마 데이터가 유연하게 캐싱되도록 구성되어 있는지 검토하십시오.
