"""Search and product-fit constraints for the Softline catalog."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

WORDS = re.compile(r"[a-zа-я0-9]+")
STOP = {
    "для",
    "на",
    "и",
    "в",
    "по",
    "от",
    "с",
    "без",
    "до",
    "из",
    "мне",
    "нам",
    "нужно",
    "нужна",
    "нужен",
    "нужны",
    "купить",
    "под",
    "подберите",
    "хочу",
    "компания",
    "компании",
    "бизнес",
}
ALIASES = {
    "astra linux se": "astra linux special edition",
    "астра линукс се": "astra linux special edition",
    "астра линукс": "astra linux",
    "астра": "astra",
    "линукс": "linux",
    "офис майкрософт": "microsoft office",
    "майкрософт": "microsoft",
    "микрософт": "microsoft",
    "дом и бизнес": "дома бизнеса",
    "домашний и рабочий": "дома бизнеса",
    "home and business": "дома бизнеса",
    "домой и на работу": "дома бизнеса",
    "семейная": "семьи",
    "семейный": "семьи",
    "всю семью": "семьи",
    "family": "семьи",
    "касперский": "kaspersky",
    "касперского": "kaspersky",
    "каспер": "kaspersky",
    "крипто про": "криптопро",
    "crypto pro": "cryptopro",
    "эцп": "электронной подписи",
    "эннидеск": "anydesk",
    "ани деск": "anydesk",
    "эни деск": "anydesk",
    "макс патрол": "maxpatrol",
    "max patrol": "maxpatrol",
    "бэкап": "бэкап резервное копирование",
    "бекап": "бэкап резервное копирование",
    "удалёнка": "удаленный доступ",
    "удаленка": "удаленный доступ",
    "серваки": "сервер",
    "сервак": "сервер",
    "свитч": "коммутатор",
    "серверы": "сервер",
    "антивирь": "антивирус",
    "винда": "windows",
    "винду": "windows",
    "мдаемон": "mdaemon",
    "отечественная ос": "российские операционные системы",
    "отечественная операционная система": "российские операционные системы",
    "расширенного": "расширенный",
    "автотранспорт": "автотранспортом",
    "мтс линк": "мтс линк webinar",
    "мтс": "мтс webinar",
    "малый офис": "small office",
    "малого офиса": "small office security",
    "cryptopro": "криптопро",
    "цсп": "csp",
    "any desk": "anydesk",
    "кибербэкап": "кибер бэкап",
    "cyberprotect": "киберпротект",
    "макспатрол": "maxpatrol",
}
RENEW = re.compile(r"\b(?:renewal|renew|продлени[ея]|продлить|продляем)\b")
NOT_RENEW = re.compile(r"\b(?:без|не)\s+(?:продлени[ея]|renewal)\b")
NEW = re.compile(
    r"\b(?:первичная|первичное|новая покупка|новая лицензия|новую лицензию|base license|new license)\b"
)
NO_USB = re.compile(r"\b(?:без|не нужен|не нужна|не надо)\s+(?:usb|юсб|ключа|донгла)\b")
WITH_USB = re.compile(r"\b(?:с|нужен|нужна)\s+(?:usb|юсб|донглом)\b")
USB = re.compile(r"\b(?:usb|юсб|донгл)\b")
ELECTRONIC = re.compile(r"\b(?:электронн\w*|esd)\b")
ELECTRONIC_REQUEST = re.compile(
    r"\b(?:электронн\w*\s+(?:поставк\w*|лицензи\w*|ключ\w*)|esd)\b"
)
QUANTITY = re.compile(
    r"\b(\d{1,6})\s*(?:шт|мест|пк|комп\w*|устройств\w*|рабоч\w*\s+мест|узл\w*|пользовател\w*)\b"
)
RANGE = re.compile(r"(?<!\d)(\d{1,6})\s*(?:-|–|—|до)\s*(\d{1,6})(?!\d)")
COUNT_LABEL = re.compile(
    r"устройств|рабоч\w* мест|пользовател|узлов|^от \d|узлы|количество|установок|лицензий"
)
PREREQUISITE = re.compile(
    r"\b(?:обновлен\w*|переход\w*|upgrade|migration|cross.grade)\b"
)
SUPPORT_LICENSE = re.compile(
    r"\b(?:сертификат\w*|сопровожд\w*|техподдерж\w*|техническ\w* поддерж\w*)\b"
)
CSP_VERSION_REQUEST = re.compile(r"\b(?:csp|цсп|верси[яи])\s+([45])(?:\.(\d+))?\b")
CSP_VERSION_OFFER = re.compile(r"\bcsp\s+(?:версии\s+)?([45])\.(\d+)\b")
SCANNER_EXTRA_IP = re.compile(r"\b(?:дополнительн\w*\s+ip|additional ip)\b")
ACADEMIC_AUDIENCE = re.compile(r"академ|образовательн|учебн|школ|университет|\bвуз")
EXCLUDED_EDITION = re.compile(
    r"\b(?:не|кроме)\s+(семейн\w*|персональн\w*|standard|solo|advanced|enterprise)\b"
)
PLATFORM_REQUEST = re.compile(
    r"\b(?:для|под|на)\s+(macos|mac|linux|windows(?:\s+(?:server\s+20\d{2}|11|10|8\.1|8|7))?)\b"
)
PLATFORM_NAMES = {
    "mac": re.compile(r"\b(?:mac(?:os)?|mac os|os x)\b"),
    "macos": re.compile(r"\b(?:mac(?:os)?|mac os|os x)\b"),
    "linux": re.compile(r"\b(?:linux|линукс)\b"),
    "windows": re.compile(r"\bwindows\b"),
}
OFFICE_YEARS = {"2016", "2019", "2021", "2024"}
ALIAS_RE = re.compile(
    r"\b(?:" + "|".join(map(re.escape, sorted(ALIASES, key=len, reverse=True))) + r")\b"
)
BASELINE_COLUMNS = (1, 1, 1, 1, 1)
RANK_COLUMNS = (9, 2, 16, 3, 0.45)
ASTRA_COMPATIBILITY_INTENTS = frozenset(
    {
        "антивирус",
        "бэкап",
        "драйвер",
        "защита",
        "криптопро",
        "поддержка",
        "программное",
        "резервное",
        "сертификат",
        "софт",
        "субд",
        "vpn",
    }
)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def words(text: str) -> list[str]:
    return WORDS.findall(normalize(text))


def quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def trigrams(text: str) -> set[str]:
    return {text[i : i + 3] for i in range(len(text) - 2) if " " not in text[i : i + 3]}


def code_match_candidate(tokens: list[str], row: sqlite3.Row) -> bool:
    return bool(row["code"]) and words(row["code"]) == tokens


def matches_anydesk_package(terms: set[str], title_terms: set[str]) -> bool:
    editions = terms & {"solo", "standard", "advanced", "ultimate"}
    addon_terms = {"additional", "namespace", "concurrent", "managed", "bundle"}
    requested_addons = terms & addon_terms
    named_addons = title_terms & addon_terms
    return (
        "anydesk" in title_terms
        and (not editions or bool(editions & title_terms))
        and (requested_addons <= named_addons if requested_addons else not named_addons)
    )


def matches_csp_package(query: str, terms: set[str], title: str) -> bool:
    if "криптопро csp" not in title:
        return False
    modules = {"tls", "tsp"}
    named = modules & set(words(title))
    requested = modules & terms
    if requested != named:
        return False
    version = CSP_VERSION_REQUEST.search(query)
    if not version:
        return True
    return (version[1], version[2] or "0") in CSP_VERSION_OFFER.findall(title)


def matches_purchase_prerequisite(query: str, title: str) -> bool:
    for pattern in (PREREQUISITE, RENEW):
        if bool(pattern.search(query)) != bool(pattern.search(title)):
            return False
    if RENEW.search(query) and not SUPPORT_LICENSE.search(query):
        return True
    return bool(SUPPORT_LICENSE.search(query)) == bool(SUPPORT_LICENSE.search(title))


def matches_specialized_license(query: str, terms: set[str], title: str) -> bool:
    if {"криптопро", "csp"} <= terms:
        return matches_csp_package(
            query, terms, title
        ) and matches_purchase_prerequisite(query, title)
    if {"сканер", "вс"} <= terms:
        return (
            "сканер-вс" in title
            and bool(SCANNER_EXTRA_IP.search(query))
            == bool(SCANNER_EXTRA_IP.search(title))
            and matches_purchase_prerequisite(query, title)
        )
    if is_cyber_backup(terms):
        return matches_purchase_prerequisite(query, title) and (
            not ACADEMIC_AUDIENCE.search(title) or bool(ACADEMIC_AUDIENCE.search(query))
        )
    return True


def is_cyber_backup(terms: set[str]) -> bool:
    return bool(terms & {"бэкап", "копирование"} and terms & {"кибер", "киберпротект"})


def specialized_package_penalty(query: str, terms: set[str], title: str) -> int:
    if {"сканер", "вс"} <= terms:
        return 45 * sum(
            edition in title and edition not in query
            for edition in ("enterprise", "инспектор")
        )
    if is_cyber_backup(terms):
        certified_without_request = (
            "сертифицированн" in title and "сертифиц" not in query
        )
        if (
            not title.startswith("киберпротект кибер бэкап")
            or certified_without_request
        ):
            return 45
    return 0


def parse_intent(query: str) -> tuple[list[str], dict]:
    text = normalize(query)
    renewal = bool(RENEW.search(NOT_RENEW.sub(" ", text)))
    new = bool(NEW.search(text) or NOT_RENEW.search(text))
    electronic = bool(NO_USB.search(text) or ELECTRONIC_REQUEST.search(text))
    usb = bool(WITH_USB.search(text))
    quantities = {int(count[1]) for count in QUANTITY.finditer(text)}
    platforms = {m[1].replace("macos", "mac") for m in PLATFORM_REQUEST.finditer(text)}
    excluded = {
        "семейн|для семьи|family"
        if word.startswith("семейн")
        else "персональн|personal"
        if word.startswith("персональн")
        else word
        for word in EXCLUDED_EDITION.findall(text)
    }
    conflict = None
    if renewal and new:
        conflict = "Что подбираем: новую лицензию или продление?"
    elif electronic and usb:
        conflict = "Какой формат поставки нужен: электронный без USB или с USB-ключом?"
    elif len(quantities) > 1 or 0 in quantities:
        conflict = "Укажите одно положительное количество рабочих мест или устройств."
    elif len(platforms) > 1:
        conflict = "Для какой операционной системы подбираем товар?"
    operation = "renewal" if renewal else "new" if new else None
    delivery = "electronic" if electronic else "usb" if usb else None
    stripped = text
    for pattern in (
        QUANTITY,
        NO_USB,
        WITH_USB,
        NOT_RENEW,
        RENEW,
        NEW,
        PLATFORM_REQUEST,
        EXCLUDED_EDITION,
    ):
        stripped = pattern.sub(" ", stripped)
    expanded = ALIAS_RE.sub(lambda m: ALIASES[m.group()], stripped)
    tokens = list(
        dict.fromkeys(
            t
            for t in words("microsoft 365" if expanded.strip() == "365" else expanded)
            if t not in STOP and (len(t) > 1 or t.isdigit())
        )
    )[:14]
    return tokens, {
        "operation": operation,
        "delivery": delivery,
        "quantity": next(iter(quantities)) if len(quantities) == 1 else None,
        "conflict": conflict,
        "platform": next(iter(platforms)) if len(platforms) == 1 else None,
        "excluded_editions": sorted(excluded),
    }


ROLE_SEARCH = {
    "rack_server": ["сервер", "rack"],
    "server_hardware": ["сервер"],
    "ssd": ["ssd"],
    "external_ssd": ["внешние", "ssd"],
    "tablet": ["планшет", "samsung"],
    "ups": ["ибп", "apc"],
    "switch": ["коммутатор", "switch", "1000"],
    "office": ["microsoft", "office", "2024"],
    "office_business": ["microsoft", "office", "2024", "бизнеса"],
    "office_suite": ["microsoft", "office"],
    "office_home_business": ["microsoft", "office", "дома", "бизнеса"],
    "office_subscription": ["microsoft", "365"],
    "small_office_security": ["kaspersky", "small", "office", "security"],
}
GIGABIT = re.compile(
    r"\bgigabit\b|1\s*(?:gbps|gbe|гбит)|1000\s*(?:mbps|мбит|base)|10/100/1000"
)


def office_role_matches(
    role: str, row: sqlite3.Row, title: str, category: str, years: set[str]
) -> bool:
    required = {"365"} if role == "office_subscription" else {"office"}
    if role != "office_subscription":
        required.update(years)
    if role in {"office", "office_business"}:
        required.add("2024")
    if role in {"office_business", "office_home_business"}:
        required.update(("дома", "бизнеса"))
    title_terms = set(words(title))
    microsoft = "microsoft" in title_terms or "microsoft" in words(row["vendor"])
    return microsoft and "офис" in category and required <= title_terms


def matches_explicit_requirements(
    row: sqlite3.Row, title: str, constraints: dict
) -> bool:
    platform = constraints.get("platform")
    if platform:
        declared = normalize(platform_text(row["description"]))
        pattern = PLATFORM_NAMES.get(platform) or re.compile(
            r"\b" + re.escape(platform) + r"\b"
        )
        if not pattern.search(declared):
            return False
    return not any(
        re.search(r"\b(?:" + pattern + r")", title)
        for pattern in constraints.get("excluded_editions", ())
    )


def software_role(terms: set[str]) -> str | None:
    if {"small", "office", "security"} <= terms:
        return "small_office_security"
    if (
        "365" in terms
        and (terms == {"365"} or terms & {"microsoft", "office", "семьи"})
        and not terms & {"антивирус", "kaspersky", "защита", "security"}
    ):
        return "office_subscription"
    if not {"microsoft", "office"} <= terms:
        return None
    home_business = {"дома", "бизнеса"} <= terms
    if "2024" in terms:
        return "office_business" if home_business else "office"
    return "office_home_business" if home_business else "office_suite"


def product_role(tokens: list[str]) -> str | None:
    terms = set(tokens)
    if "сервер" in terms and terms & {"стоечный", "стоечные", "rack"}:
        return "rack_server"
    server_vendors = {"dell", "hpe", "lenovo", "supermicro"}
    if "сервер" in terms and (
        tokens == ["сервер"]
        or (
            bool(terms & server_vendors)
            and not terms & {"лицензий", "лицензия", "лицензии", "ключей", "по"}
        )
    ):
        return "server_hardware"
    if "ssd" in terms and terms & {"внешний", "внешние", "external"}:
        return "external_ssd"
    if tokens[:1] == ["планшет"] and "samsung" in terms:
        return "tablet"
    if tokens[:1] == ["ssd"]:
        return "ssd"
    if "apc" in terms and ("ибп" in terms or {"бесперебойного", "питания"} <= terms):
        return "ups"
    if "коммутатор" in terms and terms & {"гигабитный", "gigabit", "гигабит", "1gbe"}:
        return "switch"
    return software_role(terms)


def fits_product_role(
    role: str | None,
    row: sqlite3.Row,
    title: str,
    category: str,
    office_years: set[str],
) -> bool:
    if role == "rack_server":
        return ("rack" in category or "стоечн" in category) and "сервер" in title
    if role == "server_hardware":
        return (
            bool({"сервер", "серверы"} & set(words(category)))
            and ("сервер" in title or "server" in title)
            and not {"по", "аксессуары", "комплектующие"} & set(words(category))
        )
    if role == "external_ssd":
        return "внешн" in category and "ssd" in category and "ssd" in title
    if role == "tablet":
        return "планшет" in category and "samsung" in words(title)
    if role == "ssd":
        return "ssd" in category and "ssd" in title and "аксессуар" not in category
    if role == "ups":
        return category.startswith("ибп") and "apc" in words(title)
    if role == "switch":
        spec = normalize(f"{title} {row['description']}")
        return "коммутатор" in category and bool(GIGABIT.search(spec))
    if role == "small_office_security":
        return "kaspersky small office security" in title and "антивирус" in category
    if role and role.startswith("office"):
        return office_role_matches(role, row, title, category, office_years)
    return True


def catalog_evidence(field: str, value: str, url: str) -> dict[str, str]:
    return {"kind": "feed", "field": field, "value": value, "url": url}


def purchase_priority(row: sqlite3.Row) -> int:
    price = row["price"]
    return 0 if row["available"] and price is not None and price > 0 else 1


def platform_text(description: str) -> str:
    match = re.search(r"Платформа:\s*[^•]+", description, re.IGNORECASE)
    return match[0].strip() if match else ""


def quantity_evidence(row: sqlite3.Row, quantity: int) -> tuple[str, str] | None:
    description = row["description"]
    pattern = r"(?:узлов|устройств|рабоч\w* мест|пользователей)\s+от\s+(\d{1,6})\s+до\s+(\d{1,6})"
    match = re.search(pattern, description, re.IGNORECASE)
    if match and int(match[1]) <= quantity <= int(match[2]):
        return "description", match[0]
    for key, values in json.loads(row["params"]).items():
        if not COUNT_LABEL.search(normalize(key)):
            continue
        for value in values:
            match = RANGE.search(value)
            if value.strip() == str(quantity) or (
                match and int(match[1]) <= quantity <= int(match[2])
            ):
                return f"param[{key}]", value
    title = row["name"]
    for match in RANGE.finditer(title):
        if int(match[1]) <= quantity <= int(match[2]) and COUNT_LABEL.search(
            normalize(title[match.end() : match.end() + 35])
        ):
            return "name", title[match.start() : match.end() + 35].strip()
    for match in QUANTITY.finditer(title.casefold()):
        if int(match[1]) == quantity:
            return "name", title[match.start() : match.end()]
    return None


def license_evidence(
    row: sqlite3.Row, confirmed: list[str], constraints: dict
) -> list[dict[str, str]]:
    params = [
        (key, value)
        for key, values in json.loads(row["params"]).items()
        for value in values
    ]
    title = row["name"]
    result = []
    if constraints.get("platform"):
        result.append(
            catalog_evidence(
                "description", platform_text(row["description"]), row["url"]
            )
        )
    if "Тип покупки подтверждён каталогом" in confirmed:
        pattern = RENEW if constraints["operation"] == "renewal" else NEW
        hit = next(
            (
                (f"param[{key}]", value)
                for key, value in params
                if "лиценз" in normalize(key) and pattern.search(normalize(value))
            ),
            ("name", title),
        )
        result.append(catalog_evidence(*hit, row["url"]))
    if "Формат поставки подтверждён" in confirmed:
        hit = next(
            (
                (f"param[{key}]", value)
                for key, value in params
                if constraints["delivery"] == "electronic"
                and any(t in normalize(key) for t in ("доставк", "поставк"))
                and ELECTRONIC.search(normalize(value))
            ),
            ("name", title),
        )
        result.append(catalog_evidence(*hit, row["url"]))
    quantity = constraints["quantity"]
    if quantity and f"Поддерживаемое количество включает {quantity}" in confirmed:
        hit = quantity_evidence(row, quantity)
        if hit:
            result.append(catalog_evidence(*hit, row["url"]))
    return result


@dataclass
class SearchEngine:
    database: Path

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def verify_catalog(self) -> None:
        with closing(self.connect()) as db:
            if db.execute("SELECT sku FROM products LIMIT 1").fetchone() is None:
                raise sqlite3.DatabaseError("Catalog has no products.")
            db.execute("SELECT term FROM vocabulary LIMIT 1").fetchone()
            db.execute(
                "SELECT rowid FROM catalog WHERE catalog MATCH 'readiness' LIMIT 1"
            ).fetchone()
            db.execute(
                "SELECT name FROM title_trigrams WHERE title_trigrams MATCH 'readiness' LIMIT 1"
            ).fetchone()

    @staticmethod
    def _correct(db: sqlite3.Connection, tokens: list[str]) -> dict[str, str]:
        corrected = {}
        for term in tokens:
            if (
                len(term) < 4
                or term.isdigit()
                or db.execute(
                    "SELECT 1 FROM vocabulary WHERE term=?", (term,)
                ).fetchone()
            ):
                continue
            transposed = (
                SearchEngine._transposed_word(db, term) if len(tokens) > 1 else None
            )
            if transposed:
                corrected[term] = transposed
                continue
            pieces = sorted(trigrams(term))
            if not pieces:
                continue
            candidates = db.execute(
                """SELECT name FROM title_trigrams WHERE title_trigrams MATCH ?
                   ORDER BY rank LIMIT 34""",
                (" OR ".join(map(quote, pieces)),),
            )
            options = {
                w
                for row in candidates
                for w in words(row["name"])
                if abs(len(w) - len(term)) <= 2
            }
            threshold = 0.77 if len(term) >= 6 else 0.80
            ranked = sorted(
                (SequenceMatcher(None, term, w).ratio(), w) for w in options
            )
            if ranked and ranked[-1][0] >= threshold:
                corrected[term] = ranked[-1][1]
        return corrected

    @staticmethod
    def _transposed_word(db: sqlite3.Connection, term: str) -> str | None:
        variants = [
            term[:i] + term[i + 1] + term[i] + term[i + 2 :]
            for i in range(len(term) - 1)
            if term[i] != term[i + 1]
        ]
        if not variants:
            return None
        matches = db.execute(
            "SELECT term FROM vocabulary WHERE term IN (SELECT value FROM json_each(?)) LIMIT 2",
            (json.dumps(variants),),
        ).fetchall()
        return matches[0][0] if len(matches) == 1 else None

    @staticmethod
    def _retrieve(
        db: sqlite3.Connection, tokens: list[str], weights: tuple, limit: int
    ) -> list[sqlite3.Row]:
        if not tokens:
            return []
        ors = " OR ".join(quote(term) for term in tokens)
        if len(tokens) == 1:
            matches = [(ors, limit)]
        else:
            matches = [
                (" AND ".join(map(quote, tokens)), min(160, limit)),
                ("name : " + quote(" ".join(tokens)), min(180, limit)),
                (ors, limit),
            ]
        rows: dict[str, sqlite3.Row] = {}
        for expression, cap in matches:
            for row in db.execute(
                """SELECT p.*, bm25(catalog, ?, ?, ?, ?, ?) AS lexical
                    FROM catalog JOIN products p ON p.rowid=catalog.rowid
                    WHERE catalog MATCH ? ORDER BY lexical LIMIT ?""",
                (*weights, expression, cap),
            ):
                rows.setdefault(row["sku"], row)
        return list(rows.values())

    def _retrieve_candidates(
        self, db: sqlite3.Connection, tokens: list[str], clean: str, role: str | None
    ) -> list[sqlite3.Row]:
        if "anydesk" in tokens:
            return db.execute(
                """SELECT p.*, bm25(catalog, ?, ?, ?, ?, ?) AS lexical
                   FROM catalog JOIN products p ON p.rowid=catalog.rowid
                   WHERE catalog MATCH ? ORDER BY lexical LIMIT 230""",
                (*RANK_COLUMNS, 'name : "anydesk"'),
            ).fetchall()
        rows = self._retrieve(db, tokens, RANK_COLUMNS, 230)
        seen = {row["sku"] for row in rows}
        rows.extend(
            row
            for row in self._retrieve_cyber_base(db, set(tokens), normalize(clean))
            if row["sku"] not in seen
        )
        if role:
            seen.update(row["sku"] for row in rows)
            rows.extend(
                row
                for row in self._retrieve(db, ROLE_SEARCH[role], RANK_COLUMNS, 400)
                if row["sku"] not in seen
            )
        return rows

    @staticmethod
    def _retrieve_cyber_base(
        db: sqlite3.Connection, terms: set[str], query: str
    ) -> list[sqlite3.Row]:
        if not is_cyber_backup(terms) or any(
            pattern.search(query) for pattern in (PREREQUISITE, SUPPORT_LICENSE, RENEW)
        ):
            return []
        return db.execute(
            """SELECT p.*, bm25(catalog, ?, ?, ?, ?, ?) AS lexical
               FROM catalog JOIN products p ON p.rowid = catalog.rowid
               WHERE catalog MATCH ? AND p.category_id = '1033'
               AND p.description LIKE '%Тип лицензии: полная версия%'
               ORDER BY lexical LIMIT 80""",
            (*RANK_COLUMNS, 'name : "киберпротект кибер бэкап"'),
        ).fetchall()

    @staticmethod
    def _fit(row: sqlite3.Row, constraints: dict) -> tuple[bool, list[str], list[str]]:
        if not matches_explicit_requirements(row, normalize(row["name"]), constraints):
            return False, [], []
        matched, confirmed, unknown = SearchEngine._license_fit(row, constraints)
        if matched and constraints.get("platform"):
            confirmed.append("Операционная система подтверждена каталогом")
        return matched, confirmed, unknown

    @staticmethod
    def _license_fit(
        row: sqlite3.Row, constraints: dict
    ) -> tuple[bool, list[str], list[str]]:
        params = json.loads(row["params"])
        fields = [
            (normalize(k), normalize(v)) for k, vals in params.items() for v in vals
        ]
        title = normalize(row["name"])
        licensing = " ".join([title, *(v for k, v in fields if "лиценз" in k)])
        delivery = " ".join(
            [title, *(v for k, v in fields if "доставк" in k or "поставк" in k)]
        )
        confirmed, unknown = [], []
        operation = constraints["operation"]
        if operation:
            renewal = bool(RENEW.search(licensing))
            new = bool(NEW.search(licensing))
            if (operation == "new" and renewal) or (
                operation == "renewal" and new and not renewal
            ):
                return False, [], []
            if (operation == "renewal" and renewal) or (operation == "new" and new):
                confirmed.append("Тип покупки подтверждён каталогом")
            else:
                unknown.append("тип покупки")
        required_delivery = constraints["delivery"]
        if required_delivery:
            usb = bool(USB.search(NO_USB.sub(" ", title)))
            electronic = bool(ELECTRONIC.search(delivery) or NO_USB.search(title))
            if (required_delivery == "electronic" and usb) or (
                required_delivery == "usb" and electronic and not usb
            ):
                return False, [], []
            if (required_delivery == "electronic" and electronic) or (
                required_delivery == "usb" and usb
            ):
                confirmed.append("Формат поставки подтверждён")
            else:
                unknown.append("формат поставки")
        quantity = constraints["quantity"]
        if quantity:
            accepted = SearchEngine._quantity_matches(row, title, fields, quantity)
            if accepted is False:
                return False, [], []
            if accepted:
                confirmed.append(f"Поддерживаемое количество включает {quantity}")
            else:
                unknown.append("допустимое количество")
        return True, confirmed, unknown

    @staticmethod
    def _quantity_matches(
        row: sqlite3.Row, title: str, fields: list[tuple[str, str]], quantity: int
    ) -> bool | None:
        quantity_fields = [v for k, v in fields if COUNT_LABEL.search(k)]
        title_ranges = [
            (int(m[1]), int(m[2]))
            for m in RANGE.finditer(title)
            if COUNT_LABEL.search(title[m.end() : m.end() + 35])
        ]
        ranges = [
            (int(m[1]), int(m[2])) for v in quantity_fields for m in RANGE.finditer(v)
        ] + title_ranges
        declared_range = re.search(
            r"(?:узлов|устройств|рабоч\w* мест|пользователей)\s+от\s+(\d{1,6})\s+до\s+(\d{1,6})",
            normalize(row["description"]),
        )
        if declared_range:
            ranges.append((int(declared_range[1]), int(declared_range[2])))
        exact = [int(v) for v in quantity_fields if v.isdecimal()]
        if not title_ranges:
            exact.extend(int(m[1]) for m in QUANTITY.finditer(title))
        if not (ranges or exact):
            return None
        return quantity in exact or any(a <= quantity <= b for a, b in ranges)

    @staticmethod
    def _item(
        row: sqlite3.Row,
        confirmed: list[str],
        unknown: list[str],
        constraints: dict | None = None,
    ) -> dict:
        path = urlsplit(row["url"]).path.lower().rstrip("/")
        return {
            "sku": row["sku"],
            "group": row["group_id"],
            "code": row["code"],
            "name": row["name"],
            "vendor": row["vendor"],
            "url": row["url"],
            "category": row["category"],
            "category_id": row["category_id"],
            "available": bool(row["available"]),
            "price": row["price"],
            "old_price": row["old_price"],
            "currency": row["currency"],
            "description": row["description"][:350],
            "platform": platform_text(row["description"]),
            "confirmed": confirmed,
            "unknown": unknown,
            "evidence": license_evidence(row, confirmed, constraints)
            if confirmed and constraints
            else [],
            "family_url": "/".join(path.split("/")[:3])
            if path.count("/") >= 3
            else path,
        }

    def search(
        self,
        query: str,
        limit: int = 4,
        *,
        mode: str = "enhanced",
        rewrite: str | None = None,
        preferred_platform: str = "",
    ) -> dict:
        clean = query.strip()[:240]
        tokens, constraints = parse_intent(clean)
        corrections: dict[str, str] = {}
        rewritten_to = rewrite if rewrite and not any(constraints.values()) else None
        if rewritten_to:
            tokens, _ = parse_intent(rewritten_to)
        with closing(self.connect()) as db:
            if mode == "baseline":
                basic = list(dict.fromkeys(words(clean)))[:14]
                rows = self._retrieve(db, basic, BASELINE_COLUMNS, 120)
                ranked = [(-float(row["lexical"]), row, [], []) for row in rows]
                items = self._top_items(ranked, limit, {})
                return {
                    "items": items,
                    "corrections": {},
                    "intent": {},
                    "clarification": None,
                }
            if constraints["conflict"]:
                return {
                    "items": [],
                    "corrections": {},
                    "intent": constraints,
                    "clarification": constraints["conflict"],
                }
            exact = (
                db.execute(
                    "SELECT * FROM products WHERE code = ? COLLATE NOCASE ORDER BY sku",
                    (clean,),
                ).fetchall()
                if clean
                else []
            )
            if exact:
                return {
                    "items": self._top_items(
                        [(0.0, row, [], []) for row in exact], limit, constraints
                    ),
                    "corrections": {},
                    "intent": constraints,
                    "clarification": None,
                }
            corrections = self._correct(db, tokens)
            tokens = list(dict.fromkeys(corrections.get(t, t) for t in tokens))
            role = product_role(tokens)
            rows = self._retrieve_candidates(db, tokens, clean, role)
            wants_astra_os = {"astra", "linux"}.issubset(tokens) and not (
                ASTRA_COMPATIBILITY_INTENTS & set(tokens)
            )
            if wants_astra_os and len(tokens) > 2:
                known_skus = {row["sku"] for row in rows}
                rows.extend(
                    row
                    for row in self._retrieve(db, ["astra", "linux"], RANK_COLUMNS, 100)
                    if row["sku"] not in known_skus
                )
        weighted = self._rank(rows, clean, tokens, constraints, wants_astra_os, role)
        items = self._top_items(weighted, limit, constraints, preferred_platform)
        clarification = None
        main = items[0] if items else None
        if (
            not main
            and "kaspersky" in tokens
            and constraints["operation"] == "renewal"
            and constraints["quantity"]
        ):
            clarification = (
                f"В каталоге не подтверждено продление Kaspersky на "
                f"{constraints['quantity']} устройств. Укажите артикул действующей лицензии."
            )
        elif (
            constraints["operation"] == "renewal"
            and set(tokens) <= {"kaspersky"}
            and main
        ):
            clarification = "Какая редакция лицензии используется сейчас?"
        elif (
            constraints["quantity"]
            and main
            and "допустимое количество" in main["unknown"]
        ):
            clarification = "Уточните редакцию или артикул лицензии, чтобы проверить диапазон устройств."
        return {
            "items": items,
            "corrections": corrections,
            "intent": constraints,
            "clarification": clarification,
            "failure_reason": None
            if items
            else self._failure_reason(tokens, rows, constraints),
            "rewritten_to": rewritten_to,
        }

    @staticmethod
    def _failure_reason(
        tokens: list[str], rows: list[sqlite3.Row], constraints: dict
    ) -> str:
        if not tokens:
            return "missing_product"
        if not rows:
            return "no_catalog_match"
        return (
            "constraints_filtered"
            if any(constraints.values())
            else "irrelevant_matches"
        )

    def _rank(
        self,
        rows: list[sqlite3.Row],
        clean: str,
        tokens: list[str],
        constraints: dict,
        wants_astra_os: bool,
        role: str | None,
    ) -> list[tuple[float, sqlite3.Row, list[str], list[str]]]:
        weighted: list[tuple[float, sqlite3.Row, list[str], list[str]]] = []
        normalized_query = NOT_RENEW.sub(" ", normalize(clean))
        phrase = " ".join(tokens)
        query_terms = set(tokens)
        office_years = OFFICE_YEARS & query_terms
        phrase_trigrams = trigrams(phrase)
        trigram_count = max(1, len(phrase_trigrams))
        has_constraints = any(constraints.values())
        pc_quantity = bool(re.search(r"\b\d+\s*(?:пк|комп\w*)\b", normalize(clean)))
        for row in rows:
            title = normalize(row["name"])
            title_tokens = words(title)
            title_terms = set(title_tokens)
            title_head = re.split(
                r"\s+\(|,\s+|\s+(?:для|на платформе|совместим)\s+", title, maxsplit=1
            )[0]
            head_terms = set(words(title_head))
            identity_terms = set(words(row["vendor"] + " " + row["code"]))
            vendor_path = urlsplit(row["url"]).path.split("/")[1]
            website_vendor = set(words(vendor_path))
            category = normalize(row["category"])
            category_terms = set(words(category))
            if not matches_specialized_license(normalized_query, query_terms, title):
                continue
            if not fits_product_role(role, row, title, category, office_years):
                continue
            if (
                pc_quantity
                and "смартфон" in category
                and "смартфон" not in " ".join(tokens)
            ):
                continue
            fit, confirmed, unknown = (
                self._fit(row, constraints) if has_constraints else (True, [], [])
            )
            if (
                "kaspersky" in query_terms
                and constraints["operation"] == "renewal"
                and constraints["quantity"]
                and unknown
            ):
                continue
            matches = sum(t in title_terms or t in identity_terms for t in tokens)
            if not fit or (
                not role and not matches and not category_terms.intersection(tokens)
            ):
                continue
            lexical = -float(row["lexical"])
            if not self._matches_requested_role(
                query_terms,
                title,
                title_terms,
                vendor_path,
                row["category"],
                category_terms,
                wants_astra_os,
                constraints["quantity"],
            ):
                continue
            full_phrase = len(tokens) > 1 and phrase in title
            contained = {
                t
                for t in tokens
                if t in title_terms or t in identity_terms or t in category_terms
            }
            named_coverage = len(query_terms & head_terms) / len(tokens)
            vendor_fit = bool(query_terms & (identity_terms | website_vendor))
            category_fit = len(query_terms & category_terms) / len(tokens)
            strong_identity = vendor_fit or bool(
                query_terms & head_terms & {"maxpatrol", "anydesk", "криптопро"}
            )
            if (
                not role
                and matches < 2
                and len(tokens) >= 3
                and category_fit < 0.5
                and not strong_identity
                and not code_match_candidate(tokens, row)
            ):
                continue
            coverage = len(contained) / len(tokens)
            similarity = len(phrase_trigrams & trigrams(title)) / trigram_count
            code_match = code_match_candidate(tokens, row)
            renewal_only = bool(RENEW.search(title)) and not constraints["operation"]
            specialty = "сертификат" in title or "техподдержк" in title
            identity_prefix = " ".join(title_tokens[: len(tokens)]) == phrase
            plus_penalty = (
                "расширенный" in tokens
                and "плюс" in title_terms
                and "плюс" not in tokens
            )
            endpoint_bias = (
                constraints["quantity"]
                and "kaspersky" in tokens
                and "endpoint security" in title
            )
            short_term = constraints["operation"] == "renewal" and (
                "на 1 год" in title or "12 мес" in row["description"]
            )
            electronic_title = constraints["delivery"] == "electronic" and bool(
                ELECTRONIC.search(title)
            )
            academic = any(
                stem in title
                for stem in ("академическ", "учебных заведений", "образовательн")
            )
            academic_requested = any(
                stem in normalize(clean)
                for stem in ("школ", "вуз", "академ", "учебн", "университет")
            )
            ambiguous_audience = (
                constraints["operation"] == "renewal"
                and academic
                and not academic_requested
            )
            backup_intent = "бэкап" in tokens or "копирование" in tokens
            backup_brand = backup_intent and "киберпротект" in normalize(row["vendor"])
            backup_category = backup_intent and "резервного копирования" in normalize(
                row["category"]
            )
            identity_boost = any(
                t in head_terms
                for t in {"maxpatrol", "anydesk", "криптопро"} & query_terms
            )
            branded_office = (
                "microsoft" in tokens
                and "microsoft" in head_terms
                and "офисн" in normalize(row["category"])
            )
            family_subscription = (
                branded_office
                and "семьи" in title_terms
                and ("семьи" in tokens or normalize(clean) == "365")
            )
            if constraints["quantity"] and "допустимое количество" in unknown:
                fit_penalty = 75
            else:
                fit_penalty = 0
            score = (
                1.05 * min(lexical, 70)
                + 46 * coverage
                + 34 * named_coverage
                + 22 * vendor_fit
                + 23 * category_fit
                + 13 * full_phrase
                + 10 * similarity
                + 150 * code_match
                + 35 * len(confirmed)
                - 24 * len(unknown)
                + 20 * row["available"]
                - 16 * renewal_only
                - 10 * specialty
                - fit_penalty
                + 16 * identity_prefix
                - 28 * plus_penalty
                + 40 * bool(endpoint_bias)
                + 13 * short_term
                + 14 * electronic_title
                + 38 * backup_brand
                + 22 * backup_category
                + 32 * identity_boost
                + 20 * branded_office
                + 27 * family_subscription
                - 35 * ambiguous_audience
                - specialized_package_penalty(normalized_query, query_terms, title)
            )
            weighted.append((score, row, confirmed, unknown))
        if has_constraints and any(not hit[3] for hit in weighted):
            weighted = [hit for hit in weighted if not hit[3]]
        weighted.sort(
            key=lambda hit: (
                -hit[0],
                int(hit[1]["sku"]) if hit[1]["sku"].isdigit() else 0,
            )
        )
        return weighted

    @staticmethod
    def _matches_requested_role(
        terms: set[str],
        title: str,
        title_terms: set[str],
        vendor_path: str,
        category: str,
        category_terms: set[str],
        wants_astra_os: bool,
        quantity: int | None,
    ) -> bool:
        if "anydesk" in terms and not matches_anydesk_package(terms, title_terms):
            return False
        if "kaspersky" in terms:
            if vendor_path != "kaspersky" and not title.startswith(
                ("kaspersky ", "лаборатория касперского ")
            ):
                return False
            if "расширенный" in terms and (
                "расширенный" not in title_terms
                or ("плюс" in title_terms) != ("плюс" in terms)
            ):
                return False
        server_brands = terms & {"dell", "hpe", "lenovo", "supermicro"}
        if (
            "сервер" in terms
            and server_brands
            and not server_brands & (title_terms | set(words(vendor_path)))
        ):
            return False
        if "проф" in terms and "проф" not in title_terms:
            return False
        if wants_astra_os and not (
            vendor_path == "astralinux"
            and "astra linux" in title
            and "операционн" in normalize(category)
        ):
            return False
        if "субд" in terms and (
            "операционные" in category_terms
            or not (
                title.startswith("субд ")
                or {"база", "базы", "базами", "субд"} & category_terms
            )
        ):
            return False
        if (
            quantity
            and terms & {"endpoint", "компьютеры", "пк"}
            and "смартфон" in normalize(category)
        ):
            return False
        return terms != {"сервер"} or bool({"сервер", "серверы"} & category_terms)

    def _top_items(
        self,
        weighted: list[tuple[float, sqlite3.Row, list[str], list[str]]],
        limit: int,
        constraints: dict,
        preferred_platform: str = "",
    ) -> list[dict]:
        items, seen = [], set()
        preference = (
            re.compile(rf"\b{re.escape(preferred_platform)}\b", re.IGNORECASE)
            if preferred_platform
            else None
        )
        ranked = sorted(
            weighted,
            key=lambda hit: (
                purchase_priority(hit[1]),
                -(
                    hit[0]
                    + 12
                    * bool(
                        preference
                        and preference.search(platform_text(hit[1]["description"]))
                    )
                ),
            ),
        )
        if not ranked or limit <= 0:
            return items
        priority = purchase_priority(ranked[0][1])
        for _, row, confirmed, unknown in ranked:
            if purchase_priority(row) != priority:
                break
            if row["group_id"] in seen:
                continue
            seen.add(row["group_id"])
            items.append(self._item(row, confirmed, unknown, constraints))
            if len(items) == limit:
                break
        return items
