"""Check every card against separately declared search and companion expectations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from app import Service
from search import SearchEngine, normalize

KSOS = "Kaspersky Small Office Security новая лицензия"
BACKUP = "Новософт Handy Backup (лицензии), стоимость 1 лицензии версии Standard"
NAS = "Сетевое хранилище QNAP 2 disks TS-233"
SOLO = "AnyDesk (лицензия на 1 год), Solo"
SCANNER = "НПО «Эшелон» Сканер-ВС 7, Основная лицензия"
ASTRA = "Astra Linux Special Edition 1.8"
ALT = "BaseALT Альт СП Рабочая станция, релиз 10"
WIN11 = "Microsoft Windows 11 Pro FQC-10572"
CSP_WORKPLACE = "КриптоПро CSP 5.0: годовая лицензия на 1 рабочее место"
CSP_SERVER = "КриптоПро CSP 5.0: серверная лицензия СКЗИ"
CSP_TIMESTAMP = "Лицензия на право использования ПО КриптоПро TSP Client"
CSP_HARDWARE = "Актив «Рутокен ЭЦП Flash», Rutoken (без упаковки), 2.0"
CSP_COMPANIONS = (
    "Актив «Рутокен Lite», Rutoken (без упаковки), 1010",
    CSP_TIMESTAMP,
    CSP_HARDWARE,
)
MAXPATROL = "Positive Technologies MaxPatrol Endpoint Protection Platform"
MAXPATROL_QUOTES = ("MaxPatrol SIEM", "MaxPatrol VM", "PT Sandbox")
ENDPOINT_ALTERNATIVE = (
    "Kaspersky Endpoint Security для бизнеса Стандартный базовая лицензия"
)
MTS_LINK = (
    "МТС Линк (Webinar Group) МТС Линк Стандарт",
    "МТС Линк (Webinar Group) МТС Линк Про",
    "МТС Линк (Webinar Group) МТС Линк Optimum 30",
)


@dataclass(frozen=True)
class Case:
    query: str
    context: str = ""
    direct: tuple[str, ...] = ()
    companions: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()


CASES = (
    Case(
        "касперского для малого офиса", direct=(KSOS,), companions=(BACKUP, SOLO, NAS)
    ),
    Case(
        "Kaspersky Small Office Security без NAS",
        direct=(KSOS,),
        companions=(BACKUP, SOLO),
    ),
    Case(
        "Kaspersky Small Office Security и резервное копирование",
        direct=(KSOS,),
        companions=(BACKUP,),
    ),
    Case("Kaspersky Small Office Security для Mac"),
    Case("лицензия Сканер-ВС 7", direct=(SCANNER,), companions=(ASTRA, ALT, WIN11)),
    Case("Сканер-ВС 7 для российской ОС", direct=(SCANNER,), companions=(ASTRA, ALT)),
    Case("Сканер-ВС для Windows", "windows-office", direct=(SCANNER,)),
    Case(
        "AnyDesk Advanced для Windows",
        "windows-office",
        direct=("AnyDesk (лицензия на 1 год), Advanced",),
        companions=(KSOS, BACKUP),
    ),
    Case("AnyDesk", direct=(SOLO,), companions=(KSOS, BACKUP, WIN11)),
    Case("AnyDesk без ОС", direct=(SOLO,), companions=(KSOS, BACKUP)),
    Case(
        "AnyDesk для Windows 11",
        direct=(SOLO,),
        companions=(KSOS, BACKUP),
    ),
    Case(
        "AnyDesk и резервное копирование на NAS",
        direct=(SOLO,),
        companions=(BACKUP, NAS),
    ),
    Case(
        "AnyDesk и резервное копирование без NAS",
        direct=(SOLO,),
        companions=(BACKUP,),
    ),
    Case("AnyDesk для macOS и резервное копирование на NAS"),
    Case(
        "КриптоПро CSP 5.0 для рабочего места",
        direct=(CSP_WORKPLACE,),
        companions=CSP_COMPANIONS,
    ),
    Case(
        "КриптоПро CSP 5.0",
        "windows-server",
        direct=(CSP_SERVER,),
        companions=CSP_COMPANIONS,
    ),
    Case("КриптоПро CSP 5.0 для macOS", direct=(CSP_WORKPLACE,)),
    Case(
        "КриптоПро CSP 5.0 с неизвлекаемым ключом",
        direct=(CSP_WORKPLACE,),
        companions=(CSP_HARDWARE, CSP_TIMESTAMP),
    ),
    Case(
        "КриптоПро CSP 5.0 без токена",
        direct=(CSP_WORKPLACE,),
        companions=(CSP_TIMESTAMP,),
    ),
    Case(
        "Microsoft 365 не семейная",
        direct=("Microsoft 365 Персональный",),
        companions=(
            "Kaspersky Standard лицензия Base Download Pack Russian Edition",
            BACKUP,
        ),
    ),
    Case(
        "Office 2021 Home and Business Microsoft",
        alternatives=("Microsoft Office для дома и бизнеса 2024",),
    ),
    Case("Microsoft Office 2021 для дома и бизнеса для Linux"),
    Case("max patrol", direct=(MAXPATROL,), companions=MAXPATROL_QUOTES),
    Case("MaxPatrol EPP продление", direct=(MAXPATROL,)),
    Case("MaxPatrol SIEM", direct=("MaxPatrol SIEM",)),
    Case("MaxPatrol VM", direct=("MaxPatrol VM",)),
    Case("PT Sandbox", direct=("PT Sandbox",)),
    Case(
        "MaxPatrol EPP новая лицензия на 150 ПК для Windows 11",
        alternatives=(ENDPOINT_ALTERNATIVE,),
    ),
    Case(
        "MaxPatrol EPP новая лицензия на 150 ПК для Windows",
        alternatives=(ENDPOINT_ALTERNATIVE,),
    ),
    Case(
        "MaxPatrol Endpoint Protection Platform новая лицензия на 200 ПК для Windows 10",
        alternatives=(ENDPOINT_ALTERNATIVE,),
    ),
    Case("MaxPatrol EPP новая лицензия на 500 ПК для Windows 11"),
    Case("MaxPatrol EPP новая лицензия на 150 ПК для macOS"),
    Case(
        "Microsoft Office для дома и бизнеса, антивирус уже есть",
        direct=("Microsoft Office для дома и бизнеса 2024",),
        companions=(BACKUP, SOLO),
    ),
    Case("мтс", direct=MTS_LINK, companions=("Гарнитура Logitech H540",)),
    Case("МТС Линк без гарнитуры", direct=MTS_LINK),
    Case("МТС Линк только лицензия", direct=MTS_LINK),
)


def assessment(
    kind: str, items: Sequence[dict], expected: tuple[str, ...], seen: set[str]
) -> tuple[list[dict], list[str]]:
    remaining = list(expected)
    checked = []
    for item in items:
        name = normalize(item["name"])
        match = next(
            (prefix for prefix in remaining if name.startswith(normalize(prefix))), None
        )
        evidence = [fact["url"] for fact in item["evidence"] if fact["value"]]
        evidenced = kind != "recommendation" or bool(item.get("source") and evidence)
        distinct = item["group"] not in seen
        if match:
            remaining.remove(match)
        seen.add(item["group"])
        checked.append(
            {
                "type": kind,
                "name": item["name"],
                "sku": item["sku"],
                "group": item["group"],
                "expected_family": match,
                "evidence_urls": sorted(set(evidence)),
                "accepted": bool(match and evidenced and distinct),
            }
        )
    return checked, remaining


def check(service: Service, case: Case) -> dict:
    response = service.search(case.query, context=case.context)
    seen: set[str] = set()
    cards = []
    missing = []
    for key, kind, expected in (
        ("items", "direct", case.direct),
        ("recommendations", "recommendation", case.companions),
        ("alternatives", "alternative", case.alternatives),
    ):
        judged, omitted = assessment(kind, response[key], expected, seen)
        cards.extend(judged)
        missing.extend(omitted)
    return {
        "query": case.query,
        "profile": case.context,
        "cards": cards,
        "missing": missing,
        "passed": not missing and all(card["accepted"] for card in cards),
    }


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    cli.add_argument("--out", type=Path, default=Path("reports/companion-quality.json"))
    args = cli.parse_args()
    with TemporaryDirectory() as work:
        service = Service(
            SearchEngine(args.db), Path(work) / "events.sqlite", log_diagnostics=False
        )
        rows = [check(service, case) for case in CASES]
    cards = [card for row in rows for card in row["cards"]]
    summary = {"cases": len(rows), "passed": sum(row["passed"] for row in rows)}
    for kind in ("direct", "recommendation", "alternative"):
        selected = [card for card in cards if card["type"] == kind]
        summary[kind] = {
            "returned": len(selected),
            "accepted": sum(card["accepted"] for card in selected),
        }
    args.out.parent.mkdir(exist_ok=True, parents=True)
    args.out.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(summary, flush=True)
    return int(summary["passed"] != summary["cases"])


if __name__ == "__main__":
    raise SystemExit(main())
