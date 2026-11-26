"""Import the original Softline YML feed into a local search index."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from defusedxml import ElementTree as ET

MARKUP = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
CREATE_PRODUCTS = """
    CREATE TABLE products (
        sku TEXT PRIMARY KEY, group_id TEXT, name TEXT NOT NULL, vendor TEXT,
        code TEXT, url TEXT NOT NULL, category_id TEXT, category TEXT, available INTEGER,
        price REAL, old_price REAL, currency TEXT, description TEXT, params TEXT
    )
"""
INSERT_PRODUCT = """
    INSERT OR IGNORE INTO products (
        sku, group_id, name, vendor, code, url, category_id, category, available,
        price, old_price, currency, description, params
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def clean(value: str | None) -> str:
    return SPACE.sub(" ", MARKUP.sub(" ", html.unescape(value or ""))).strip()


def price(value: str | None) -> float | None:
    try:
        amount = float(value or "")
        return amount if 0 < amount < 1e12 else None
    except ValueError:
        return None


def offer_record(tag, categories: dict[str, str], stats: Counter) -> tuple | None:
    sku = tag.get("id", "")
    fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in tag}
    name, url = clean(fields.get("name")), clean(fields.get("url"))
    parsed_url = urlsplit(url)
    if (
        not sku
        or not name
        or parsed_url.scheme != "https"
        or parsed_url.hostname != "store.softline.ru"
        or parsed_url.username is not None
    ):
        stats["invalid"] += 1
        return None
    params: dict[str, list[str]] = {}
    for child in tag:
        if child.tag.rsplit("}", 1)[-1] != "param":
            continue
        key, val = clean(child.get("name")), clean(child.text)
        if key and val and val.casefold() != "другое или отсутствует":
            values = params.setdefault(key, [])
            if val not in values:
                values.append(val)
    cid = clean(fields.get("categoryId"))
    available = int(tag.get("available", "").lower() == "true")
    stats["available"] += available
    stats["with_price"] += int(price(fields.get("price")) is not None)
    return (
        sku,
        tag.get("group_id") or fields.get("group_id") or sku,
        name,
        clean(fields.get("vendor")),
        clean(fields.get("vendorCode")),
        url,
        cid,
        categories.get(cid, ""),
        available,
        price(fields.get("price")),
        price(fields.get("oldprice")),
        clean(fields.get("currencyId")),
        clean(fields.get("description"))[:1200],
        json.dumps(params, ensure_ascii=False, separators=(",", ":")),
    )


def feed_rows(path: Path, stats: Counter):
    with path.open("rb") as signature:
        compressed = signature.read(2) == b"\x1f\x8b"
    open_feed = gzip.open if compressed else open
    categories: dict[str, str] = {}
    with open_feed(path, "rb") as stream:
        stack = []
        for event, tag in ET.iterparse(
            stream, events=("start", "end"), forbid_entities=True, forbid_external=True
        ):
            if event == "start":
                stack.append(tag)
                continue
            stack.pop()
            kind = tag.tag.rsplit("}", 1)[-1]
            if kind == "category":
                categories[tag.get("id", "")] = clean(tag.text)
            elif kind == "offer":
                stats["offers"] += 1
                record = offer_record(tag, categories, stats)
                if record is not None:
                    yield record
            if kind in {"offer", "category"}:
                stack[-1].remove(tag)
        stats["categories"] = len(categories)


def build(feed: Path, destination: Path) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".building")
    if temporary.exists():
        raise FileExistsError(f"Previous build exists: {temporary}")
    stats: Counter = Counter()
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA journal_mode=OFF")
            db.execute(CREATE_PRODUCTS)
            db.executemany(INSERT_PRODUCT, feed_rows(feed, stats))
            db.execute("CREATE INDEX products_group ON products(group_id)")
            db.execute(
                "CREATE INDEX products_category_available ON products(category_id, available)"
            )
            db.execute("CREATE INDEX products_code ON products(code COLLATE NOCASE)")
            db.execute(
                "CREATE VIRTUAL TABLE catalog USING fts5(name, vendor, code, category, "
                "description, content='products', content_rowid='rowid', tokenize='unicode61')"
            )
            db.execute("INSERT INTO catalog(catalog) VALUES('rebuild')")
            db.execute(
                "CREATE VIRTUAL TABLE title_trigrams USING fts5(name, content='products', "
                "content_rowid='rowid', tokenize='trigram')"
            )
            db.execute("INSERT INTO title_trigrams(title_trigrams) VALUES('rebuild')")
            db.execute("CREATE TABLE vocabulary(term TEXT PRIMARY KEY)")
            db.execute(
                "CREATE VIRTUAL TABLE title_terms USING fts5vocab(catalog, 'col')"
            )
            db.execute(
                "INSERT INTO vocabulary SELECT term FROM title_terms WHERE col = 'name'"
            )
            db.execute("DROP TABLE title_terms")
            stats["indexed"] = db.execute("SELECT count(*) FROM products").fetchone()[0]
            stats["duplicates"] = stats["offers"] - stats["invalid"] - stats["indexed"]
            db.commit()
            if not stats["indexed"]:
                raise ValueError("No valid products found in feed")
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/catalog.sqlite"))
    args = parser.parse_args()
    print(json.dumps(build(args.feed, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
