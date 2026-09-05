from __future__ import annotations

import hashlib
import json
import re
import ipaddress
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup


SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "pin",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "jwt",
    "api_key",
    "apikey",
    "cardnumber",
    "card_number",
    "cvv",
    "cvc",
    "residentnumber",
    "name",
    "username",
    "user_name",
    "email",
    "e_mail",
    "phone",
    "mobile",
    "tel",
    "telephone",
    "address",
    "birth",
    "birthday",
}

EXACT_FINAL_ACTION_LABELS = {
    "예약",
    "예약하기",
    "예약 신청",
    "예매",
    "예매하기",
    "결제하기",
    "주문하기",
    "구매하기",
    "book now",
    "reserve now",
    "pay now",
    "checkout",
}

FINAL_ACTION_TERMS = (
    "결제",
    "구매",
    "주문",
    "예약완료",
    "예약 완료",
    "예약확정",
    "예약 확정",
    "최종예약",
    "최종 예약",
    "취소하기",
    "삭제",
    "탈퇴",
    "logout",
    "withdraw",
)

NON_EXPLORATION_CONTROL_TERMS = (
    "뒤로가기",
    "뒤로 가기",
    "back",
    "next slide",
    "previous slide",
    "본문 바로가기",
    "주요메뉴 바로가기",
    "메인페이지로 이동",
    "전체메뉴",
    "전체 메뉴",
    "맨 위로",
    "맨위로",
)

UNAVAILABLE_ACTION_TERMS = (
    "예약불가",
    "예약 불가",
    "예매불가",
    "예매 불가",
    "매진",
    "마감",
    "sold out",
    "unavailable",
)

PREFERENCE_ACTION_TERMS = (
    "오늘은 그만 보기",
    "오늘 하루 열지 않기",
    "오늘하루열지 않기",
    "다시 보지 않기",
    "don't show again",
    "popupcheck",
)

MUTATION_PATH_TERMS = (
    "/book",
    "/booking",
    "/reserve",
    "/reservation",
    "/order",
    "/checkout",
    "/payment",
    "/pay",
    "/purchase",
    "/cancel",
    "/delete",
    "/withdraw",
    "/logout",
    "/refund",
)

HARD_MUTATION_PATH_TERMS = (
    "/order",
    "/checkout",
    "/payment",
    "/pay",
    "/purchase",
    "/cancel",
    "/delete",
    "/withdraw",
    "/logout",
    "/refund",
)

WRITE_HANDLER_TERMS = (
    "create",
    "submit",
    "confirm",
    "complete",
    "insert",
    "update",
    "save",
    "apply",
    "register",
    "execute",
)

READ_POST_TERMS = (
    "availability",
    "available",
    "calendar",
    "calculate",
    "estimate",
    "quote",
    "search",
    "query",
    "lookup",
    "validate",
    "verify",
    "check",
    "list",
    "catalog",
)

# Imweb renders booking calendars through POST handlers whose path contains
# ``/booking`` even though they only return HTML fragments.  Keep this an exact
# allow-list so a real order mutation such as ``booking/add_order.cm`` remains
# blocked.
IMWEB_READ_POST_PATHS = {
    "/booking/html_day_booking.cm",
    "/booking/html_day_detail.cm",
    "/booking/html_detail_calendar.cm",
    "/booking/html_list.cm",
    "/booking/html_mfe_list.cm",
}

SAFE_INFRA_POST_TERMS = (
    "/cdn-cgi/challenge-platform/",
    "/boot",
    "/generate-track-id",
    "/display-callouts/slots",
    "/analytics/collect",
    "/telemetry",
)

_STATIC_ASSET_SUFFIXES = (
    ".css", ".js", ".mjs", ".map", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip",
    ".mp4", ".mp3",
)

_COUNTRY_SECOND_LEVELS = {
    "ac", "co", "com", "ed", "edu", "go", "gov", "gr", "mil", "ne", "net", "or", "org", "re",
}


