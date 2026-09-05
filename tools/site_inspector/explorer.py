from __future__ import annotations

import hashlib
from html import unescape
import json
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from engines import browser_session
from pengucro.storage import get_data_dir

from .models import (
    ActionRecord,
    InspectionResult,
    InspectorConfig,
    NetworkRecord,
    PageState,
)
from .report import write_reports
from .security import (
    categorize_endpoint,
    classify_action,
    classify_request,
    sanitize_body,
    sanitize_headers,
    sanitize_html_snapshot,
    sanitize_url,
    same_site,
)


LogCallback = Callable[[str, str], None]
ProgressCallback = Callable[[int, int, str], None]


_CANDIDATE_SCRIPT = r"""
() => {
  const roots = [document];
  for (let index = 0; index < roots.length; index += 1) {
    for (const node of roots[index].querySelectorAll('*')) {
      if (node.shadowRoot && !roots.includes(node.shadowRoot)) roots.push(node.shadowRoot);
    }
  }
  const queryAll = (selector) => roots.flatMap(root => [...root.querySelectorAll(selector)]);
  const unique = (selector) => {
    try { return queryAll(selector).length === 1; }
    catch (_) { return false; }
  };
  const cssPath = (element) => {
    if (element.id) {
      const selector = `#${CSS.escape(element.id)}`;
      if (unique(selector)) return selector;
    }
    for (const attr of ['data-testid', 'data-cy', 'name']) {
      const value = element.getAttribute(attr);
      if (value) {
        const selector = `${element.tagName.toLowerCase()}[${attr}="${CSS.escape(value)}"]`;
        if (unique(selector)) return selector;
      }
    }
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 12) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = [...parent.children].filter(x => x.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      const selector = parts.join(' > ');
      if (unique(selector)) return selector;
      node = parent;
    }
    return parts.join(' > ');
  };
  const visible = (el) => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 1 && rect.height > 1;
  };
  const contextOf = (element) => {
    let node = element.parentElement;
    let fallback = '';
    for (let depth = 0; node && depth < 7; depth += 1) {
      const text = (node.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 320);
      if (!fallback && text) fallback = text;
      if (/예약\s*(?:가능|불가)|\b(?:available|unavailable)\b/i.test(text)) return text;
      node = node.parentElement;
    }
    return fallback;
  };
  const nodes = queryAll(
    'a[href],button,[role="button"],select,input[type="date"],input[type="checkbox"],input[type="radio"]'
  );
  const result = nodes.filter(visible).slice(0, 250).map((el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let kind = tag;
    if (tag === 'a') kind = 'link';
    if (tag === 'button' || el.getAttribute('role') === 'button') kind = 'button';
    if (tag === 'input') kind = type;
    const label = (
      el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') ||
      el.getAttribute('name') || el.value || ''
    ).trim().replace(/\s+/g, ' ').slice(0, 160);
    const context = contextOf(el);
    return {
      kind,
      label,
      context,
      selector: cssPath(el),
      href: el.href || '',
      onclick: el.getAttribute('onclick') || '',
      form_action: el.form?.action || '',
      form_method: (el.form?.method || '').toUpperCase(),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      checked: Boolean(el.checked),
      min: el.getAttribute('min') || '',
      max: el.getAttribute('max') || '',
      options: tag === 'select' ? [...el.options].map(o => ({
        value: o.value, label: (o.textContent || '').trim(), disabled: o.disabled
      })).slice(0, 30) : []
    };
  });
  return JSON.stringify(result);
}
"""


_PAGE_METADATA_SCRIPT = r"""
() => JSON.stringify({
  links: [...document.querySelectorAll('a[href]')].map(a => a.href).filter(Boolean).slice(0, 1000),
  scripts: [...document.scripts].map(s => s.src).filter(Boolean).slice(0, 300),
  forms: [...document.forms].map(f => ({
    action: f.action || location.href,
    method: (f.method || 'get').toUpperCase(),
    fields: [...f.elements].map(e => e.name || e.id || e.type).filter(Boolean).slice(0, 80)
  })).slice(0, 50)
})
"""


_DOM_INVENTORY_SCRIPT = r"""
() => {
  const roots = [document];
  for (let index = 0; index < roots.length; index += 1) {
    for (const node of roots[index].querySelectorAll('*')) {
      if (node.shadowRoot && !roots.includes(node.shadowRoot)) roots.push(node.shadowRoot);
    }
  }
  const queryAll = (selector) => roots.flatMap(root => [...root.querySelectorAll(selector)]);
  const cssPath = (element) => {
    if (!element || !element.tagName) return '';
    const esc = (value) => String(value).replace(/[^a-zA-Z0-9_-]/g, ch => `\\${ch}`);
    if (element.id) return `#${esc(element.id)}`;
    for (const attr of ['data-testid', 'data-cy', 'name']) {
      const value = element.getAttribute(attr);
      if (value) return `${element.tagName.toLowerCase()}[${attr}="${esc(value)}"]`;
    }
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(x => x.tagName === node.tagName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  };
  const labelOf = (el) => (
    el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('placeholder') || el.getAttribute('name') || ''
  ).trim().replace(/\s+/g, ' ').slice(0, 180);
  const controls = queryAll(
    'button,a[href],input,select,textarea,[role="button"],[role="tab"]'
  ).slice(0, 1200).map(el => ({
    selector: cssPath(el),
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || '').toLowerCase(),
    role: el.getAttribute('role') || '',
    id: el.id || '',
    name: el.getAttribute('name') || '',
    label: labelOf(el),
    href: el.href || '',
    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    required: Boolean(el.required),
    readonly: Boolean(el.readOnly),
    placeholder: el.getAttribute('placeholder') || '',
    min: el.getAttribute('min') || '',
    max: el.getAttribute('max') || '',
    option_count: el.tagName.toLowerCase() === 'select' ? el.options.length : 0
  }));
  const forms = queryAll('form').slice(0, 100).map(form => ({
    selector: cssPath(form),
    action: form.action || location.href,
    method: (form.method || 'GET').toUpperCase(),
    fields: Array.from(form.querySelectorAll('input,select,textarea,button')).slice(0, 200).map(el => ({
      selector: cssPath(el),
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: el.getAttribute('name') || '',
      id: el.id || '',
      required: Boolean(el.required),
      has_value: Boolean(el.value),
      value_length: String(el.value || '').length,
      option_count: el.tagName.toLowerCase() === 'select' ? el.options.length : 0
    }))
  }));
  const dateControls = controls.filter(item =>
    item.type === 'date' || /date|day|month|year|일자|날짜|달력/i.test(`${item.name} ${item.id} ${item.label}`)
  ).slice(0, 100);
  const calendarNextControls = controls.filter(item =>
    /다음\s*(달|월|날짜)|next\s*(month|date|day)/i.test(item.label) ||
    /(?:next.*(?:date|day|month)|(?:date|day|month).*next)/i.test(`${item.id} ${item.name}`)
  ).slice(0, 20);
  const routeTargets = [];
  for (const el of queryAll('[href],[action],[formaction],[data-url],[data-href],[data-action]').slice(0, 2000)) {
    for (const attr of ['href', 'action', 'formaction', 'data-url', 'data-href', 'data-action']) {
      const value = el.getAttribute(attr);
      if (value && !value.startsWith('#') && !value.toLowerCase().startsWith('javascript:')) {
        try { routeTargets.push(new URL(value, location.href).href); } catch (_) {}
      }
    }
  }
  const inlineHandlers = queryAll('[onclick],[onchange],[onsubmit]').slice(0, 1000).map(el => ({
    selector: cssPath(el),
    event: el.hasAttribute('onclick') ? 'click' : (el.hasAttribute('onchange') ? 'change' : 'submit'),
    handler: String(el.getAttribute('onclick') || el.getAttribute('onchange') || el.getAttribute('onsubmit') || '')
      .replace(/(['"])[^'"]{20,}\1/g, '$1[value]$1').slice(0, 300)
  }));
  const frameworkHints = [];
  if (document.querySelector('#__NEXT_DATA__')) frameworkHints.push('nextjs');
  if (window.__NUXT__ || document.querySelector('#__nuxt')) frameworkHints.push('nuxt');
  if (document.querySelector('[ng-version]')) frameworkHints.push('angular');
  if (document.querySelector('[data-reactroot],#__next')) frameworkHints.push('react');
  return JSON.stringify({
    url: location.href,
    lang: document.documentElement.lang || '',
    body_classes: String(document.body?.className || '').slice(0, 500),
    controls,
    forms,
    date_controls: dateControls,
    calendar_next_controls: calendarNextControls,
    route_targets: Array.from(new Set(routeTargets)).slice(0, 2000),
    inline_handlers: inlineHandlers,
    iframes: queryAll('iframe').slice(0, 100).map(frame => ({
      src: frame.src || '', title: frame.title || '', name: frame.name || ''
    })),
    templates: queryAll('template').length,
    shadow_roots: roots.length - 1,
    tables: queryAll('table').slice(0, 100).map(table => ({
      selector: cssPath(table),
      headers: Array.from(table.querySelectorAll('th')).slice(0, 30).map(th => labelOf(th))
    })),
    framework_hints: frameworkHints
  });
}
"""


