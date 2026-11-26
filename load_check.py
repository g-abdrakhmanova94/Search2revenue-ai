"""Measure the local HTTP service with an isolated analytics database."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from app import SearchServer, Service, handler
from search import SearchEngine

WORKLOAD = (
    ("/api/search", "Astra Linux SE для рабочих станций"),
    ("/api/search", "Касперского расширенного продлить на 200 ПК"),
    ("/api/search", "1С Автотранспорт Проф на 20 мест без USB"),
    ("/api/search", "сервак"),
    ("/api/search", "Астра Линукс и защита рабочих мест"),
    ("/api/search", "Kaspersky продление на 200 ПК или 300 ПК"),
    ("/api/search", "KL4867RASFR"),
    ("/api/search", "резервное копирование cyberprotect"),
    ("/api/suggestions", "astra linux"),
    ("/api/suggestions", "майкро"),
)


def request(port: int, job: tuple[str, str]) -> tuple[str, int, float]:
    route, query = job
    started = time.perf_counter()
    connection = HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", route + "?" + urlencode({"q": query}))
        with connection.getresponse() as response:
            payload = json.load(response)
            if (
                response.status == 503
                and response.getheader("Retry-After") == "1"
                and isinstance(payload.get("error"), str)
            ):
                return route, response.status, (time.perf_counter() - started) * 1000
            if response.status != 200:
                raise RuntimeError(f"Unexpected HTTP status: {response.status}")
    finally:
        connection.close()
    if not isinstance(payload.get("items"), list) or not isinstance(
        payload.get("status"), str
    ):
        raise TypeError("Unexpected search response")
    return route, 200, (time.perf_counter() - started) * 1000


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"requests": 0}
    samples = sorted(values)
    return {
        "requests": len(samples),
        "median_ms": round(statistics.median(samples), 1),
        "p95_ms": round(samples[math.ceil(0.95 * len(samples)) - 1], 1),
        "max_ms": round(samples[-1], 1),
    }


def measure(port: int, repeats: int, concurrency: int) -> dict:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(pool.map(lambda job: request(port, job), WORKLOAD * repeats))
    elapsed = time.perf_counter() - started
    served = [(route, duration) for route, status, duration in samples if status == 200]
    return {
        "concurrency": concurrency,
        "requests_per_second": round(len(samples) / elapsed, 1),
        "successful_per_second": round(len(served) / elapsed, 1),
        "all": summarize([duration for _, duration in served]),
        "busy": {
            path: summarize(
                [d for route, status, d in samples if route == path and status == 503]
            )
            for path in ("/api/search", "/api/suggestions")
        },
        "search": summarize(
            [duration for route, duration in served if route == "/api/search"]
        ),
        "suggestions": summarize(
            [duration for route, duration in served if route == "/api/suggestions"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument(
        "--port", type=int, help="check an existing server on 127.0.0.1"
    )
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 8])
    args = parser.parse_args()
    if (args.port is None and not args.db.is_file()) or args.repeats < 1:
        parser.error(
            "Provide a local port or existing database and a positive number of repeats"
        )
    if any(level < 1 for level in args.concurrency):
        parser.error("Concurrency must be positive")

    if args.port is not None:
        if not 1 <= args.port <= 65535:
            parser.error("Port must be between 1 and 65535")
        reports = [measure(args.port, args.repeats, n) for n in args.concurrency]
        print(json.dumps(reports, indent=2))
        return

    with TemporaryDirectory() as tmp:
        service = Service(SearchEngine(args.db), Path(tmp) / "events.sqlite")
        with SearchServer(("127.0.0.1", 0), handler(service)) as server:
            port = server.server_port
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for job in WORKLOAD:
                    if request(port, job)[1] != 200:
                        raise RuntimeError("Server rejected a warm-up request")
                reports = [
                    measure(port, args.repeats, concurrency)
                    for concurrency in args.concurrency
                ]
                completed_searches = sum(row["search"]["requests"] for row in reports)
                if service.analytics()["events"] != 8 + completed_searches:
                    raise RuntimeError("Unexpected number of recorded search events")
                if request(port, WORKLOAD[0])[1] != 200:
                    raise RuntimeError("Server did not recover after the load test")
            finally:
                server.shutdown()
                worker.join()
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
