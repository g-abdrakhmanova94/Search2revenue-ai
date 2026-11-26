"""Compare search variants against the original XLSX examples without training on them."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

from defusedxml import ElementTree as ET

from search import SearchEngine, normalize


@dataclass(frozen=True)
class Label:
    vendor: str
    name_terms: tuple[str, ...]
    category: str | None = None


# Offer-level URLs are different from family pages. Labels are evaluation-only.
LABELS = {
    "/astralinux/astra-linux-special-edition": Label(
        "astralinux", ("astra linux special edition",), "222320"
    ),
    "/microsoft/microsoft-office-dlya-doma-i-biznesa-2024": Label(
        "microsoft", ("office", "для дома и бизнеса", "2024")
    ),
    "/kaspersky/kaspersky-small-office-security": Label(
        "kaspersky", ("kaspersky small office security",)
    ),
    "/kripto-pro/cryptopro-csp": Label("kripto-pro", ("криптопро csp",)),
    "/anydesk/anydesk": Label("anydesk", ("anydesk",)),
    "/cyberprotect": Label("cyberprotect", ("кибер бэкап",), "1033"),
    "/npo-echelon/eshelon-skaner-vs": Label("npo-echelon", ("сканер-вс",)),
    "/webinar": Label("webinar", ("мтс линк",)),
    "/positive-technologies/maxpatrol-endpoint-protection-platform": Label(
        "positive-technologies", ("maxpatrol endpoint protection",)
    ),
    "/microsoft/microsoft-365-for-family": Label(
        "microsoft", ("microsoft 365", "для семьи")
    ),
}


def workbook_xml(archive: ZipFile, name: str):
    if archive.getinfo(name).file_size > 32 * 1024 * 1024:
        raise ValueError(f"Workbook XML member is too large: {name}")
    return ET.fromstring(archive.read(name), forbid_dtd=True)


def cases(workbook: Path) -> list[tuple[str, str]]:
    with ZipFile(workbook) as z:
        strings = (
            [
                "".join(node.text or "" for node in item.iterfind(".//{*}t"))
                for item in workbook_xml(z, "xl/sharedStrings.xml")
            ]
            if "xl/sharedStrings.xml" in z.namelist()
            else []
        )
        sheet = workbook_xml(z, "xl/worksheets/sheet1.xml")
    result = []
    for row in sheet.iterfind(".//{*}row"):
        cells = {}
        for cell in row:
            column = cell.get("r", "").rstrip("0123456789")
            if column not in {"A", "B"}:
                continue
            value = cell.findtext("{*}v", "")
            if cell.get("t") == "s":
                value = strings[int(value)]
            elif cell.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.iterfind(".//{*}t"))
            cells[column] = value
        if cells.get("A") and cells.get("B", "").startswith("https://"):
            result.append((cells["A"], cells["B"]))
    if not result:
        raise ValueError("Workbook has no query and target URL rows")
    return result


def family_groups(engine: SearchEngine, target: str) -> set[str]:
    url = urlsplit(target)
    path = url.path.rstrip("/").lower()
    label = LABELS.get(path)
    vendor = label.vendor if label else path.strip("/").split("/")[0]
    categories = set(parse_qs(url.query).get("filter[category][]", []))
    with closing(engine.connect()) as db:
        rows = db.execute(
            "SELECT group_id, name, url, category_id FROM products WHERE url LIKE ?",
            (f"https://store.softline.ru/{vendor}/%",),
        )
        groups = set()
        for group, name, offer_url, category in rows:
            if categories and category not in categories:
                continue
            offer_path = urlsplit(offer_url).path.rstrip("/").lower()
            exact = offer_path == path or (
                label is None and offer_path.startswith((path + "/", path + "-"))
            )
            alias = (
                label
                and (label.category is None or category == label.category)
                and all(term in normalize(name) for term in label.name_terms)
            )
            if exact or alias:
                groups.add(group)
    return groups


def run(engine: SearchEngine, prompts: list[tuple[str, str]], mode: str) -> dict:
    rows, durations = [], []
    for query, target in prompts:
        groups = family_groups(engine, target)
        start = time.perf_counter()
        result = engine.search(query, mode=mode)
        durations.append((time.perf_counter() - start) * 1000)
        items = result["items"]
        rank = next(
            (n for n, item in enumerate(items, 1) if item["group"] in groups), None
        )
        rows.append(
            {
                "query": query,
                "target": target,
                "target_groups": sorted(groups),
                "rank": rank,
                "count": len(items),
                "first": items[0]["url"] if items else None,
                "first_name": items[0]["name"] if items else None,
                "corrections": result["corrections"],
            }
        )
    return {
        "queries": len(rows),
        "hit_at_1": sum(r["rank"] == 1 for r in rows) / len(rows),
        "hit_at_4": sum(r["rank"] is not None for r in rows) / len(rows),
        "mrr_at_4": sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows),
        "four_results": sum(r["count"] >= 4 for r in rows) / len(rows),
        "median_ms": round(statistics.median(durations), 1),
        "rows": rows,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument("--db", type=Path, default=Path("data/catalog.sqlite"))
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    prompts = cases(args.workbook)
    engine = SearchEngine(args.db)
    report = {mode: run(engine, prompts, mode) for mode in ("baseline", "enhanced")}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for mode, output in report.items():
        print(mode, {k: v for k, v in output.items() if k != "rows"})
        for row in output["rows"]:
            print(row["rank"], row["query"], row["first"], row["corrections"])


if __name__ == "__main__":
    main()
