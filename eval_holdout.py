"""Evaluate every returned position on a separate set of catalog queries."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from search import SearchEngine, normalize


class EvaluationRow(TypedDict):
    query: str
    returned: int
    relevant: int
    judgments: list[dict[str, str | bool]]


@dataclass(frozen=True)
class Case:
    query: str
    category_fragments: tuple[str, ...]
    name_fragments: tuple[str, ...] = ()
    confirmed_fragments: tuple[str, ...] = ()

    def relevant(self, item: dict) -> bool:
        category = normalize(item["category"])
        title = normalize(item["name"])
        established = " ".join(item["confirmed"])
        return (
            any(fragment in category for fragment in self.category_fragments)
            and (
                not self.name_fragments
                or any(fragment in title for fragment in self.name_fragments)
            )
            and all(fragment in established for fragment in self.confirmed_fragments)
        )


# Labels were written before running these queries. Every position is judged
# independently. The labels are product/category and constraint checks, not
# expert judgments of exact customer fit.
CASES = (
    Case(
        "стоечный сервер",
        ("rack-сервер", "стоечн", "серверное оборудование"),
        ("сервер",),
    ),
    Case("оперативная память DDR4", ("память",), ("ddr4",)),
    Case("SSD для компьютера", ("ssd",), ("ssd",)),
    Case("ноутбук Lenovo", ("ноутбук",), ("lenovo",)),
    Case("монитор Samsung", ("монитор",), ("samsung",)),
    Case("лазерный принтер HP", ("принтер", "мфу"), ("laser", "лазер")),
    Case("источник бесперебойного питания APC", ("ибп", "бесперебой"), ("apc",)),
    Case("гигабитный сетевой коммутатор", ("коммутатор",), ("коммутатор", "switch")),
    Case("роутер MikroTik", ("маршрутизатор", "роутер"), ("mikrotik", "микротик")),
    Case("офисный пакет Microsoft Office 2024", ("офис",), ("office",)),
    Case(
        "продлить расширенный Kaspersky на 250 ПК",
        ("антивирус",),
        ("kaspersky",),
        ("Тип покупки подтверждён", "включает 250"),
    ),
    Case(
        "лицензия 1С Управление автотранспортом Проф на 20 мест без USB",
        ("автоматизац",),
        ("автотранспортом",),
        ("Формат поставки", "включает 20"),
    ),
)


# Written after fixing the first set but before executing any of these queries.
VALIDATION = (
    Case("NAS Synology для офиса", ("сетев", "nas"), ("synology",)),
    Case("34-дюймовый монитор для работы", ("монитор",), ("34",)),
    Case("HDD Seagate 4 ТБ", ("жестк", "hdd"), ("seagate",)),
    Case("сетевой свитч 1GbE", ("коммутатор",), ("коммутатор", "switch")),
    Case("планшет Samsung Galaxy", ("планшет",), ("samsung",)),
    Case("Внешний SSD USB", ("внешн",), ("ssd",)),
    Case(
        "Kaspersky Endpoint Security расширенный продление на 499 компьютеров",
        ("антивирус",),
        ("kaspersky",),
        ("Тип покупки подтверждён", "включает 499"),
    ),
    Case("электронный Microsoft Office 2024 для дома и работы", ("офис",), ("office",)),
)


# This final split is reserved for reporting and is not used to tune ranking.
FINAL = (
    Case("сервер rack для стойки", ("rack-сервер", "стоечн"), ("сервер",)),
    Case("коммутатор 1GbE", ("коммутатор",), ("коммутатор", "switch")),
    Case("Kingston память DDR4", ("память",), ("kingston",)),
    Case("сетевое хранилище QNAP", ("сетев", "nas"), ("qnap",)),
    Case("монитор Dell для офиса", ("монитор",), ("dell",)),
    Case("ИБП APC Smart UPS", ("ибп",), ("smart-ups", "smart ups")),
    Case(
        "Kaspersky Endpoint Security для бизнеса продление на 150 рабочих мест",
        ("антивирус",),
        ("kaspersky",),
        ("Тип покупки подтверждён", "включает 150"),
    ),
    Case(
        "Microsoft Office 2024 для дома и бизнеса бессрочный",
        ("офис",),
        ("office для дома и бизнеса 2024", "office home & business 2024"),
    ),
)


def measure(engine: SearchEngine, cases: tuple[Case, ...] = CASES) -> dict:
    rows: list[EvaluationRow] = []
    for case in cases:
        result = engine.search(case.query)
        items = result["items"]
        judgments: list[dict[str, str | bool]] = [
            {
                "sku": item["sku"],
                "name": item["name"],
                "category": item["category"],
                "relevant": case.relevant(item),
            }
            for item in items
        ]
        rows.append(
            {
                "query": case.query,
                "returned": len(items),
                "relevant": sum(1 for item in judgments if item["relevant"] is True),
                "judgments": judgments,
            }
        )
    return {
        "queries": len(rows),
        "four_returned": sum(row["returned"] == 4 for row in rows),
        "four_relevant": sum(row["relevant"] == 4 for row in rows),
        "relevant_slots": sum(row["relevant"] for row in rows),
        "slots": 4 * len(rows),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument(
        "--out", type=Path, default=Path("reports/pilot-relevance.json")
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--validation", action="store_true")
    group.add_argument("--final", action="store_true")
    args = parser.parse_args()
    cases = FINAL if args.final else VALIDATION if args.validation else CASES
    report = measure(SearchEngine(args.db), cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print({k: v for k, v in report.items() if k != "rows"})
    for row in report["rows"]:
        print(row["query"], row["returned"], row["relevant"])


if __name__ == "__main__":
    main()
