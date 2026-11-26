"""Offline product-family and paraphrase evaluation on the Softline feed."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from app import Service
from evaluate import cases, family_groups
from search import SearchEngine

PARAPHRASES = [
    ("астра линукс спец редакция", "/astralinux/astra-linux-special-edition"),
    ("Astra Linux SE для рабочих станций", "/astralinux/astra-linux-special-edition"),
    ("лицензия астра линукс смоленск", "/astralinux/astra-linux-special-edition"),
    ("защищенная астра линукс", "/astralinux/astra-linux-special-edition"),
    ("Astra Linxu special edition", "/astralinux/astra-linux-special-edition"),
    (
        "офис майкрософт для дома и бизнеса 2024",
        "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024",
    ),
    (
        "office 2024 home and business microsoft",
        "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024",
    ),
    (
        "microsoft office домой и на работу 2024",
        "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024",
    ),
    (
        "майкрософт офис дом и бизнес 2024",
        "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024",
    ),
    (
        "micosoft office для дома и бизнеса",
        "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024",
    ),
    ("касперский для малого офиса", "/kaspersky/kaspersky-small-office-security"),
    ("kasperski small office", "/kaspersky/kaspersky-small-office-security"),
    ("small office security касперского", "/kaspersky/kaspersky-small-office-security"),
    (
        "лицензия касперский small office security",
        "/kaspersky/kaspersky-small-office-security",
    ),
    (
        "продлить kaspersky small office security на 5 пк",
        "/kaspersky/kaspersky-small-office-security",
    ),
    ("криптопро цсп версия 5", "/kripto-pro/cryptopro-csp"),
    ("CryptoPro CSP для электронной подписи", "/kripto-pro/cryptopro-csp"),
    ("крипто про csp 5 для эцп", "/kripto-pro/cryptopro-csp"),
    ("криптопрo csp 5.0", "/kripto-pro/cryptopro-csp"),
    ("СКЗИ КриптоПро CSP", "/kripto-pro/cryptopro-csp"),
    ("ани деск", "/anydesk/anydesk"),
    ("удаленный доступ any desk", "/anydesk/anydesk"),
    ("андеск anydesk лицензия", "/anydesk/anydesk"),
    ("anydsek для удаленки", "/anydesk/anydesk"),
    ("AnyDesk Standard", "/anydesk/anydesk"),
    ("кибербэкап от киберпротект", "/cyberprotect"),
    ("киберпротект бэкап", "/cyberprotect"),
    ("кибер бекап для резервного копирования", "/cyberprotect"),
    ("кибер бэкап стандартная редакция", "/cyberprotect"),
    ("резервное копирование cyberprotect", "/cyberprotect"),
    ("эшелон сканер вс", "/npo-echelon/eshelon-skaner-vs"),
    ("сканер-вс 7 эшелон", "/npo-echelon/eshelon-skaner-vs"),
    ("лицензия scaner vs эшелон", "/npo-echelon/eshelon-skaner-vs"),
    ("сканер вс анализ защищенности", "/npo-echelon/eshelon-skaner-vs"),
    ("нпо эшелон сканер-вс", "/npo-echelon/eshelon-skaner-vs"),
    ("мтс линк для видеовстреч", "/webinar"),
    ("webinar group мтс линк", "/webinar"),
    ("мтс линк стандарт", "/webinar"),
    ("мтс вебинары", "/webinar"),
    ("видеоконференции от мтс", "/webinar"),
    (
        "макспатрол защита рабочих станций",
        "/positive-technologies/maxpatrol-endpoint-protection-platform",
    ),
    (
        "max patrol endpoint positive technologies",
        "/positive-technologies/maxpatrol-endpoint-protection-platform",
    ),
    (
        "макс патрол антивирус",
        "/positive-technologies/maxpatrol-endpoint-protection-platform",
    ),
    (
        "endpoint protection maxpatrol",
        "/positive-technologies/maxpatrol-endpoint-protection-platform",
    ),
    (
        "макс патрол epp",
        "/positive-technologies/maxpatrol-endpoint-protection-platform",
    ),
    ("семейная подписка на microsoft 365", "/microsoft/microsoft-365-for-family"),
    ("office 365 для семьи", "/microsoft/microsoft-365-for-family"),
    ("майкрософт 365 family", "/microsoft/microsoft-365-for-family"),
    ("microsoft 365 на всю семью", "/microsoft/microsoft-365-for-family"),
    ("семейный майкрософт 365 на шесть человек", "/microsoft/microsoft-365-for-family"),
]


def measured(
    engine: SearchEngine, queries, mode: str, groups: dict[str, set[str]]
) -> dict:
    durations, rows = [], []
    for query, path in queries:
        start = time.perf_counter()
        response = engine.search(query, mode=mode)
        durations.append((time.perf_counter() - start) * 1000)
        items = response["items"]
        rows.append(
            {
                "query": query,
                "target_family": path,
                "rank": next(
                    (
                        n
                        for n, item in enumerate(items, 1)
                        if item["group"] in groups[path]
                    ),
                    None,
                ),
                "count": len(items),
                "top_names": [item["name"] for item in items],
                "top_skus": [item["sku"] for item in items],
                "top_groups": [item["group"] for item in items],
                "target_groups": sorted(groups[path]),
            }
        )
    total = len(rows)
    ranked = [r["rank"] for r in rows if r["rank"] is not None]
    return {
        "queries": total,
        "hit_at_1": sum(r == 1 for r in ranked) / total,
        "hit_at_4": len(ranked) / total,
        "mrr_at_4": sum(1 / r for r in ranked) / total,
        "four_results": sum(r["count"] >= 4 for r in rows) / total,
        "zero_results": sum(r["count"] == 0 for r in rows) / total,
        "median_ms": round(statistics.median(durations), 1),
        "rows": rows,
    }


def complete_results(engine: SearchEngine, queries) -> dict:
    rows = []
    counts = []
    with TemporaryDirectory() as temp_dir:
        service = Service(
            engine, Path(temp_dir) / "events.sqlite", log_diagnostics=False
        )
        for query, _ in queries:
            response = service.search(query)
            direct = response["items"]
            companions = response["recommendations"]
            groups = {item["group"] for item in direct + companions}
            counts.append(len(groups))
            rows.append(
                {
                    "query": query,
                    "count": len(groups),
                    "direct": [item["name"] for item in direct],
                    "companions": [item["name"] for item in companions],
                    "elapsed_ms": response["elapsed_ms"],
                }
            )
    return {
        "queries": len(rows),
        "four_results": counts.count(4),
        "slots_filled": sum(counts),
        "rows": rows,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--workbook", type=Path, default=Path("materials/search-queries.xlsx")
    )
    p.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    p.add_argument("--out", type=Path, default=Path("reports/search-quality.json"))
    args = p.parse_args()
    engine = SearchEngine(args.db)
    examples = [
        (query, urlsplit(url).path.lower().rstrip("/"))
        for query, url in cases(args.workbook)
    ]
    targets = {path for _, path in [*examples, *PARAPHRASES]}
    groups = {path: family_groups(engine, path) for path in sorted(targets)}
    if missing := [path for path, ids in groups.items() if not ids]:
        p.error(f"Targets missing from the feed: {missing}")
    report = {
        name: {
            mode: measured(engine, queries, mode, groups)
            for mode in ("baseline", "enhanced")
        }
        for name, queries in (
            ("softline_examples", examples),
            ("author_paraphrases", PARAPHRASES),
        )
    }
    report["softline_examples"]["with_companions"] = complete_results(engine, examples)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name, results in report.items():
        for mode, values in results.items():
            print(name, mode, {k: v for k, v in values.items() if k != "rows"})


if __name__ == "__main__":
    main()
