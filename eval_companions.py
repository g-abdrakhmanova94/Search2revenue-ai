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
    Case("лицензия Сканер-ВС 7", direct=(SCANNER,), companions=(ASTRA,)),
    Case("Сканер-ВС для Windows", "windows-office", direct=(SCANNER,)),
    Case(
        "AnyDesk Advanced для Windows",
        "windows-office",
        direct=("AnyDesk (лицензия на 1 год), Advanced",),
        companions=(KSOS, BACKUP),
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
