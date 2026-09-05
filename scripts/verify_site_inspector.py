from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.site_inspector import InspectorConfig, SiteInspector


HTML = b"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Inspector Fixture</title></head>
<body>
  <h1>Test Camp</h1>
  <a href="/details">camp details</a>
  <select id="site"><option value="">site</option><option value="A">A site</option></select>
  <input id="date" type="date" min="2030-01-01">
  <button id="calculate">price calculate</button>
  <button id="reserve">reservation test</button>
  <button id="pay">pay now</button>
  <pre id="out"></pre>
  <script src="/app.js"></script>
</body></html>"""

SCRIPT = b"""
const API = {
  availability: '/api/availability',
  calculate: '/api/booking/calculate',
  finalBook: '/v1/book',
  payment: '/payments/confirm'
};
fetch(API.availability).then(r => r.json()).then(x => out.textContent = JSON.stringify(x));
const outputEl = document.getElementById('out');
const siteEl = document.getElementById('site');
const widget = document.createElement('booking-widget');
widget.attachShadow({mode: 'open'}).innerHTML = `
  <button id="shadow-availability">shadow availability</button>
  <button id="shadow-reserve">&#50696;&#50557;&#54616;&#44592;</button>
`;
document.body.appendChild(widget);
document.getElementById('calculate').addEventListener('click', () =>
  fetch(API.calculate, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({siteId:siteEl.value || 'A'})})
    .then(r => r.json()).then(x => outputEl.textContent = JSON.stringify(x))
);
document.getElementById('reserve').addEventListener('click', () =>
  fetch(API.finalBook, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({siteId:siteEl.value || 'A'})})
);
document.getElementById('pay').addEventListener('click', () =>
  fetch(API.payment, {method:'POST', body:'bookingId=1'})
);
"""


class FixtureHandler(BaseHTTPRequestHandler):
    book_calls = 0
    calculation_calls = 0
    payment_calls = 0

    def log_message(self, *_args):
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in {"/", "/details"}:
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/sitemap.xml":
            self._send(
                200,
                b'<?xml version="1.0"?><sitemapindex><sitemap><loc>/nested-sitemap.xml</loc></sitemap></sitemapindex>',
                "application/xml",
            )
        elif self.path == "/nested-sitemap.xml":
            body = f'<?xml version="1.0"?><urlset><url><loc>http://127.0.0.1:{self.server.server_port}/details</loc></url></urlset>'.encode()
            self._send(200, body, "application/xml")
        elif self.path == "/app.js":
            self._send(200, SCRIPT, "application/javascript")
        elif self.path == "/api/availability":
            self._send(
                200,
                json.dumps({"sites": ["A"], "token": "must-be-redacted"}).encode(),
                "application/json",
            )
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        self.rfile.read(length)
        if self.path == "/api/booking/calculate":
            type(self).calculation_calls += 1
            self._send(200, b'{"price":50000}', "application/json")
            return
        if self.path == "/v1/book":
            type(self).book_calls += 1
            self._send(201, b'{"bookingId":"unsafe"}', "application/json")
            return
        if self.path == "/payments/confirm":
            type(self).payment_calls += 1
            self._send(201, b'{"paymentId":"unsafe"}', "application/json")
            return
        self._send(200, b"{}", "application/json")


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="site-inspector-smoke-") as temp_dir:
            url = f"http://127.0.0.1:{server.server_port}/"
            result = SiteInspector(
                InspectorConfig(
                    start_url=url,
                    output_root=Path(temp_dir),
                    max_pages=6,
                    max_actions_per_page=6,
                    max_depth=1,
                    manual_intervention_timeout_seconds=0,
                ),
                log=lambda message, level="info": print(f"[{level}] {message}"),
            ).run()
            print(json.dumps({"warnings": result.warnings}, ensure_ascii=False))
            print(
                json.dumps(
                    [
                        {"label": item.label, "risk": item.risk, "outcome": item.outcome}
                        for item in result.actions
                    ],
                    ensure_ascii=False,
                )
            )
            assert result.states, "no states captured"
            availability = [item for item in result.network if "/api/availability" in item.url]
            calculations = [item for item in result.network if "/api/booking/calculate" in item.url]
            bookings = [item for item in result.network if "/v1/book" in item.url]
            print(
                json.dumps(
                    [
                        {
                            "method": item.method,
                            "path": item.url,
                            "status": item.status,
                            "blocked": item.blocked,
                            "error": item.error,
                        }
                        for item in result.network
                        if item.resource_type in {"xhr", "fetch"}
                    ],
                    ensure_ascii=False,
                )
            )
            assert any(item.status == 200 and not item.blocked for item in availability)
            assert any(item.status == 200 and not item.blocked for item in calculations)
            assert any(item.blocked for item in bookings)
            assert FixtureHandler.calculation_calls > 0, "read-only calculation did not run"
            assert FixtureHandler.book_calls == 0, "mutation firewall allowed final booking"
            assert FixtureHandler.payment_calls == 0, "mutation firewall allowed payment"
            assert (result.output_dir / "report.md").exists()
            assert (result.output_dir / "engine_spec.json").exists()
            assert (result.output_dir / "engine_blueprint.json").exists()
            assert (result.output_dir / "site_structure.json").exists()
            assert (result.output_dir / "crawl_frontier.json").exists()
            assert (result.output_dir / "request_schemas.json").exists()
            assert result.dom_inventory, "DOM inventory was not captured"
            assert any(
                item.get("shadow_roots", 0) > 0 for item in result.dom_inventory
            ), "open shadow roots were not traversed"
            assert any(
                control.get("label") == "shadow availability"
                for item in result.dom_inventory
                for control in item.get("controls", [])
            ), "shadow DOM controls were not inventoried"
            assert any(
                item.label == "예약하기" and item.risk == "blocked-final-action"
                for item in result.actions
            ), "shadow DOM final action was not blocked"
            assert result.date_probes, "future date DOM probe did not run"
            assert not any(state.url.endswith(".xml") for state in result.states), "sitemap XML was visited as a page"
            inspection_text = (result.output_dir / "inspection.json").read_text(encoding="utf-8")
            assert "must-be-redacted" not in inspection_text
            print(
                json.dumps(
                    {
                        "states": len(result.states),
                        "actions": len(result.actions),
                        "network": len(result.network),
                        "blocked": sum(1 for item in result.network if item.blocked),
                        "calculation_calls": FixtureHandler.calculation_calls,
                        "book_calls": FixtureHandler.book_calls,
                        "payment_calls": FixtureHandler.payment_calls,
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
