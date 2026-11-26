"""Run the Search2Revenue AI storefront and its local JSON API."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlsplit

from improvements import ImprovementQueue
from recommendations import PROFILES, companion_items, preferred_platform
from search import SearchEngine, parse_intent
from search_queue import SearchQueue

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
EVENTS_TIMEOUT_SECONDS = 0.25
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/softline-logo.svg": ("softline-logo.svg", "image/svg+xml"),
}
COMBINED = re.compile(r"\s+и\s+(?=(?:защит|антивирус|резервн|бэкап))", re.IGNORECASE)
CONFIGURATOR = re.compile(
    r"astra linux|астра линукс|kaspersky endpoint|касперского", re.IGNORECASE
)
OFFICE_LEGACY = re.compile(r"\b(?:2016|2019|2021)\b")
ANYDESK_SUBSTITUTE = {
    "solo": (
        "Standard",
        "До 20 пользователей вместо одного. Одно одновременное исходящее подключение.",
    ),
    "standard": (
        "Advanced",
        "До 100 пользователей и два одновременных исходящих подключения в базовой лицензии.",
    ),
}


class Service:
    def __init__(
        self,
        engine: SearchEngine,
        log_path: Path,
        *,
        log_diagnostics: bool = True,
        search_queue: SearchQueue | None = None,
        snapshot_cache: bool = False,
    ) -> None:
        self.engine = engine
        self.search_queue = search_queue if search_queue is not None else SearchQueue()
        self._payload = (
            lru_cache(maxsize=256)(self._search_payload)
            if snapshot_cache
            else self._search_payload
        )
        self.log_diagnostics = log_diagnostics
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.log_path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS search_event (
                happened INTEGER NOT NULL, status TEXT NOT NULL, result_count INTEGER NOT NULL,
                recommendation_count INTEGER NOT NULL, elapsed_ms REAL NOT NULL)"""
            )

        self.improvements = ImprovementQueue(engine, log_path)

    def office_alternative(self, query: str, context: str) -> list[dict]:
        tokens, _ = parse_intent(query)
        if not {"microsoft", "office", "дома", "бизнеса"} <= set(tokens) or not (
            {"2016", "2019", "2021"} & set(tokens)
        ):
            return []
        updated, replaced = OFFICE_LEGACY.subn("2024", query)
        if replaced != 1:
            return []
        items = self.engine.search(
            updated, preferred_platform=preferred_platform(query, context)
        )["items"]
        return [
            dict(
                item,
                reason="То же назначение. Версия 2024 вместо запрошенного выпуска.",
            )
            for item in items
            if item["available"] and item["price"] and not item["unknown"]
        ][:1]

    def anydesk_alternative(self, query: str, context: str) -> list[dict]:
        requested = re.fullmatch(
            r"any\s*desk\s+(solo|standard)", query.strip(), re.IGNORECASE
        )
        if not requested:
            return []
        edition, reason = ANYDESK_SUBSTITUTE[requested[1].lower()]
        items = self.engine.search(
            "AnyDesk " + edition,
            preferred_platform=preferred_platform(query, context),
        )["items"]
        return [
            dict(
                item,
                reason=reason,
                source="https://support.anydesk.com/docs/anydesk-licenses",
            )
            for item in items
            if item["available"] and item["price"] and not item["unknown"]
        ][:1]

    def search(self, query: str, *, record: bool = True, context: str = "") -> dict:
        start = time.perf_counter()
        replacement = (
            self.approved_replacement(COMBINED.split(query, maxsplit=1)[0])
            if record
            else None
        )
        result = deepcopy(self._payload(query, context, replacement, record))
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
        if record:
            self.record_search(
                query, result, len(result["items"]), len(result["recommendations"])
            )
        return result

    def _search_payload(
        self, query: str, context: str, replacement: str | None, record: bool
    ) -> dict:
        terms = COMBINED.split(query, maxsplit=1)
        result = self.engine.search(
            terms[0],
            rewrite=replacement,
            preferred_platform=preferred_platform(query, context),
        )
        products = result["items"]
        requested = terms[1] if len(terms) > 1 else ""
        slots = 2 if requested and len(products) >= 2 else 4 - len(products)
        extra = (
            companion_items(
                self.engine, products, query, requested, context, limit=slots
            )
            if record and slots > 0
            else []
        )
        products = products[: 4 - len(extra)]
        n = len(products)
        top_four_count = n + len(extra)
        clarification = result["clarification"]
        if (
            not clarification
            and products
            and CONFIGURATOR.search(query)
            and not result["intent"]["operation"]
        ):
            clarification = "Подбираете новую лицензию или продлеваете действующую?"
        status = (
            "needs_clarification"
            if clarification
            else "no_results"
            if n == 0
            else "limited"
            if top_four_count < 4
            else "ok"
        )
        alternatives = (
            self.office_alternative(terms[0], context)
            or self.anydesk_alternative(terms[0], context)
            if record and not products and not clarification
            else []
        )
        result.update(
            items=products,
            alternatives=alternatives,
            recommendations=extra,
            top_four_count=top_four_count,
            status=status,
            clarification=clarification,
        )
        return result

    def suggest(self, query: str, *, context: str = "") -> dict:
        result = self.search(query, record=False, context=context)
        products = result["items"]
        result["companion_preview"] = None
        if len(query) >= 6 and products:
            terms = COMBINED.split(query, maxsplit=1)
            requested = terms[1] if len(terms) == 2 else ""
            candidates = companion_items(
                self.engine, products, query, requested, context, limit=1
            )
            if candidates:
                result["companion_preview"] = candidates[0]
        return result

    def approved_replacement(self, query: str) -> str | None:
        try:
            return self.improvements.replacement(query)
        except sqlite3.Error:
            LOG.warning("Could not read approved search improvements")
            return None

    def record_search(
        self, query: str, result: dict, count: int, recommendations_count: int
    ) -> None:
        reason = (
            "conflicting_constraints"
            if result["intent"].get("conflict")
            else result.get("failure_reason")
        )
        try:
            if reason and self.log_diagnostics:
                self.improvements.log_failure(query, reason)
            connection = sqlite3.connect(self.log_path, timeout=EVENTS_TIMEOUT_SECONDS)
            with closing(connection) as db, db:
                db.execute(
                    "INSERT INTO search_event VALUES (?,?,?,?,?)",
                    (
                        int(time.time()),
                        result["status"],
                        count,
                        recommendations_count,
                        result["elapsed_ms"],
                    ),
                )
        except sqlite3.Error:
            LOG.warning("Could not record search analytics")

    def analytics(self, *, manager: bool = False) -> dict:
        with closing(sqlite3.connect(self.log_path, timeout=5)) as db:
            counts = dict(
                db.execute(
                    """SELECT CASE WHEN status = 'limited'
                                   AND result_count + recommendation_count >= 4
                                   THEN 'ok' ELSE status END AS outcome, count(*)
                       FROM search_event GROUP BY outcome"""
                )
            )
            totals = db.execute(
                """SELECT count(*), sum(result_count + recommendation_count >= 4),
                   sum(recommendation_count>0),
                   round(avg(elapsed_ms),1) FROM search_event"""
            ).fetchone()
        return {
            **(self.improvements.details() if manager else {}),
            "manager": manager,
            "events": totals[0],
            "four_results": totals[1] or 0,
            "with_recommendations": totals[2] or 0,
            "mean_ms": totals[3] or 0,
            "statuses": counts,
        }


