"""Bounded assistance for the observed CGV member-login form, not authentication proof."""
from __future__ import annotations

import base64
import io
import re
from urllib.parse import urlparse

from PIL import Image, ImageOps

from engines.zeroworld_captcha import _recognize


_READ_FORM = r"""() => {
  const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  if ([...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')].some(visible)) return null;
  const id = document.querySelector('#loginInput1');
  const password = document.querySelector('#loginInput2');
  const captcha = document.querySelector('#loginInput3');
  const form = captcha && captcha.closest('form');
  const canvas = form && form.querySelector('canvas[aria-label="캡차 이미지"]');
  const submit = form && form.querySelector('button[type="submit"]');
  if (!form || ![id, password, captcha, canvas, submit].every(visible) ||
      id.form !== form || password.form !== form || password.type !== 'password' ||
      captcha.name !== 'captcha' || submit.disabled ||
      canvas.width !== 240 || canvas.height !== 95) return null;
  return {image: canvas.toDataURL('image/png'),
    credentialsReady: !!id.value.trim() && !!password.value,
    editingCredentials: [id, password].includes(document.activeElement),
    captchaFilled: !!captcha.value};
}"""

_SUBMIT_UNCHANGED = r"""expected => {
  if (!['cgv.co.kr', 'www.cgv.co.kr'].includes(location.hostname) ||
      location.pathname !== '/mem/login') return false;
  const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  if ([...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')].some(visible)) return false;
  const id = document.querySelector('#loginInput1');
  const password = document.querySelector('#loginInput2');
  const captcha = document.querySelector('#loginInput3');
  const form = captcha && captcha.closest('form');
  const canvas = form && form.querySelector('canvas[aria-label="캡차 이미지"]');
  const submit = form && form.querySelector('button[type="submit"]');
  if (!id || !password || !canvas || !submit || submit.disabled ||
      ![id, password, captcha, canvas, submit].every(visible) || id.form !== form || password.form !== form ||
      !id.value.trim() || !password.value || password.type !== 'password' ||
      captcha.value !== expected.answer || canvas.toDataURL('image/png') !== expected.image ||
      [id, password].includes(document.activeElement)) return false;
  submit.click();
  return true;
}"""


def recognize_cgv_digits(image_bytes: bytes) -> str:
    """Require agreement between pixel-model outputs; no digest or answer enumeration."""
    if len(image_bytes) > 1024 * 1024:
        return ""
    with Image.open(io.BytesIO(image_bytes)) as source:
        if source.width > 1000 or source.height > 1000:
            return ""
        # The observed 240x95 canvas places the six glyphs in its upper band;
        # the bottom contains decoration, not additional answer characters.
        rgb = source.convert('RGB')
        rgb = rgb.crop((0, 0, rgb.width, round(rgb.height * .55)))
        gray = ImageOps.autocontrast(rgb.convert('L')).convert('RGB')
    original = io.BytesIO()
    rgb.save(original, 'PNG')
    image_bytes = original.getvalue()
    buffer = io.BytesIO()
    gray.save(buffer, 'PNG')

    def first(raw, beta):
        values = _recognize(raw, beta, 12, expected_length=6)
        answer = values[0] if isinstance(values, list) and values else values
        return answer if isinstance(answer, str) and re.fullmatch(r'[0-9]{6}', answer) else ''

    primary = [first(image_bytes, beta) for beta in (False, True)]
    if primary[0] and primary[0] == primary[1]:
        return primary[0]
    secondary = [first(buffer.getvalue(), beta) for beta in (False, True)]
    votes = primary + secondary
    for answer in set(votes) - {''}:
        if votes.count(answer) >= 3:
            return answer
    return ''


class CgvLoginAssistant:
    """At most one login submission per wait; never change credentials or refresh challenges."""

    def __init__(self, log, stop_event=None):
        self.log = log
        self.stop_event = stop_event
        self.done = False
        self.seen_images = set()
        self._notified = False

    def _stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def step(self, page):
        if self.done or self._stopped():
            return
        try:
            url = urlparse(str(page.url))
            if url.scheme != 'https' or url.hostname not in {'cgv.co.kr', 'www.cgv.co.kr'} or url.path != '/mem/login':
                return
            state = page.evaluate(_READ_FORM)
            if not isinstance(state, dict) or state.get('captchaFilled'):
                return
            if not state.get('credentialsReady') or state.get('editingCredentials'):
                if not self._notified:
                    self._notified = True
                    self.log('[CGV] 아이디·비밀번호 입력을 기다립니다. 입력 후 다른 곳을 클릭하면 숫자 인식과 로그인을 한 번 시도합니다.', 'info')
                return
            image = state.get('image', '')
            if image in self.seen_images or not image.startswith('data:image/png;base64,'):
                return
            if len(self.seen_images) >= 2 or len(image) > 1_400_000:
                self.done = True
                self.log('[CGV] 자동 숫자 인식 한도에 도달했습니다. 열린 Chrome에서 직접 로그인해주세요.', 'warning')
                return
            self.seen_images.add(image)
            answer = recognize_cgv_digits(base64.b64decode(image.split(',', 1)[1], validate=True))
            if self._stopped():
                return
            if not answer:
                self.log('[CGV] 숫자 인식 결과가 일치하지 않아 자동 제출하지 않습니다. 직접 입력해주세요.', 'warning')
                return
            current = page.evaluate(_READ_FORM)
            if current != state or self._stopped():
                return
            page.locator('#loginInput3').fill(answer, timeout=1500)
            if self._stopped():
                return
            # An exception after click may mean the login was received. Never retry it.
            self.done = True
            submitted = page.evaluate(_SUBMIT_UNCHANGED, {'image': image, 'answer': answer})
            if submitted:
                self.log('[CGV] 숫자 입력 후 로그인 요청을 한 번 실행했습니다. 회원 인증 완료를 확인합니다.', 'info')
            else:
                self.log('[CGV] 로그인 화면이 변경되어 자동 제출하지 않았습니다. 직접 확인해주세요.', 'warning')
        except Exception:
            self.done = True
            # Browser exceptions may include form values. Do not log them.
            self.log('[CGV] 자동 로그인 단계를 확인할 수 없습니다. 반복 제출 없이 수동 로그인을 기다립니다.', 'warning')