_ENDPOINT_PATTERN = re.compile(
    r"[\"']((?:https?://[^\"'\\\s]+|/)(?:api/|v\d+/|auth/|availability/|booking|book|reserve|reservation|payment|checkout|calendar|inventory)[^\"'\\\s]*)",
    re.IGNORECASE,
)

_STATIC_ENDPOINT_PATTERN = re.compile(
    r"[\"']([^\"'\\\s]*(?:run_proc\.php|rev\.[a-z0-9_.-]+\.php|reservation(?:/[a-z0-9_.-]+)?|booking(?:/[a-z0-9_.-]+)?)[^\"'\\\s]*)[\"']",
    re.IGNORECASE,
)

_INLINE_ROUTE_PATTERN = re.compile(
    r'''["']((?:https?://[^"'\\\s<>]+|/[^"'\\\s<>]*|(?:\.\.?/)?[a-z0-9_./-]+)(?:\.do(?:\?[^"'\\\s<>]*)?|\.php(?:\?[^"'\\\s<>]*)?|/api(?:/[^"'\\\s<>]*)?|ajax[^"'\\\s<>]*|calendar[^"'\\\s<>]*|reservation[^"'\\\s<>]*|booking[^"'\\\s<>]*))["']''',
    re.IGNORECASE,
)

_STATIC_ASSET_SUFFIXES = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip",
    ".mp4", ".mp3",
)

_ROUTE_SELECTOR_QUERY_KEYS = {"go", "action", "mode", "route", "cmd", "op", "view", "type"}

_STATIC_COMMAND_PATTERN = re.compile(
    r"[\"']((?:get|search|select|load|fetch|check)_[a-z0-9_]{3,})[\"']",
    re.IGNORECASE,
)

_PAGE_BLOCKER_TERMS = (
    "개발자 도구 사용이 금지되어 있습니다",
    "developer tools are not allowed",
    "devtools is not allowed",
)


def _canonical_url(value: str) -> str:
    value, _fragment = urldefrag(value)
    parsed = urlsplit(value)
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def _crawl_route_signature(value: str) -> str:
    parsed = urlsplit(_canonical_url(value))
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, item if key.lower() in _ROUTE_SELECTOR_QUERY_KEYS else "{value}"))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), "")
    )


def _same_origin(url: str, origin: tuple[str, str]) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme.lower(), parsed.netloc.lower()) == origin


def _request_key(request) -> int:
    """Use Playwright's stable implementation object across request wrappers."""
    return id(getattr(request, "_impl_obj", request))


def _page_key(page) -> int:
    """Identify pages across the lightweight wrappers emitted by Playwright."""
    return id(getattr(page, "_impl_obj", page))