class SearchHandler(BaseHTTPRequestHandler):
    service: Service
    manager_enabled: bool
    server_version = "Search2Revenue"
    protocol_version = "HTTP/1.1"
    timeout = 5

    def send(self, body: bytes, kind: str, code: int = 200) -> None:
        self.send_response(code)
        self.close_connection = True
        self.send_header("Connection", "close")
        if code == 503:
            self.send_header("Retry-After", "1")
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def json(self, data: dict, code: int = 200) -> None:
        self.send(
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def log_message(self, format: str, *args) -> None:
        return

    def manager_host(self) -> str | None:
        if not self.manager_enabled or self.client_address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            return None
        host = self.headers.get("Host", "").lower()
        port = self.connection.getsockname()[1]
        if host in {f"{local}:{port}" for local in ("localhost", "127.0.0.1", "[::1]")}:
            return host
        return None

    def do_POST(self) -> None:
        self.close_connection = True
        if self.path != "/api/improvements/approve":
            self.json({"error": "Страница не найдена"}, 404)
            return
        host = self.manager_host()
        if not host or self.headers.get("Origin") != f"http://{host}":
            self.json({"error": "Одобрение доступно в локальной панели"}, 403)
            return
        if self.headers.get_content_type() != "application/json":
            self.json({"error": "Ожидается JSON"}, 415)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1024:
                raise ValueError("Invalid payload size")
            self.connection.settimeout(2)
            data = json.loads(self.rfile.read(size))
            proposal = data.get("id") if isinstance(data, dict) else None
            if type(proposal) is not int or proposal <= 0:
                raise ValueError("Invalid proposal ID")
        except (ValueError, UnicodeError, TimeoutError):
            self.json({"error": "Некорректный запрос"}, 400)
            return
        if self.service.improvements.approve(proposal):
            self.json({"approved": True})
        else:
            self.json({"error": "Предложение уже обработано или устарело"}, 409)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path in STATIC:
            name, mime = STATIC[url.path]
            self.send((ROOT / "web" / name).read_bytes(), mime)
            return
        if url.path == "/health":
            self.json({"ok": True})
            return
        if url.path == "/api/analytics":
            self.send_analytics()
            return
        if url.path not in {"/api/search", "/api/suggestions"}:
            self.json({"error": "Страница не найдена"}, 404)
            return
        params = parse_qs(url.query)
        query = params.get("q", [""])[0].strip()
        if not query or len(query) > 240:
            self.json({"error": "Введите запрос до 240 символов"}, 400)
            return
        context = params.get("context", [""])[0]
        self.send_search(
            query,
            record=url.path == "/api/search",
            context=context if context in PROFILES else "",
        )

    def send_analytics(self) -> None:
        try:
            result = self.service.analytics(manager=self.manager_host() is not None)
        except sqlite3.Error:
            LOG.warning("Could not read search analytics")
            self.json({"error": "Аналитика временно недоступна."}, 503)
            return
        self.json(result)

    def send_search(self, query: str, *, record: bool, context: str) -> None:
        with self.service.search_queue.slot() as admitted:
            if not admitted:
                self.json(
                    {"error": "Поиск занят. Повторите запрос через секунду."}, 503
                )
                return
            result = (
                self.service.search(query, context=context)
                if record
                else self.service.suggest(query, context=context)
            )
        self.json(result)


class SearchServer(ThreadingHTTPServer):
    request_queue_size = 64


def handler(service: Service, *, manager_enabled: bool = True) -> type[SearchHandler]:
    class ConfiguredHandler(SearchHandler):
        pass

    ConfiguredHandler.service = service
    ConfiguredHandler.manager_enabled = manager_enabled
    return ConfiguredHandler


def improvement_worker(queue: ImprovementQueue, done: Event) -> None:
    while not done.wait(2):
        try:
            queue.run_pending()
        except (sqlite3.Error, OSError):
            LOG.warning("Could not process local search improvements")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    p.add_argument("--events", type=Path, default=Path("data/events.sqlite"))
    p.add_argument(
        "--snapshot-cache",
        action="store_true",
        help="cache results for an immutable catalog until process restart",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    a = p.parse_args()
    if not a.db.is_file():
        p.error("Catalog index missing. Run ingest.py first.")
    manager_enabled = (
        a.host in {"127.0.0.1", "localhost", "::1"}
        and os.getenv("SEARCH_MANAGER", "local") != "off"
    )
    service = Service(
        SearchEngine(a.db),
        a.events,
        log_diagnostics=manager_enabled,
        snapshot_cache=a.snapshot_cache,
    )
    with SearchServer(
        (a.host, a.port), handler(service, manager_enabled=manager_enabled)
    ) as server:
        print(f"Search2Revenue AI: http://{a.host}:{a.port}", flush=True)
        done = Event()
        worker = Thread(
            target=improvement_worker, args=(service.improvements, done), daemon=True
        )
        worker.start()
        try:
            server.serve_forever()
        finally:
            done.set()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
