import hashlib
import logging
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_SOFT_ID = "26273"  # YesCaptcha SoftID
POLL_INTERVAL_SECONDS = 3.0

class YesCaptchaClient:
    def __init__(self, client_key: str, soft_id: str = DEFAULT_SOFT_ID):
        self.client_key = client_key.strip() if client_key else ""
        self.soft_id = soft_id.strip() if soft_id else DEFAULT_SOFT_ID
        self.base_url = "https://api.yescaptcha.com"

    def is_valid(self) -> bool:
        return bool(self.client_key)

    @property
    def key_fingerprint(self) -> str:
        """Non-reversible identifier used to confirm which configured key ran."""
        if not self.client_key:
            return "없음"
        return hashlib.sha256(self.client_key.encode("utf-8")).hexdigest()[:8]

    def _soft_id_value(self):
        """The API defines softID as an integer; omit malformed values."""
        try:
            value = int(self.soft_id)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _response_json(response):
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("JSON 객체가 아닌 응답을 받았습니다.")
        return data

    def get_balance(self) -> tuple[bool, float, str]:
        """
        Check balance/points of the Client Key.
        Returns: (success: bool, balance_or_score: float, message: str)
        """
        if not self.is_valid():
            return False, 0.0, "API Client Key가 입력되지 않았습니다."

        url = f"{self.base_url}/getBalance"
        payload = {
            "clientKey": self.client_key
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            data = self._response_json(r)
            if data.get("errorId") == 0:
                balance = float(data.get("balance", 0))
                return True, balance, f"잔액/포인트: {balance}"
            else:
                err_desc = data.get("errorDescription", "알 수 없는 오류")
                return False, 0.0, f"오류 ({data.get('errorCode')}): {err_desc}"
        except Exception as e:
            return False, 0.0, f"네트워크 통신 오류: {e}"

    def create_recaptcha_v2_task(self, website_url: str, website_key: str, is_invisible: bool = False) -> tuple[bool, str, str]:
        """
        Create a reCAPTCHA v2 task on YesCaptcha with softID.
        Returns: (success: bool, task_id: str, error_message: str)
        """
        if not self.is_valid():
            return False, "", "API Client Key가 입력되지 않았습니다."

        url = f"{self.base_url}/createTask"
        task_type = "NoCaptchaTaskProxyless"
        
        task = {
            "type": task_type,
            "websiteURL": website_url,
            "websiteKey": website_key,
            "isInvisible": bool(is_invisible),
        }
        payload = {
            "clientKey": self.client_key,
            "task": task,
        }
        soft_id = self._soft_id_value()
        if soft_id is not None:
            payload["softID"] = soft_id

        try:
            r = requests.post(url, json=payload, timeout=15)
            data = self._response_json(r)
            if data.get("errorId") == 0:
                task_id = str(data.get("taskId", ""))
                if not task_id:
                    return False, "", "YesCaptcha가 성공 응답을 보냈지만 Task ID가 비어 있습니다."
                return True, task_id, ""
            else:
                err_desc = data.get("errorDescription", "태스크 생성 실패")
                return False, "", f"YesCaptcha 오류 ({data.get('errorCode')}): {err_desc}"
        except Exception as e:
            return False, "", f"YesCaptcha 태스크 생성 요청 실패: {e}"

    def poll_result(self, task_id: str, timeout_seconds: int = 120, stop_event=None) -> tuple[bool, str, str]:
        """
        Poll task result until solved or timeout.
        Returns: (success: bool, token: str, error_message: str)
        """
        if not task_id:
            return False, "", "유효하지 않은 Task ID입니다."

        url = f"{self.base_url}/getTaskResult"
        payload = {
            "clientKey": self.client_key,
            "taskId": task_id
        }

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if stop_event and stop_event.is_set():
                return False, "", "사용자에 의해 중지되었습니다."

            try:
                r = requests.post(url, json=payload, timeout=10)
                data = self._response_json(r)
                error_id = data.get("errorId", -1)
                
                if error_id == 0:
                    status = data.get("status")
                    if status == "ready":
                        solution = data.get("solution", {})
                        g_recaptcha_response = solution.get("gRecaptchaResponse", "")
                        if g_recaptcha_response:
                            return True, g_recaptcha_response, ""
                        else:
                            return False, "", "해결되었으나 토큰이 비어있습니다."
                    elif status == "processing":
                        time.sleep(POLL_INTERVAL_SECONDS)
                        continue
                    else:
                        return False, "", f"알 수 없는 태스크 상태: {status}"
                else:
                    err_desc = data.get("errorDescription", "태스크 결과 조회 실패")
                    return False, "", f"YesCaptcha 오류 ({data.get('errorCode')}): {err_desc}"

            except Exception as e:
                logger.warning(f"YesCaptcha 결과 폴링 중 예외 발생: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)

        return False, "", "YesCaptcha 해결 시간 초과 (Timeout)"