def site_scope_key(url_or_host: str) -> str:
    """Return a conservative registrable-site key for sibling-subdomain crawling."""
    parsed = urlsplit(url_or_host if "://" in url_or_host else f"https://{url_or_host}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _COUNTRY_SECOND_LEVELS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(left: str, right: str) -> bool:
    left_key = site_scope_key(left)
    return bool(left_key and left_key == site_scope_key(right))


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", value.lower())


def is_sensitive_key(value: str) -> bool:
    key = _normalise_key(value)
    return key in SENSITIVE_KEYS or any(
        term in key
        for term in (
            "password", "secret", "token", "cookie", "encrypted", "signature",
            "credential", "sessionid", "nonce",
        )
    )


def is_opaque_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 64 or any(char.isspace() for char in text):
        return False
    if text.count(".") == 2 and all(len(part) >= 8 for part in text.split(".")):
        return True
    return bool(
        re.fullmatch(r"[A-Za-z0-9+/=_-]{64,}", text)
        or re.fullmatch(r"[0-9a-fA-F]{64,}", text)
    )


def redact_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]
    return f"[REDACTED:{digest}:len={len(text)}]"


def sanitize_mapping(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return "[MAX_DEPTH]"
    if isinstance(value, Mapping):
        return {
            str(key): redact_scalar(item) if is_sensitive_key(str(key)) else sanitize_mapping(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_mapping(item, depth=depth + 1) for item in value]
    if is_opaque_secret(value):
        return redact_scalar(value)
    return value


def sanitize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {
        str(key): str(redact_scalar(value) if is_sensitive_key(str(key)) else value)
        for key, value in headers.items()
    }


def sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((key, str(redact_scalar(value)) if is_sensitive_key(key) else value))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def sanitize_body(body: str | bytes | None) -> Any:
    if body is None:
        return None
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    text = body.strip()
    if not text:
        return ""
    try:
        return sanitize_mapping(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        pass
    pairs = parse_qsl(text, keep_blank_values=True)
    if pairs and "=" in text:
        return {
            key: redact_scalar(value) if is_sensitive_key(key) else sanitize_mapping(value)
            for key, value in pairs
        }
    if len(text) > 2048:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
        return f"[TEXT:{digest}:len={len(text)}]"
    return text


def sanitize_html_snapshot(html: str) -> str:
    """Keep page structure while removing form values and inline executable data."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(("input", "textarea")):
        if node.has_attr("value"):
            node["value"] = "[REDACTED]"
        if node.name == "textarea":
            node.clear()
            node.append("[REDACTED]")
    for node in soup.find_all("script"):
        if not node.get("src"):
            node.clear()
            node["data-site-inspector"] = "inline-content-redacted"
    for node in soup.find_all("meta"):
        name = str(node.get("name") or node.get("property") or "")
        if is_sensitive_key(name) and node.has_attr("content"):
            node["content"] = "[REDACTED]"
    return str(soup)


def classify_action(
    label: str,
    kind: str,
    href: str = "",
    context: str = "",
) -> str:
    haystack = f"{label} {href}".lower()
    if label.strip().lower() in EXACT_FINAL_ACTION_LABELS:
        if (
            kind == "button"
            and label.strip().lower() in {"예약", "예매", "book", "reserve"}
            and re.search(r"예약\s*가능|\bavailable\b", context, re.IGNORECASE)
            and not any(
                term in href.lower()
                for term in (
                    "rev.act", "add_order", "create", "submit", "confirm",
                    "payment", "checkout",
                )
            )
        ):
            # Modern booking calendars often label the harmless slot-detail
            # opener simply "예약". The network firewall remains authoritative
            # and still blocks any order-creation request the click may trigger.
            return "explorable"
        if re.search(
            r"detail|view|selectreservview|상세|/shop/|/product/|/goods/",
            href,
            re.IGNORECASE,
        ) and not any(
            term in href.lower()
            for term in (
                "rev.act", "add_order", "create", "submit", "confirm",
                "payment", "checkout",
            )
        ):
            return "explorable"
        return "blocked-final-action"
    if any(term.lower() in haystack for term in FINAL_ACTION_TERMS):
        return "blocked-final-action"
    if kind in {"password", "file"}:
        return "blocked-sensitive-input"
    if kind in {"button", "link"} and not label.strip():
        return "skipped-unlabelled-control"
    if kind == "button" and label.strip().isdigit():
        return "skipped-pagination-control"
    if kind == "button" and label.strip() in {"첫번째", "두번째", "세번째", "네번째", "다섯번째"}:
        return "skipped-carousel-control"
    if any(term.lower() in haystack for term in UNAVAILABLE_ACTION_TERMS):
        return "skipped-unavailable-control"
    if any(term.lower() in haystack for term in PREFERENCE_ACTION_TERMS):
        return "skipped-preference-control"
    if kind == "button" and (
        label.strip().lower() == "top"
        or any(term.lower() in label.lower() for term in NON_EXPLORATION_CONTROL_TERMS)
    ):
        return "skipped-navigation-control"
    return "explorable"


def classify_request(method: str, url: str, body: str | None = None) -> tuple[str, bool]:
    method = method.upper()
    parsed = urlsplit(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    text = f"{path} {body or ''}".lower()
    if method in {"GET", "HEAD", "OPTIONS"}:
        # Some legacy sites expose state-changing actions as GET links.
        # Do not visit those while inventorying a site.
        if path.endswith(_STATIC_ASSET_SUFFIXES):
            return "read", False
        unsafe_terms = {"cancel", "delete", "remove", "withdraw", "logout", "refund"}
        segments = [value for value in path.split("/") if value]
        stem = segments[-1].rsplit(".", 1)[0] if segments else ""
        handler_words = set(re.findall(r"[a-z]+", stem))
        unsafe_query = any(
            key.lower() in {"action", "act", "cmd", "mode", "op"}
            and value.lower() in unsafe_terms
            for key, value in parse_qsl(query, keep_blank_values=True)
        )
        if handler_words & unsafe_terms or unsafe_query:
            return "blocked-unsafe-get", True
        return "read", False
    if "graphql" in path:
        if re.search(r"\bmutation\b", body or "", re.IGNORECASE):
            return "blocked-mutation", True
        if re.search(r"\bquery\b", body or "", re.IGNORECASE):
            return "read-post", False
    if path in IMWEB_READ_POST_PATHS:
        return "read-post", False
    if any(term in path for term in SAFE_INFRA_POST_TERMS) and not any(
        term in path for term in MUTATION_PATH_TERMS
    ):
        return "safe-infrastructure-post", False
    explicit_read_suffix = any(
        term in path
        for term in (
            "/calculate",
            "/estimate",
            "/quote",
            "/preview",
            "/availability",
            "/available",
            "/validate",
            "/verify",
            "/check",
            "/search",
            "/related-keywords",
            "/time-slots",
            "/slots",
            "/near-full",
        )
    )
    named_read_handler = bool(
        re.search(
            r"/(?:page)?(?:select|search|get|fetch|load|list|check)[a-z0-9_.-]*(?:/|$)",
            path,
        )
    ) and not any(term in path for term in HARD_MUTATION_PATH_TERMS) and not any(
        term in path.rsplit("/", 1)[-1] for term in WRITE_HANDLER_TERMS
    )
    display_read_handler = "/display/" in path and not any(
        term in path for term in HARD_MUTATION_PATH_TERMS
    ) and not any(term in path.rsplit("/", 1)[-1] for term in WRITE_HANDLER_TERMS)
    filter_read_handler = "/filters/" in path and not any(
        term in path for term in HARD_MUTATION_PATH_TERMS
    ) and not any(term in path.rsplit("/", 1)[-1] for term in WRITE_HANDLER_TERMS)
    if explicit_read_suffix or named_read_handler or display_read_handler or filter_read_handler or (
        any(term in text for term in READ_POST_TERMS)
        and not any(term in path for term in MUTATION_PATH_TERMS)
    ):
        return "read-post", False
    if any(term in path for term in MUTATION_PATH_TERMS):
        return "blocked-mutation", True
    return "blocked-unknown-write", True


def categorize_endpoint(url: str) -> str:
    parsed = urlsplit(url)
    path = f"{parsed.netloc}{parsed.path}?{parsed.query}".lower()
    groups = (
        ("telemetry", ("/envelope/", "log_event", "analytics", "telemetry", "/collect", "google.internal.waa", "/2/httpapi", "amplitude.com")),
        ("payment", ("payment", "pay", "checkout")),
        ("calculate", ("calculate", "estimate", "quote", "price")),
        (
            "reservation",
            ("booking", "book", "reserve", "reservation", "order", "rev.make", "rev.main"),
        ),
        ("availability", ("availability", "available", "calendar", "inventory", "slot")),
        ("auth", ("auth", "login", "token", "session", "verify", "rev.login")),
        ("result", ("result", "status", "history", "detail")),
        ("catalog", ("camp", "zone", "site", "theme", "product", "branch", "store")),
    )
    for category, terms in groups:
        if any(term in path for term in terms):
            return category
    return "other"