class SiteInspector:
    """Bounded autonomous browser explorer with a mutation firewall."""

    def __init__(
        self,
        config: InspectorConfig,
        *,
        log: LogCallback | None = None,
        progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config.validated()
        self._log_callback = log
        self._progress_callback = progress
        self.stop_event = stop_event or threading.Event()
        self._request_records: dict[int, NetworkRecord] = {}
        self._request_objects: dict[int, Any] = {}
        self._owned_page_ids: set[int] = set()
        self._script_urls: set[str] = set()
        self._state_signatures: set[str] = set()
        self._action_keys: set[str] = set()
        self._static_urls: set[str] = set()
        self._queued_route_signatures: set[str] = set()
        self._discovered_links: dict[str, dict[str, Any]] = {}
        self._origin = (
            urlsplit(self.config.start_url).scheme.lower(),
            urlsplit(self.config.start_url).netloc.lower(),
        )

    def _in_scope(self, url: str) -> bool:
        if _same_origin(url, self._origin):
            return True
        return bool(
            self.config.follow_related_subdomains
            and same_site(self.config.start_url, url)
        )

    def _emit(self, message: str, level: str = "info") -> None:
        if self._log_callback:
            self._log_callback(message, level)

    def _progress(self, current: int, total: int, message: str) -> None:
        if self._progress_callback:
            self._progress_callback(current, total, message)

    def _create_output_dir(self) -> Path:
        host = re.sub(r"[^a-zA-Z0-9._-]+", "_", urlsplit(self.config.start_url).netloc)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output_dir = Path(self.config.output_root) / f"{stamp}-{host}"
        (output_dir / "pages").mkdir(parents=True, exist_ok=False)
        (output_dir / "screenshots").mkdir(parents=True, exist_ok=False)
        (output_dir / "date-probes").mkdir(parents=True, exist_ok=False)
        return output_dir

    def run(self) -> InspectionResult:
        output_dir = self._create_output_dir()
        result = InspectionResult(output_dir=output_dir, start_url=self.config.start_url)
        chrome = None
        browser = None
        try:
            profile_path = get_data_dir() / "site-inspector-chrome-profile"
            chrome = browser_session.start_or_attach(
                9444,
                self._emit,
                profile_path=profile_path,
                allow_port_fallback=False,
            )
            if chrome is None:
                raise RuntimeError("사이트 분석용 Chrome을 시작하지 못했습니다.")
            self._emit("분석용 Chrome에 연결했습니다. 로그인 정보는 이 전용 프로필에만 보관됩니다.")
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(chrome.endpoint)
                if not browser.contexts:
                    raise RuntimeError("Chrome 브라우저 컨텍스트를 찾지 못했습니다.")
                context = browser.contexts[0]
                context.set_default_timeout(self.config.navigation_timeout_ms)
                context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
                context.route("**/*", lambda route: self._route_request(route, result))
                context.on("response", lambda response: self._record_response(response, result))
                context.on(
                    "requestfinished",
                    lambda request: self._record_finished_request(request, result),
                )
                # A persistent Chrome profile can retain a stale/restored tab
                # between runs. Always explore in a fresh page while preserving
                # the profile's cookies and login session.
                page = context.new_page()
                self._register_page(page)
                page.on(
                    "websocket",
                    lambda websocket: result.warnings.append(
                        f"WebSocket 연결 관측: {sanitize_url(websocket.url)} · 프레임 내용은 자동 재실행하지 않습니다."
                    ),
                )
                self._explore(context, page, result)
                self._reconcile_pending_responses(page, result)
                try:
                    context.unroute("**/*")
                except Exception:
                    pass
                try:
                    page.close()
                except Exception:
                    pass
                # Disconnect Playwright without terminating the dedicated
                # Chrome process. Consecutive analyses can reuse its login
                # session immediately instead of racing a profile shutdown.
                browser = None
        except Exception as exc:
            result.warnings.append(f"분석 중단: {type(exc).__name__}: {exc}")
            self._emit(f"분석 중 오류가 발생했습니다: {exc}", "error")
        finally:
            if chrome is not None:
                chrome.release()
                self._emit("분석용 Chrome을 다음 실행에서 재사용하도록 유지합니다.")
            write_reports(result)
        self._emit(f"분석 보고서를 생성했습니다: {output_dir}", "success")
        return result

    def _route_request(self, route, result: InspectionResult) -> None:
        try:
            request = route.request
            try:
                request_page = request.frame.page
            except Exception:
                request_page = None
            if request_page is None or _page_key(request_page) not in self._owned_page_ids:
                route.continue_()
                return
            body = request.post_data
            risk, blocked = classify_request(request.method, request.url, body)
            request_key = _request_key(request)
            record = NetworkRecord(
                request_key=request_key,
                method=request.method.upper(),
                url=sanitize_url(request.url),
                resource_type=request.resource_type,
                risk=risk,
                category=categorize_endpoint(request.url),
                blocked=blocked,
                request_headers=sanitize_headers(request.headers),
                request_body=sanitize_body(body),
            )
            self._request_records[request_key] = record
            self._request_objects[request_key] = request
            result.network.append(record)
            if blocked:
                if record.category != "telemetry":
                    self._emit(
                        f"위험 요청 차단 · {record.method} {urlsplit(record.url).path} · {risk}",
                        "warning",
                    )
                route.abort("blockedbyclient")
                return
            route.continue_()
        except Exception as exc:
            # A recorder failure must not turn into an accidental site outage.
            result.warnings.append(f"요청 기록 실패: {type(exc).__name__}")
            try:
                route.continue_()
            except Exception:
                pass

    def _register_page(self, page) -> None:
        """Scope recording and mutation blocking to pages created by this run."""
        page_id = _page_key(page)
        if page_id in self._owned_page_ids:
            return
        self._owned_page_ids.add(page_id)
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("popup", self._register_page)

    def _record_response(self, response, result: InspectionResult) -> None:
        record = self._find_request_record(response.request, result)
        if record is None:
            return
        record.status = response.status
        try:
            record.response_headers = sanitize_headers(response.headers)
        except Exception:
            record.response_headers = {}
        # Reading a body inside Playwright's `response` callback can wait for
        # completion while holding up later response events. Bodies are read
        # from `requestfinished`, where the payload is guaranteed complete.

    def _find_request_record(self, request, result: InspectionResult) -> NetworkRecord | None:
        record = self._request_records.get(_request_key(request))
        if record is None:
            # CDP can surface a fresh Python wrapper for the same request. Match
            # the most recent unresolved observation by method and sanitized URL.
            request_url = sanitize_url(request.url)
            request_method = request.method.upper()
            record = next(
                (
                    item
                    for item in reversed(result.network)
                    if item.status is None
                    and not item.blocked
                    and item.method == request_method
                    and item.url == request_url
                ),
                None,
            )
        return record

    def _record_finished_request(self, request, result: InspectionResult) -> None:
        """Fill status even when a very fast CDP response event was missed."""
        record = self._find_request_record(request, result)
        if record is None or record.blocked:
            return
        try:
            response = request.response()
            if response is not None:
                record.status = response.status
                if not record.response_headers:
                    record.response_headers = sanitize_headers(response.headers)
                self._record_response_body(response, record)
        except Exception as exc:
            record.error = f"response body unavailable: {type(exc).__name__}"

    def _record_response_body(self, response, record: NetworkRecord) -> None:
        if record.resource_type not in {"xhr", "fetch", "document"}:
            return
        content_type = response.headers.get("content-type", "").lower()
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > self.config.response_body_limit:
            record.response_body = f"[BODY_TOO_LARGE:{content_length}]"
            return
        if not any(term in content_type for term in ("json", "text", "javascript", "xml", "html")):
            return
        body = response.body()
        if len(body) > self.config.response_body_limit:
            record.response_body = f"[BODY_TOO_LARGE:{len(body)}]"
        else:
            record.response_body = sanitize_body(body)

    def _reconcile_pending_responses(
        self,
        page,
        result: InspectionResult,
        *,
        wait_ms: int = 400,
    ) -> None:
        """Boundedly reconcile async fetches before their page target is replaced."""
        deadline = time.monotonic() + max(0, wait_ms) / 1000
        while True:
            pending = 0
            for request_key, record in list(self._request_records.items()):
                if (
                    record.blocked
                    or record.status is not None
                    or record.error
                    or record.resource_type not in {"xhr", "fetch", "document"}
                ):
                    continue
                request = self._request_objects.get(request_key)
                if request is None:
                    continue
                try:
                    request_page = request.frame.page
                    if (
                        request_page.is_closed()
                        or _page_key(request_page) != _page_key(page)
                    ):
                        continue
                    response = request.response()
                    if response is None:
                        pending += 1
                        continue
                    record.status = response.status
                    if not record.response_headers:
                        record.response_headers = sanitize_headers(response.headers)
                    self._record_response_body(response, record)
                except Exception as exc:
                    if type(exc).__name__ == "TargetClosedError":
                        # A short-lived probe page can close after yielding its
                        # DOM. That is an instrumentation boundary, not a site
                        # request failure, so leave the record neutral.
                        continue
                    record.error = (
                        f"final response reconciliation unavailable: {type(exc).__name__}"
                    )
            if pending == 0 or time.monotonic() >= deadline:
                return
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            try:
                page.wait_for_timeout(min(50, remaining_ms))
            except Exception:
                return

    def _explore(self, context, page, result: InspectionResult) -> None:
        start = _canonical_url(self.config.start_url)
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        queued = {start}
        self._queued_route_signatures = {_crawl_route_signature(start)}
        self._discovered_links = {
            _crawl_route_signature(start): {"url": sanitize_url(start), "depth": 0}
        }
        visited_urls: set[str] = set()
        self._enqueue_sitemap(context, queue, queued, result)

        visited_documents = 0
        while (
            queue
            and visited_documents < self.config.max_pages
            and len(result.states) < self.config.max_states
            and not self.stop_event.is_set()
        ):
            url, depth = queue.popleft()
            if url in visited_urls or depth > self.config.max_depth:
                continue
            visited_urls.add(url)
            visited_documents += 1
            self._progress(visited_documents, self.config.max_pages, f"페이지 여는 중: {url}")
            self._emit(f"페이지 탐색 · 깊이 {depth} · {url}")
            try:
                page.goto(url, wait_until="commit")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    # Modern applications can keep navigation lifecycle events
                    # pending even after usable DOM content is visible.
                    pass
                self._settle(page, result=result)
                self._wait_for_manual_challenge(page)
            except Exception as exc:
                detail = str(exc).splitlines()[0][:240]
                result.warnings.append(
                    f"페이지 열기 실패: {sanitize_url(url)} · {type(exc).__name__}: {detail}"
                )
                continue

            metadata = self._page_metadata(page)
            self._enqueue_links(metadata.get("links", []), depth + 1, queue, queued)
            self._scan_scripts(context, metadata.get("scripts", []), result)
            state = self._capture_state(page, depth, result)
            if state is None:
                continue
            inventory = self._capture_dom_inventory(page, state.state_id, result)
            self._scan_inline_html(page, state.state_id, result)
            self._enqueue_links(inventory.get("route_targets", []), depth + 1, queue, queued)
            blocker = self._detect_page_blocker(page)
            if blocker or state.interactive_count == 0:
                reason = blocker or "표시된 대화형 컨트롤 없음"
                self._analyze_static_html(context, url, state.state_id, reason, result)
            self._record_forms(metadata.get("forms", []), state.state_id, result)

            actions_done = 0
            while (
                actions_done < self.config.max_actions_per_page
                and len(result.states) < self.config.max_states
                and not self.stop_event.is_set()
            ):
                candidate = self._next_candidate(page, state.state_id, result)
                if candidate is None:
                    break
                actions_done += 1
                outcome = self._perform_candidate(page, candidate)
                action = ActionRecord(
                    state_id=state.state_id,
                    kind=candidate["kind"],
                    label=candidate.get("label", ""),
                    selector=candidate["selector"],
                    risk=candidate["risk"],
                    outcome=outcome,
                    resulting_url=sanitize_url(page.url),
                )
                result.actions.append(action)
                if outcome.startswith("실행"):
                    self._settle(page, result=result, short=True)
                    metadata = self._page_metadata(page)
                    self._enqueue_links(metadata.get("links", []), depth + 1, queue, queued)
                    self._scan_scripts(context, metadata.get("scripts", []), result)
                    new_state = self._capture_state(page, depth, result)
                    if new_state is not None:
                        action.resulting_state_id = new_state.state_id
                        state = new_state
                        inventory = self._capture_dom_inventory(page, state.state_id, result)
                        self._scan_inline_html(page, state.state_id, result)
                        self._enqueue_links(
                            inventory.get("route_targets", []), depth + 1, queue, queued
                        )
            self._progress(visited_documents, self.config.max_pages, "페이지 분석 완료")

        if self.stop_event.is_set():
            result.warnings.append("사용자 요청으로 분석을 중지했습니다.")
        if queue and visited_documents >= self.config.max_pages:
            result.warnings.append("설정한 최대 방문 경로 수에 도달해 나머지 경로는 탐색하지 않았습니다.")
        if queue and len(result.states) >= self.config.max_states:
            result.warnings.append("설정한 최대 화면 상태 수에 도달해 나머지 동작과 경로는 탐색하지 않았습니다.")
        result.discovered_routes = sorted(
            self._discovered_links.values(), key=lambda item: (item["depth"], item["url"])
        )
        self._probe_unopened_dates(context, result)

    def _settle(
        self,
        page,
        *,
        result: InspectionResult | None = None,
        short: bool = False,
    ) -> None:
        # `wait_for_load_state("networkidle")` may resolve immediately when the
        # document was already idle just before a click starts an async fetch.
        # Give event handlers and queued fetches a short chance to begin first.
        page.wait_for_timeout(300 if short else 500)
        try:
            page.wait_for_load_state("networkidle", timeout=3_000 if short else 7_000)
        except Exception:
            page.wait_for_timeout(200 if short else 500)
        if result is not None:
            # Reconcile before a later action or queued navigation can destroy
            # the request's CDP target. This keeps fast fetch/XHR status data
            # deterministic without adding another fixed delay.
            self._reconcile_pending_responses(page, result, wait_ms=750)

    def _wait_for_manual_challenge(self, page) -> None:
        try:
            title = page.title().lower()
            text = page.locator("body").inner_text(timeout=2_000).lower()[:4000]
            password_count = page.locator('input[type="password"]').count()
        except Exception:
            return
        challenge = any(
            term in f"{title} {text}"
            for term in ("사람인지 확인", "보안 확인", "verify you are human", "captcha", "cloudflare")
        )
        login_page = password_count > 0 and any(term in page.url.lower() for term in ("login", "signin", "auth"))
        if not challenge and not login_page:
            return
        wait_seconds = int(self.config.manual_intervention_timeout_seconds)
        if wait_seconds <= 0:
            return
        self._emit(
            f"사람 확인 또는 로그인이 필요합니다. 열린 Chrome에서 완료하면 자동으로 계속합니다. (최대 {wait_seconds}초)",
            "warning",
        )
        deadline = time.monotonic() + wait_seconds
        initial_url = page.url
        while time.monotonic() < deadline and not self.stop_event.is_set():
            page.wait_for_timeout(1000)
            try:
                current_text = page.locator("body").inner_text(timeout=1_000).lower()[:3000]
                current_passwords = page.locator('input[type="password"]').count()
                still_challenge = any(
                    term in current_text
                    for term in ("사람인지 확인", "보안 확인", "verify you are human", "captcha")
                )
                still_login = current_passwords > 0 and page.url == initial_url
                if not still_challenge and not still_login:
                    self._emit("사람 확인 또는 로그인이 완료되어 자동 분석을 계속합니다.", "success")
                    return
            except Exception:
                continue

    def _page_metadata(self, page) -> dict[str, Any]:
        try:
            value = page.evaluate(_PAGE_METADATA_SCRIPT)
            if isinstance(value, str):
                value = json.loads(value)
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            self._emit(f"페이지 메타데이터 추출 실패: {type(exc).__name__}", "warning")
            return {}

    def _capture_dom_inventory(
        self,
        page,
        state_id: str,
        result: InspectionResult,
    ) -> dict[str, Any]:
        try:
            raw = page.evaluate(_DOM_INVENTORY_SCRIPT)
            inventory = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(inventory, dict):
                raise ValueError("DOM inventory is not an object")
        except Exception as exc:
            result.warnings.append(f"{state_id} DOM 구조 추출 실패: {type(exc).__name__}")
            return {}
        inventory["state_id"] = state_id
        inventory["url"] = sanitize_url(str(inventory.get("url") or page.url))
        inventory["route_targets"] = [
            sanitize_url(str(value)) for value in inventory.get("route_targets", [])
        ]
        for frame in inventory.get("iframes", []):
            frame["src"] = sanitize_url(str(frame.get("src", "")))
        for form in inventory.get("forms", []):
            form["action"] = sanitize_url(str(form.get("action", "")))
        for control in inventory.get("controls", []):
            if control.get("href"):
                control["href"] = sanitize_url(str(control["href"]))
        result.dom_inventory.append(inventory)
        return inventory

    def _signature(self, page) -> str:
        try:
            value = page.evaluate(
                """() => {
                    const roots = [document];
                    for (let index = 0; index < roots.length; index += 1) {
                      for (const node of roots[index].querySelectorAll('*')) {
                        if (node.shadowRoot && !roots.includes(node.shadowRoot)) roots.push(node.shadowRoot);
                      }
                    }
                    const controls = roots.flatMap(root =>
                      [...root.querySelectorAll('a,button,input,select')]
                    ).slice(0, 500).map(e => [
                      e.tagName, e.getAttribute('type'), e.getAttribute('name'),
                      e.getAttribute('aria-label'), e.innerText, e.value,
                      e.disabled, e.getAttribute('aria-disabled')
                    ]);
                    const shadowText = roots.slice(1)
                      .map(root => root.textContent || '').join(' ');
                    return JSON.stringify({
                      url: location.href,
                      title: document.title,
                      text: `${document.body?.innerText || ''} ${shadowText}`.slice(0, 24000),
                      controls
                    });
                }"""
            )
        except Exception:
            value = page.url
        return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()

    def _capture_state(self, page, depth: int, result: InspectionResult) -> PageState | None:
        signature = self._signature(page)
        if signature in self._state_signatures:
            return None
        self._state_signatures.add(signature)
        number = len(result.states) + 1
        state_id = f"state-{number:03d}"
        html_rel = f"pages/{state_id}.html"
        image_rel = f"screenshots/{state_id}.png"
        try:
            html = sanitize_html_snapshot(page.content())
            (result.output_dir / html_rel).write_text(html, encoding="utf-8", errors="replace")
        except Exception as exc:
            html_rel = ""
            result.warnings.append(f"{state_id} HTML 저장 실패: {type(exc).__name__}")
        try:
            page.screenshot(
                path=str(result.output_dir / image_rel),
                full_page=True,
                animations="disabled",
                timeout=12_000,
            )
        except Exception:
            try:
                page.screenshot(
                    path=str(result.output_dir / image_rel),
                    full_page=False,
                    animations="disabled",
                    timeout=6_000,
                )
                result.warnings.append(f"{state_id} 전체 화면 캡처 시간 초과 · 현재 보이는 영역으로 저장")
            except Exception as exc:
                image_rel = ""
                result.warnings.append(f"{state_id} 화면 저장 실패: {type(exc).__name__}")
        try:
            interactive_count = page.locator("a,button,input,select,textarea,[role=button]").count()
            title = page.title()
        except Exception:
            interactive_count = 0
            title = ""
        state = PageState(
            state_id=state_id,
            url=sanitize_url(page.url),
            title=title,
            depth=depth,
            signature=signature,
            html_file=html_rel,
            screenshot_file=image_rel,
            interactive_count=interactive_count,
        )
        result.states.append(state)
        self._emit(f"화면 상태 저장 · {state_id} · {title or page.url}")
        return state

    def _next_candidate(self, page, state_id: str, result: InspectionResult) -> dict[str, Any] | None:
        try:
            candidates = page.evaluate(_CANDIDATE_SCRIPT)
            if isinstance(candidates, str):
                candidates = json.loads(candidates)
        except Exception as exc:
            result.warnings.append(f"컨트롤 탐색 실패: {type(exc).__name__}")
            return None
        def action_priority(item: dict[str, Any]) -> int:
            label = str(item.get("label", "")).lower()
            if any(term in label for term in ("닫기", "close")):
                return 0
            if label.strip() in {
                "예약", "예약하기", "예매", "예매하기", "book now", "reserve now"
            }:
                return 1
            href = str(item.get("href", ""))
            if item.get("kind") == "link" and (
                href.lower().startswith("javascript:") or bool(urlsplit(href).fragment)
            ):
                return 1
            if item.get("kind") in {"date", "select", "checkbox", "radio"}:
                return 2
            return 3

        ordered = sorted(candidates or [], key=action_priority)
        for candidate in ordered:
            if candidate.get("disabled") or not candidate.get("selector"):
                continue
            kind = str(candidate.get("kind", ""))
            href = " ".join(
                str(candidate.get(key, ""))
                for key in ("href", "onclick", "form_action", "form_method")
            )
            href = f"{href} {page.url}"
            if kind == "link":
                raw_href = str(candidate.get("href", ""))
                is_script_router = raw_href.lower().startswith("javascript:")
                is_fragment_router = bool(urlsplit(raw_href).fragment)
                if not (is_script_router or is_fragment_router):
                    continue
            # Most form controls remain the same logical action after state
            # changes. Slot buttons are the exception: the same DOM position
            # can change from unavailable to available after selecting a date.
            context = str(candidate.get("context", ""))
            state_marker = ""
            if re.search(r"예약\s*불가|\bunavailable\b", context, re.IGNORECASE):
                state_marker = "unavailable"
            elif re.search(r"예약\s*가능|\bavailable\b", context, re.IGNORECASE):
                state_marker = "available"
            key = (
                f"{_canonical_url(page.url)}|{kind}|{candidate['selector']}|{state_marker}"
            )
            if key in self._action_keys:
                continue
            self._action_keys.add(key)
            risk = classify_action(
                str(candidate.get("label", "")),
                kind,
                href,
                context,
            )
            candidate["risk"] = risk
            if risk != "explorable":
                result.actions.append(
                    ActionRecord(
                        state_id=state_id,
                        kind=kind,
                        label=str(candidate.get("label", "")),
                        selector=str(candidate["selector"]),
                        risk=risk,
                        outcome="안전 정책에 따라 클릭하지 않음",
                        resulting_url=sanitize_url(page.url),
                    )
                )
                continue
            return candidate
        return None

    def _perform_candidate(self, page, candidate: dict[str, Any]) -> str:
        selector = candidate["selector"]
        kind = candidate["kind"]
        try:
            matches = page.locator(selector)
            locator = None
            for index in range(min(matches.count(), 20)):
                current = matches.nth(index)
                if current.is_visible() and current.is_enabled():
                    locator = current
                    break
            if locator is None:
                return "건너뜀 · 동작 직전 비표시 또는 비활성"
            if kind == "select":
                options = [
                    item for item in candidate.get("options", [])
                    if not item.get("disabled") and str(item.get("value", "")).strip()
                ]
                if not options:
                    return "실행할 선택값 없음"
                locator.select_option(str(options[0]["value"]), timeout=4_000)
                return f"실행 · 선택값 {options[0].get('label') or options[0]['value']}"
            if kind == "date":
                tomorrow = date.today() + timedelta(days=1)
                minimum = str(candidate.get("min", ""))
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", minimum):
                    try:
                        tomorrow = max(tomorrow, date.fromisoformat(minimum))
                    except ValueError:
                        pass
                value = tomorrow.isoformat()
                locator.fill(value, timeout=4_000)
                locator.dispatch_event("change")
                return f"실행 · 날짜 {value}"
            if kind in {"checkbox", "radio"}:
                locator.check(timeout=4_000)
                return "실행 · 선택"
            locator.click(timeout=5_000)
            return "실행 · 클릭"
        except Exception as exc:
            return f"실패 · {type(exc).__name__}"

    @staticmethod
    def _date_query_value(original: str, target: date, key: str = "") -> str | None:
        value = str(original).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return target.isoformat()
        if re.fullmatch(r"\d{8}", value):
            return target.strftime("%Y%m%d")
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return target.strftime("%Y-%m")
        if re.fullmatch(r"\d{6}", value):
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized and not any(
                term in normalized for term in ("month", "yyyymm", "searchym")
            ):
                return target.strftime("%y%m%d")
            return target.strftime("%Y%m")
        return None

    @staticmethod
    def _parse_date_value(value: str) -> date | None:
        text = str(value).strip()
        for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _date_key_role(key: str) -> str:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(
            term in normalized
            for term in (
                "checkout", "enddate", "returndate", "todate", "dateto", "arrivaldate"
            )
        ) or normalized.endswith("to"):
            return "end"
        if any(
            term in normalized
            for term in (
                "checkin", "startdate", "fromdate", "datefrom", "dateform", "departuredate"
            )
        ):
            return "start"
        return "single"

    @staticmethod
    def _looks_like_date_key(key: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        return any(
            term in normalized
            for term in (
                "date",
                "day",
                "revdays",
                "checkin",
                "checkout",
                "usedt",
                "usedate",
                "rsvde",
                "yyyymm",
                "yyyymmdd",
                "searchdt",
                "searchymd",
            )
        )

    @staticmethod
    def _dom_probe_summary(html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        controls = soup.select("button,a[href],input,select,textarea,[role=button]")
        forms = soup.find_all("form")
        hidden_names = sorted(
            {
                str(node.get("name") or node.get("id") or "")
                for node in soup.select('input[type="hidden"]')
                if node.get("name") or node.get("id")
            }
        )
        slot_terms = re.compile(r"예약|예매|시간|회차|slot|available|마감|매진", re.IGNORECASE)
        slot_like = sum(
            1
            for node in controls[:3000]
            if slot_terms.search(node.get_text(" ", strip=True)[:160])
        )
        structural = "|".join(
            f"{node.name}:{node.get('type', '')}:{node.get('name', '')}:{' '.join(node.get('class', [])[:3])}"
            for node in soup.select("form,input,select,button,table,[class*=calendar],[class*=date]")[:3000]
        )
        return {
            "title": soup.title.get_text(" ", strip=True)[:200] if soup.title else "",
            "control_count": len(controls),
            "form_count": len(forms),
            "hidden_field_names": hidden_names[:200],
            "slot_like_control_count": slot_like,
            "structural_signature": hashlib.sha256(
                structural.encode("utf-8", "replace")
            ).hexdigest(),
        }

    def _save_date_probe_html(
        self,
        html: str,
        result: InspectionResult,
    ) -> str:
        number = len(result.date_probes) + 1
        relative = f"date-probes/probe-{number:03d}.html"
        (result.output_dir / relative).write_text(
            sanitize_html_snapshot(html), encoding="utf-8", errors="replace"
        )
        return relative

    def _render_html_date_probe(
        self,
        context,
        target_url: str,
        result: InspectionResult,
        probe: dict[str, Any],
    ) -> None:
        """Render a sparse SPA shell so date-dependent XHR and live DOM are observable."""
        before = len(result.network)
        page = context.new_page()
        self._register_page(page)
        try:
            page.goto(target_url, wait_until="commit", timeout=self.config.navigation_timeout_ms)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except Exception:
                pass
            self._settle(page, result=result, short=True)
            if classify_action("예약하기", "button", page.url) == "explorable":
                opener = page.get_by_role(
                    "button",
                    name=re.compile(r"^(예약|예약하기|예매|예매하기|book now|reserve now)$", re.IGNORECASE),
                )
                if opener.count() > 0:
                    opener.first.click(timeout=4_000)
                    self._settle(page, result=result, short=True)
                    probe["opened_preparation_ui"] = True
            html = page.content()
            probe.update(self._dom_probe_summary(html))
            probe["html_file"] = self._save_date_probe_html(html, result)
            probe["kind"] = "get-date-query-rendered"
            network_rows = []
            seen_network: set[tuple[str, str]] = set()
            for item in result.network[before:]:
                path = urlsplit(item.url).path.lower()
                key = (item.method, _crawl_route_signature(item.url))
                if (
                    item.resource_type not in {"xhr", "fetch"}
                    or not self._in_scope(item.url)
                    or item.category == "telemetry"
                    or ("/api/" not in path and item.category == "other")
                    or key in seen_network
                ):
                    continue
                seen_network.add(key)
                network_rows.append(
                    {
                        "method": item.method,
                        "url": item.url,
                        "category": item.category,
                        "risk": item.risk,
                        "blocked": item.blocked,
                        "status": item.status,
                    }
                )
            probe["rendered_network"] = network_rows[:50]
        except Exception as exc:
            probe["render_error"] = type(exc).__name__
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _probe_unopened_dates(self, context, result: InspectionResult) -> None:
        limit = int(self.config.max_date_probes)
        if limit <= 0:
            return
        offsets = tuple(dict.fromkeys(int(value) for value in self.config.date_probe_offsets_days))
        source_urls: list[str] = []
        source_urls.extend(state.url for state in result.states)
        source_urls.extend(
            item.url
            for item in result.network
            if item.method == "GET" and item.resource_type in {"document", "xhr", "fetch"}
        )
        for inventory in result.dom_inventory:
            source_urls.extend(str(value) for value in inventory.get("route_targets", []))

        seen_targets: set[str] = set()
        for source_url in source_urls:
            if len(result.date_probes) >= limit:
                break
            parsed = urlsplit(source_url)
            if not self._in_scope(source_url):
                continue
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if not query:
                continue
            start_dates = [
                self._parse_date_value(value)
                for key, value in query
                if self._date_key_role(key) == "start"
            ]
            end_dates = [
                self._parse_date_value(value)
                for key, value in query
                if self._date_key_role(key) == "end"
            ]
            start_date = next((value for value in start_dates if value is not None), None)
            end_date = next((value for value in end_dates if value is not None), None)
            range_days = max(1, (end_date - start_date).days) if start_date and end_date else 1
            for offset in offsets:
                target_date = date.today() + timedelta(days=offset)
                changed = False
                new_query = []
                for key, value in query:
                    key_target = (
                        target_date + timedelta(days=range_days)
                        if self._date_key_role(key) == "end"
                        else target_date
                    )
                    replacement = (
                        self._date_query_value(value, key_target, key)
                        if self._looks_like_date_key(key)
                        else None
                    )
                    new_query.append((key, replacement if replacement is not None else value))
                    changed = changed or replacement is not None
                if not changed:
                    continue
                target_url = urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, urlencode(new_query), "")
                )
                canonical = _canonical_url(target_url)
                if canonical in seen_targets:
                    continue
                seen_targets.add(canonical)
                probe: dict[str, Any] = {
                    "kind": "get-date-query",
                    "source_url": sanitize_url(source_url),
                    "target_url": sanitize_url(target_url),
                    "offset_days": offset,
                    "target_date": target_date.isoformat(),
                    "status": None,
                    "content_type": "",
                    "html_file": "",
                    "error": "",
                }
                try:
                    response = context.request.get(target_url, timeout=12_000)
                    probe["status"] = response.status
                    content_type = response.headers.get("content-type", "").lower()
                    probe["content_type"] = content_type
                    if "html" in content_type:
                        html = response.text()
                        probe.update(self._dom_probe_summary(html))
                        probe["html_file"] = self._save_date_probe_html(html, result)
                        if (
                            probe.get("control_count", 0) <= 5
                            and probe.get("form_count", 0) == 0
                            and probe.get("slot_like_control_count", 0) == 0
                        ):
                            self._render_html_date_probe(context, target_url, result, probe)
                    elif "json" in content_type:
                        probe["response_schema"] = self._value_schema(
                            sanitize_body(response.body())
                        )
                except Exception as exc:
                    probe["error"] = type(exc).__name__
                result.date_probes.append(probe)
                self._emit(
                    f"미오픈 날짜 GET 탐색 · +{offset}일 · HTTP {probe['status'] or '-'}",
                    "info",
                )
                if len(result.date_probes) >= limit:
                    break

        if len(result.date_probes) < limit:
            self._probe_read_post_dates(context, result, limit, offsets)
        if len(result.date_probes) < limit:
            self._probe_browser_date_controls(context, result, limit, offsets)

    def _mutate_date_payload(self, payload: dict[str, Any], target: date) -> dict[str, Any] | None:
        mutated = json.loads(json.dumps(payload, ensure_ascii=False))
        changed = False
        component_values = {
            "sltyear": target.strftime("%Y"),
            "sltmonth": target.strftime("%m"),
            "sltday": target.strftime("%d"),
            "yyyy": target.strftime("%Y"),
            "mm": target.strftime("%m"),
            "dd": target.strftime("%d"),
            "useyear": target.strftime("%Y"),
            "usemonth": target.strftime("%m"),
            "useday": target.strftime("%d"),
            "yyyymm": target.strftime("%Y%m"),
            "yyyymmdd": target.strftime("%Y%m%d"),
        }
        for key, value in list(mutated.items()):
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in component_values:
                mutated[key] = component_values[normalized]
                changed = True
                continue
            if not self._looks_like_date_key(str(key)):
                continue
            replacement = self._date_query_value(str(value), target, str(key))
            if replacement is not None:
                mutated[key] = replacement
                changed = True
        if any(
            isinstance(value, str) and value.startswith("[REDACTED:")
            for value in mutated.values()
        ):
            return None
        return mutated if changed else None

    def _probe_read_post_dates(
        self,
        context,
        result: InspectionResult,
        limit: int,
        offsets: tuple[int, ...],
    ) -> None:
        seen: set[tuple[str, str]] = set()
        read_name = re.compile(
            r"/(?:page)?(?:select|search|get|fetch|load|list|check)[a-z0-9_.-]*(?:/|$)",
            re.IGNORECASE,
        )
        for record in result.network:
            if len(result.date_probes) >= limit:
                return
            if (
                record.method != "POST"
                or record.blocked
                or record.risk != "read-post"
                or not isinstance(record.request_body, dict)
                or not read_name.search(urlsplit(record.url).path)
            ):
                continue
            content_type = record.request_headers.get("content-type", "").lower()
            for offset in offsets:
                target = date.today() + timedelta(days=offset)
                payload = self._mutate_date_payload(record.request_body, target)
                if payload is None:
                    continue
                payload_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                key = (record.url, payload_key)
                if key in seen:
                    continue
                seen.add(key)
                probe: dict[str, Any] = {
                    "kind": "read-post-date-payload",
                    "source_url": record.url,
                    "target_url": record.url,
                    "offset_days": offset,
                    "target_date": target.isoformat(),
                    "status": None,
                    "content_type": "",
                    "request_schema": self._value_schema(payload),
                    "html_file": "",
                    "error": "",
                }
                try:
                    kwargs: dict[str, Any] = {"timeout": 12_000}
                    if "json" in content_type:
                        kwargs["data"] = payload
                    else:
                        kwargs["form"] = {str(k): str(v) for k, v in payload.items()}
                    response = context.request.post(record.url, **kwargs)
                    probe["status"] = response.status
                    response_content_type = response.headers.get("content-type", "").lower()
                    probe["content_type"] = response_content_type
                    body = response.body()
                    if "html" in response_content_type:
                        html = body.decode("utf-8", "replace")
                        probe.update(self._dom_probe_summary(html))
                        probe["html_file"] = self._save_date_probe_html(html, result)
                    else:
                        sanitized = sanitize_body(body)
                        probe["response_schema"] = self._value_schema(sanitized)
                        if isinstance(sanitized, dict):
                            probe["response_collection_counts"] = {
                                str(key): len(value)
                                for key, value in sanitized.items()
                                if isinstance(value, list)
                            }
                except Exception as exc:
                    probe["error"] = type(exc).__name__
                result.date_probes.append(probe)
                self._emit(
                    f"미오픈 날짜 조회 POST · +{offset}일 · HTTP {probe['status'] or '-'}",
                    "info",
                )
                if len(result.date_probes) >= limit:
                    return

    def _probe_browser_date_controls(
        self,
        context,
        result: InspectionResult,
        limit: int,
        offsets: tuple[int, ...],
    ) -> None:
        targets: list[tuple[str, dict[str, Any], str]] = []
        seen: set[tuple[str, str, str]] = set()
        for inventory in result.dom_inventory:
            url = str(inventory.get("url", ""))
            date_inputs = []
            for control in inventory.get("date_controls", []):
                if (
                    control.get("tag") == "input"
                    and control.get("readonly")
                    and not control.get("disabled")
                    and control.get("selector")
                ):
                    key = (url, str(control["selector"]), "readonly-date-calendar")
                    if key not in seen:
                        seen.add(key)
                        targets.append((url, control, "readonly-date-calendar"))
                    continue
                if (
                    control.get("tag") != "input"
                    or control.get("disabled")
                    or control.get("readonly")
                    or not control.get("selector")
                    or control.get("type") not in {"", "date", "text", "search"}
                ):
                    continue
                key = (url, str(control["selector"]), "date-inputs")
                if key not in seen:
                    seen.add(key)
                    date_inputs.append(control)
            if date_inputs:
                targets.append((url, {"controls": date_inputs}, "date-inputs"))
            for control in inventory.get("calendar_next_controls", []):
                if not control.get("selector"):
                    continue
                key = (url, str(control["selector"]), "calendar-next")
                if key not in seen:
                    seen.add(key)
                    targets.append((url, control, "calendar-next"))

        for url, control, kind in targets:
            if len(result.date_probes) >= limit:
                return
            page = context.new_page()
            self._register_page(page)
            try:
                page.goto(url, wait_until="commit", timeout=self.config.navigation_timeout_ms)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=8_000)
                except Exception:
                    pass
                page.wait_for_timeout(500)
                selector = str(control.get("selector", ""))
                if kind == "date-inputs":
                    offset = offsets[-1] if offsets else 180
                    base_date = date.today() + timedelta(days=offset)
                    filled = []
                    controls = list(control.get("controls", []))
                    for index, item in enumerate(controls):
                        item_selector = str(item.get("selector", ""))
                        locator = page.locator(item_selector).first
                        current = locator.input_value(timeout=3_000)
                        semantic = " ".join(
                            str(item.get(key, ""))
                            for key in ("id", "name", "label", "placeholder")
                        ).lower()
                        is_end = index > 0 or any(
                            term in semantic
                            for term in (
                                "checkout", "check-out", "enddate", "end_date", "return",
                                "도착", "종료", "퇴실",
                            )
                        )
                        target_date = base_date + timedelta(days=1 if is_end else 0)
                        maximum = str(item.get("max", ""))
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", maximum):
                            try:
                                target_date = min(target_date, date.fromisoformat(maximum))
                            except ValueError:
                                pass
                        hint = current or str(item.get("placeholder", ""))
                        value = self._format_browser_date_value(
                            target_date,
                            hint,
                            str(item.get("type", "")),
                        )
                        if value is None:
                            continue
                        locator.fill(value, timeout=4_000)
                        locator.dispatch_event("input")
                        locator.dispatch_event("change")
                        filled.append(
                            {"selector": item_selector, "target_date": target_date.isoformat()}
                        )
                    if not filled:
                        raise ValueError("지원되는 날짜 입력 형식을 찾지 못했습니다.")
                    page.wait_for_timeout(700)
                    probe = {
                        "kind": "browser-date-inputs",
                        "source_url": sanitize_url(url),
                        "target_url": sanitize_url(page.url),
                        "target_date": base_date.isoformat(),
                        "controls": filled,
                        "status": "captured",
                        "error": "",
                    }
                elif kind == "readonly-date-calendar":
                    date_opener = page.locator(selector).first
                    try:
                        date_opener.click(timeout=4_000)
                    except Exception:
                        # Reservation pages often cover a readonly input with a
                        # calendar icon. A DOM click invokes the same harmless
                        # picker handler without pointer-actionability waiting.
                        date_opener.evaluate("element => element.click()")
                    self._settle(page, result=result, short=True)
                    advanced = False
                    next_locator = page.locator(
                        '[role="dialog"] [aria-label*="다음"], '
                        '[role="dialog"] [title*="다음"], '
                        '[class*="calendar" i] [id*="next" i], '
                        '[class*="calendar" i] [class*="next" i], '
                        '[class*="date" i] [id*="next" i]'
                    )
                    for index in range(min(next_locator.count(), 20)):
                        candidate = next_locator.nth(index)
                        if candidate.is_visible():
                            try:
                                candidate.click(timeout=3_000)
                            except Exception:
                                candidate.evaluate("element => element.click()")
                            self._settle(page, result=result, short=True)
                            advanced = True
                            break
                    probe = {
                        "kind": kind,
                        "source_url": sanitize_url(url),
                        "target_url": sanitize_url(page.url),
                        "target_date": (
                            date.today() + timedelta(days=30)
                        ).isoformat() if advanced else "",
                        "selector": selector,
                        "label": str(control.get("label", "")),
                        "calendar_advanced": advanced,
                        "status": "captured",
                        "error": "",
                    }
                else:
                    page.locator(selector).first.click(timeout=4_000)
                    page.wait_for_timeout(700)
                    probe = {
                        "kind": kind,
                        "source_url": sanitize_url(url),
                        "target_url": sanitize_url(page.url),
                        "selector": selector,
                        "label": str(control.get("label", "")),
                        "status": "captured",
                        "error": "",
                    }
                html = page.content()
                probe.update(self._dom_probe_summary(html))
                probe["html_file"] = self._save_date_probe_html(html, result)
            except Exception as exc:
                probe = {
                    "kind": kind,
                    "source_url": sanitize_url(url),
                    "target_url": sanitize_url(page.url),
                    "selector": str(control.get("selector", "")),
                    "label": str(control.get("label", "")),
                    "status": "failed",
                    "error": type(exc).__name__,
                    "html_file": "",
                }
            finally:
                self._reconcile_pending_responses(page, result, wait_ms=750)
                try:
                    page.close()
                except Exception:
                    pass
            result.date_probes.append(probe)

    @staticmethod
    def _format_browser_date_value(target: date, hint: str, input_type: str) -> str | None:
        if input_type == "date":
            return target.isoformat()
        value = str(hint).strip()
        if re.search(r"(?:yyyy|\d{4})[-.](?:mm|\d{2})[-.](?:dd|\d{2})", value, re.IGNORECASE):
            separator = "." if "." in value else "-"
            return target.strftime(f"%Y{separator}%m{separator}%d")
        if re.search(r"(?:yyyy|\d{4})/(?:mm|\d{2})/(?:dd|\d{2})", value, re.IGNORECASE):
            return target.strftime("%Y/%m/%d")
        if re.search(r"(?:yyyymmdd|\b\d{8}\b)", value, re.IGNORECASE):
            return target.strftime("%Y%m%d")
        if re.search(r"(?:yyyy|\d{4})년\s*(?:mm|\d{1,2})월\s*(?:dd|\d{1,2})일", value, re.IGNORECASE):
            return target.strftime("%Y년 %m월 %d일")
        return None

    @staticmethod
    def _value_schema(value: Any, *, depth: int = 0) -> Any:
        if depth > 8:
            return {"type": "max-depth"}
        if isinstance(value, dict):
            return {
                "type": "object",
                "fields": {
                    str(key): SiteInspector._value_schema(item, depth=depth + 1)
                    for key, item in list(value.items())[:200]
                },
            }
        if isinstance(value, list):
            return {
                "type": "array",
                "items": SiteInspector._value_schema(value[0], depth=depth + 1)
                if value
                else {"type": "unknown"},
            }
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean"}
        if isinstance(value, (int, float)):
            return {"type": "number"}
        return {"type": "string"}

    def _detect_page_blocker(self, page) -> str:
        try:
            text = page.locator("body").inner_text(timeout=2_000).lower()[:8_000]
        except Exception:
            return ""
        for term in _PAGE_BLOCKER_TERMS:
            if term.lower() in text:
                return term
        return ""

    def _analyze_static_html(
        self,
        context,
        url: str,
        state_id: str,
        reason: str,
        result: InspectionResult,
    ) -> None:
        canonical = _canonical_url(url)
        if canonical in self._static_urls:
            return
        self._static_urls.add(canonical)
        try:
            response = context.request.get(url, timeout=12_000)
            if not response.ok:
                raise RuntimeError(f"HTTP {response.status}")
            html = response.text()
        except Exception as exc:
            result.warnings.append(
                f"{state_id} 정적 보조 분석 실패: {type(exc).__name__}"
            )
            return

        soup = BeautifulSoup(html, "html.parser")
        forms = []
        for form in soup.find_all("form")[:50]:
            fields = []
            for field in form.find_all(("input", "select", "textarea", "button"))[:80]:
                name = str(field.get("name") or field.get("id") or field.get("type") or "").strip()
                if name:
                    fields.append(name)
            forms.append(
                {
                    "action": sanitize_url(urljoin(url, str(form.get("action") or url))),
                    "method": str(form.get("method") or "GET").upper(),
                    "fields": fields,
                }
            )

        selects = []
        for select in soup.find_all("select")[:50]:
            options = []
            for option in select.find_all("option")[:50]:
                value = str(option.get("value") or "").strip()
                label = option.get_text(" ", strip=True)[:120]
                if value or label:
                    options.append({"value": value, "label": label})
            selects.append(
                {
                    "name": str(select.get("name") or select.get("id") or ""),
                    "options": options,
                }
            )

        endpoints = set()
        for match in _ENDPOINT_PATTERN.finditer(html):
            endpoints.add(urljoin(url, match.group(1)))
        for match in _STATIC_ENDPOINT_PATTERN.finditer(html):
            endpoints.add(urljoin(url, match.group(1)))
        for match in _INLINE_ROUTE_PATTERN.finditer(html):
            endpoints.add(urljoin(url, match.group(1)))
        commands = sorted({match.group(1) for match in _STATIC_COMMAND_PATTERN.finditer(html)})
        for endpoint in sorted(endpoints):
            cleaned = sanitize_url(endpoint)
            item = {
                "url": cleaned,
                "category": categorize_endpoint(cleaned),
                "source_script": f"static-html:{sanitize_url(url)}",
            }
            if item not in result.script_endpoints:
                result.script_endpoints.append(item)

        observation = {
            "state_id": state_id,
            "url": sanitize_url(url),
            "reason": reason,
            "forms": forms,
            "selects": selects,
            "external_scripts": [
                sanitize_url(urljoin(url, str(node.get("src"))))
                for node in soup.find_all("script", src=True)[:100]
            ],
            "endpoint_candidates": [sanitize_url(value) for value in sorted(endpoints)],
            "command_candidates": commands,
        }
        result.static_observations.append(observation)
        self._record_forms(forms, state_id, result)
        result.warnings.append(
            f"{state_id} 동적 분석 제한 · {reason} · 공개 HTML 정적 보조 분석 적용"
        )
        self._emit(
            f"정적 보조 분석 · select {len(selects)}개 · form {len(forms)}개 · endpoint {len(endpoints)}개",
            "warning",
        )

    def _enqueue_links(
        self,
        links: list[str],
        depth: int,
        queue: deque[tuple[str, int]],
        queued: set[str],
    ) -> None:
        if depth > self.config.max_depth:
            return
        candidates: list[tuple[int, str]] = []
        for link in links:
            try:
                url = _canonical_url(urljoin(self.config.start_url, str(link)))
            except Exception:
                continue
            if not self._in_scope(url):
                continue
            suffix = urlsplit(url).path.lower()
            if suffix.endswith(_STATIC_ASSET_SUFFIXES):
                continue
            _risk, blocked = classify_request("GET", url)
            if blocked:
                continue
            signature = _crawl_route_signature(url)
            self._discovered_links.setdefault(
                signature, {"url": sanitize_url(url), "depth": depth}
            )
            if url in queued or signature in self._queued_route_signatures:
                continue
            haystack = url.lower()
            score = 1
            if any(term in haystack for term in ("/ct/shop/", "/shop/", "/store/", "/product/")):
                score = -1
            elif any(
                term in haystack
                for term in (
                    "reservation", "reserve", "booking", "calendar", "available",
                    "detail", "view", "rev.", "tickets.", "travel.",
                )
            ):
                score = 0
            elif any(term in haystack for term in ("login", "auth", "member", "account")):
                score = 2
            candidates.append((score, url))
        for _score, url in sorted(candidates, key=lambda item: (item[0], item[1])):
            signature = _crawl_route_signature(url)
            if url in queued or signature in self._queued_route_signatures:
                continue
            queued.add(url)
            self._queued_route_signatures.add(signature)
            queue.append((url, depth))

    def _scan_inline_html(
        self,
        page,
        state_id: str,
        result: InspectionResult,
    ) -> None:
        """Extract route literals from live HTML without persisting inline scripts."""
        try:
            html = page.content()
        except Exception as exc:
            result.warnings.append(f"{state_id} 인라인 경로 추출 실패: {type(exc).__name__}")
            return
        endpoints: set[str] = set()
        for pattern in (_ENDPOINT_PATTERN, _STATIC_ENDPOINT_PATTERN, _INLINE_ROUTE_PATTERN):
            for match in pattern.finditer(html):
                endpoints.add(urljoin(page.url, match.group(1)))
        source = f"inline-html:{state_id}"
        for endpoint in sorted(endpoints):
            if urlsplit(endpoint).path.lower().endswith(_STATIC_ASSET_SUFFIXES):
                continue
            cleaned = sanitize_url(endpoint)
            item = {
                "url": cleaned,
                "category": categorize_endpoint(cleaned),
                "source_script": source,
            }
            already_seen_inline = any(
                existing.get("url") == cleaned
                and str(existing.get("source_script", "")).startswith("inline-html:")
                for existing in result.script_endpoints
            )
            if not already_seen_inline and item not in result.script_endpoints:
                result.script_endpoints.append(item)

    def _enqueue_sitemap(self, context, queue, queued, result: InspectionResult) -> None:
        root = f"{self._origin[0]}://{self._origin[1]}/sitemap.xml"
        sitemap_queue: deque[tuple[str, int]] = deque([(root, 0)])
        seen_sitemaps: set[str] = set()
        page_links: list[str] = []
        max_sitemap_files = 8
        max_page_links = 2_000
        deadline = time.monotonic() + 15.0
        while (
            sitemap_queue
            and len(seen_sitemaps) < max_sitemap_files
            and len(page_links) < max_page_links
            and time.monotonic() < deadline
        ):
            sitemap_url, depth = sitemap_queue.popleft()
            if sitemap_url in seen_sitemaps or depth > 2:
                continue
            seen_sitemaps.add(sitemap_url)
            try:
                remaining_ms = max(500, int((deadline - time.monotonic()) * 1_000))
                response = context.request.get(sitemap_url, timeout=min(3_000, remaining_ms))
                if not response.ok:
                    continue
                text = response.text()
            except Exception:
                continue
            links = [
                urljoin(sitemap_url, unescape(value.strip()))
                for value in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, flags=re.IGNORECASE)
            ]
            child_maps = [value for value in links if urlsplit(value).path.lower().endswith(".xml")]
            pages = [value for value in links if value not in child_maps]
            per_sitemap_limit = 250
            if len(pages) > per_sitemap_limit:
                indexes = {
                    round(index * (len(pages) - 1) / (per_sitemap_limit - 1))
                    for index in range(per_sitemap_limit)
                }
                pages = [pages[index] for index in sorted(indexes)]
            page_links.extend(pages[: max_page_links - len(page_links)])
            if child_maps and depth < 2:
                remaining = max_sitemap_files - len(seen_sitemaps) - len(sitemap_queue)
                if remaining <= 0:
                    continue
                if len(child_maps) > remaining:
                    priority = [
                        value
                        for value in child_maps
                        if any(
                            term in urlsplit(value).path.lower()
                            for term in (
                                "shop", "store", "product", "reservation", "booking", "default"
                            )
                        )
                    ][:remaining]
                    others = [value for value in child_maps if value not in priority]
                    slots = remaining - len(priority)
                    sampled = []
                    if slots > 0 and others:
                        indexes = {
                            round(index * (len(others) - 1) / max(1, slots - 1))
                            for index in range(slots)
                        }
                        sampled = [others[index] for index in sorted(indexes)]
                    child_maps = priority + sampled
                sitemap_queue.extend((value, depth + 1) for value in child_maps[:remaining])
        self._enqueue_links(page_links, 1, queue, queued)
        if seen_sitemaps:
            self._emit(
                f"사이트맵 구조 수집 · XML {len(seen_sitemaps)}개 · 페이지 후보 {len(page_links)}개"
            )

    def _scan_scripts(self, context, scripts: list[str], result: InspectionResult) -> None:
        for script_url in scripts:
            url = _canonical_url(urljoin(self.config.start_url, str(script_url)))
            if url in self._script_urls or not self._in_scope(url):
                continue
            self._script_urls.add(url)
            try:
                response = context.request.get(url, timeout=10_000)
                if not response.ok:
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "javascript" not in content_type and not urlsplit(url).path.endswith((".js", ".mjs")):
                    continue
                body = response.body()
                if len(body) > self.config.script_body_limit:
                    body = body[: self.config.script_body_limit]
                text = body.decode("utf-8", "replace")
            except Exception:
                continue
            candidates: set[str] = set()
            for pattern in (_ENDPOINT_PATTERN, _STATIC_ENDPOINT_PATTERN, _INLINE_ROUTE_PATTERN):
                candidates.update(match.group(1) for match in pattern.finditer(text))
            for candidate in sorted(candidates):
                endpoint = urljoin(url, candidate)
                cleaned = sanitize_url(endpoint)
                item = {
                    "url": cleaned,
                    "category": categorize_endpoint(cleaned),
                    "source_script": sanitize_url(url),
                }
                if item not in result.script_endpoints:
                    result.script_endpoints.append(item)

    def _record_forms(self, forms: list[dict[str, Any]], state_id: str, result: InspectionResult) -> None:
        for form in forms:
            method = str(form.get("method", "GET")).upper()
            action_url = urljoin(self.config.start_url, str(form.get("action", "")))
            risk, blocked = classify_request(method, action_url)
            if blocked:
                fields = [str(value) for value in form.get("fields", []) if str(value).strip()]
                field_summary = ", ".join(fields[:20])
                label = f"{method} {urlsplit(action_url).path}"
                if field_summary:
                    label += f" · 입력필드: {field_summary}"
                result.actions.append(
                    ActionRecord(
                        state_id=state_id,
                        kind="form",
                        label=label,
                        selector="form",
                        risk=risk,
                        outcome="폼 구조만 기록하고 제출하지 않음",
                        resulting_url=sanitize_url(action_url),
                    )
                )
