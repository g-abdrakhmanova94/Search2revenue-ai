"""Choose distinct companion families supported by the catalog or a vendor source."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from itertools import chain

from search import (
    SearchEngine,
    catalog_evidence,
    csp_scope,
    normalize,
    parse_intent,
    platform_text,
    purchase_priority,
)

PROFILES = {"windows-office": "Windows 11", "windows-server": "Windows Server 2019"}
CRYPTO_SOURCE = "https://cryptopro.ru/products/csp/compare"
OFFICE_SOURCE = "https://support.microsoft.com/en-us/office/system-requirements/office-suites-for-individuals-and-families"
BACKUP_SOURCE = "https://docs.cyberprotect.ru/ru-RU/CyberBackup/19.0/user/supported-operating-systems-and-environments.html"
RUBACKUP_SOURCE = (
    "https://docs.rubackup.ru/oneclickguide/2.9.0.0.0/client-requirements/"
)
RUBACKUP_SERVER_SOURCE = (
    "https://docs.rubackup.ru/oneclickguide/2.9.0.0.0/server-requirements/"
)
GRANULEX_SOURCE = "https://granulex.ru/documentation"
HANDY_SOURCE = "https://www.handybackup.ru/programma-dlya-kopirovania-failov.shtml"
KSOS_SOURCE = "https://www2.kaspersky.com/small-business-security/small-office-security"
ANYDESK_SOURCE = "https://support.anydesk.com/supported-operating-systems"
QNAP_SOURCE = "https://www.qnap.com/en-us/product/ts-233/specs/software?os=qts"
HANDY_NAS_SOURCE = "https://www.handybackup.ru/backup-comparisons.shtml"
SCANNER_SOURCE = "https://scaner-vs.ru/wp-content/uploads/2025/06/%D0%98%D0%BD%D1%81%D1%82%D1%80%D1%83%D0%BA%D1%86%D0%B8%D1%8F-%D0%BF%D0%BE-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D0%B5-%D0%A1%D0%BA%D0%B0%D0%BD%D0%B5%D1%80-%D0%92%D0%A1-7.pdf"
RUTOKEN_SOURCE = "https://dev.rutoken.ru/pages/viewpage.action?pageId=2228237"
RUTOKEN_ACTIVE_SOURCE = (
    "https://developer.rutoken.ru/pages/viewpage.action?pageId=86048961"
)
TSP_SOURCE = "https://www.cryptopro.ru/order/OrderForm.aspx?search=TSP+Client"
ASTRA_VERSION = re.compile(r"(?<!\d)1\.\d+(?!\d)")
X64 = re.compile(r"\b(?:[xх]86[-_– ]64|amd64|[xх]64)\b", re.IGNORECASE)
WINDOWS = re.compile(r"\bwindows\s+(?:server\s+20\d{2}|11|10)\b", re.IGNORECASE)
EXPLICIT_PLATFORM = re.compile(
    r"\b(?:windows|linux|mac|macos|астра|линукс)\b", re.IGNORECASE
)
REQUIRES_EXISTING_LICENSE = re.compile(
    r"продлен|renewal|cross.grade|техподдержк|сертификат"
)


def windows_platforms(text: str) -> set[str]:
    return {" ".join(match.lower().split()) for match in WINDOWS.findall(text)}


def preferred_platform(query: str, context: str) -> str:
    return "" if EXPLICIT_PLATFORM.search(query) else PROFILES.get(context, "")


def office_2024_source(primary: dict) -> dict[str, str] | None:
    title = normalize(primary["name"])
    if title.startswith(
        "microsoft office для дома и бизнеса 2024"
    ) and "windows" in normalize(primary["platform"]):
        return {
            "kind": "vendor",
            "field": "Office Home & Business 2024 · Operating system",
            "value": "Windows 11",
            "url": OFFICE_SOURCE,
        }
    return None


def is_anydesk_windows(primary: dict) -> bool:
    title = normalize(primary["name"])
    return bool(
        re.fullmatch(
            r"anydesk \(лицензия на 1 год\), (?:solo|standard|advanced)", title
        )
        and "windows" in normalize(primary["platform"])
    )


def is_kaspersky_windows_office(primary: dict) -> bool:
    return normalize(primary["name"]).startswith(
        "kaspersky small office security"
    ) and "windows" in normalize(primary["platform"])


def windows_infrastructure(primary: dict, query: str, context: str) -> set[str]:
    explicit = windows_platforms(query)
    if not explicit and re.search(
        r"linux|\bmac(?:os)?\b|астра|линукс", query, re.IGNORECASE
    ):
        return set()
    requested = explicit or windows_platforms(PROFILES.get(context, ""))
    if is_anydesk_windows(primary) or is_kaspersky_windows_office(primary):
        return {"windows 11"} & requested if requested else {"windows 11"}
    if "сервер" in normalize(primary["category"]):
        return {os for os in requested if "server" in os}
    if "офисн" not in normalize(primary["category"]):
        return set()
    supported = windows_platforms(primary["platform"])
    if office_2024_source(primary):
        supported.add("windows 11")
    return supported & requested if requested else supported


def verified_astra_backup_platform(primary: dict, query: str) -> bool:
    actual = set(ASTRA_VERSION.findall(primary["name"]))
    requested = set(ASTRA_VERSION.findall(query))
    return bool(
        actual
        and (actual | requested) <= {"1.7", "1.8"}
        and (not requested or requested == actual)
        and X64.search(primary["name"])
    )


def rubackup_candidates(db):
    return db.execute(
        """SELECT * FROM products WHERE category_id='1033'
           AND name LIKE '%RuBackup OneClick%'
           AND description LIKE '%полная версия%'"""
    )


def astra_candidates(db, requested: str, primary: dict, query: str):
    preference = normalize(requested)
    if not normalize(primary["name"]).startswith("astra linux special edition"):
        return ()
    verified_backup_platform = verified_astra_backup_platform(primary, query)
    if any(stem in preference for stem in ("резервн", "бэкап")):
        if not verified_backup_platform:
            return ()
        granulex = db.execute(
            """SELECT * FROM products WHERE name LIKE 'Granulex Recovery%'
               AND name LIKE '%Astra Linux%' AND name LIKE '%резервного копирования%'"""
        )
        return chain(granulex, rubackup_candidates(db))
    if "антивирус" in preference:
        return ()
    security = db.execute(
        """SELECT * FROM products
           WHERE (category_id='687' AND name LIKE '%Astra Linux SE%'
               AND name LIKE '%CSP версии 5.0%' AND name LIKE '%на одном%'
               AND description LIKE '%полная версия%' AND description LIKE '%Linux%'
               AND description NOT LIKE '%Linpus%')
              OR (category_id='687' AND name LIKE '%генератор паролей%'
               AND description LIKE '%Astra Linux Special Edition%')"""
    )
    return (
        chain(security, rubackup_candidates(db))
        if not preference and verified_backup_platform
        else security
    )


def windows_candidates(db, primary: dict, requested: str):
    category = normalize(primary["category"])
    preference = normalize(requested)
    if "офисн" in category and not any(
        stem in preference for stem in ("резервн", "бэкап")
    ):
        return db.execute(
            """SELECT * FROM products WHERE category_id='650' AND description LIKE '%Windows%'
               AND (name LIKE '%Kaspersky Small Office Security%' OR name LIKE '%Dr.Web Desktop Security%')"""
        )
    if (
        "сервер" in category
        and "по" not in category.split()
        and "антивирус" not in preference
    ):
        return db.execute(
            """SELECT * FROM products WHERE category_id='1033' AND
               ((description LIKE '%Windows Server%' AND name LIKE '%Server%')
                OR (name LIKE '%Кибер Бэкап%' AND name LIKE '%физического сервера%'
                    AND description LIKE '%Windows%' AND description LIKE '%полная версия%'))"""
        )
    return ()


def compatibility_evidence(
    row: sqlite3.Row, *, astra: bool, platforms: set[str]
) -> tuple[str, list[dict[str, str]]]:
    name = row["name"]
    title = normalize(name)
    platform = platform_text(row["description"])
    if astra and "криптопро csp" in title:
        return "Криптографическая защита и электронная подпись для Astra Linux SE.", [
            catalog_evidence("name", name, row["url"]),
            {
                "kind": "vendor",
                "field": "КриптоПро CSP 5.0 · ОС",
                "value": "Astra Linux — x64 / MIPS / Эльбрус",
                "url": CRYPTO_SOURCE,
            },
        ]
    if astra and title.startswith("granulex recovery"):
        return (
            "Резервное копирование и выборочное восстановление объектов Linux-каталога.",
            [
                catalog_evidence("name", name, row["url"]),
                {
                    "kind": "vendor",
                    "field": "Granulex Recovery · Документация",
                    "value": "Сертификаты для Astra Linux 1.7 и 1.8",
                    "url": GRANULEX_SOURCE,
                },
            ],
        )
    if astra and "rubackup oneclick" in title:
        return (
            "Резервное копирование инфраструктуры. Для серверной части нужно отдельное подходящее оборудование.",
            [
                catalog_evidence("name", name, row["url"]),
                {
                    "kind": "vendor",
                    "field": "RuBackup OneClick · Клиент",
                    "value": "Astra Linux 1.7 и 1.8",
                    "url": RUBACKUP_SOURCE,
                },
                {
                    "kind": "vendor",
                    "field": "RuBackup OneClick · Сервер",
                    "value": "Astra Linux SE 1.8 и оборудование по системным требованиям",
                    "url": RUBACKUP_SERVER_SOURCE,
                },
            ],
        )
    if (
        astra
        and "генератор паролей" in title
        and "astra linux special edition" in normalize(platform)
    ):
        return "Генерация паролей на рабочих местах Astra Linux SE.", [
            catalog_evidence("description", platform, row["url"])
        ]
    shared = platforms & windows_platforms(platform)
    if (
        "windows server 2019" in platforms
        and row["category_id"] == "1033"
        and "кибер бэкап" in title
        and "физического сервера" in title
        and "windows" in normalize(platform)
    ):
        return (
            "Резервное копирование физического сервера. Проверьте версию, доступную по лицензии.",
            [
                catalog_evidence("name", name, row["url"]),
                catalog_evidence("description", platform, row["url"]),
                {
                    "kind": "vendor",
                    "field": "Кибер Бэкап 19.0 · Агент",
                    "value": "64-битные Windows Server 2019 Standard и Datacenter",
                    "url": BACKUP_SOURCE,
                },
            ],
        )
    if not shared:
        return "", []
    purpose = (
        "Резервное копирование"
        if row["category_id"] == "1033"
        else "Защита рабочих мест"
    )
    return f"{purpose}. Совместимая платформа — {', '.join(sorted(shared))}.", [
        catalog_evidence("description", platform, row["url"])
    ]


def compatible_item(
    engine: SearchEngine,
    row: sqlite3.Row,
    constraints: dict,
    *,
    astra: bool,
    platforms: set[str],
) -> dict | None:
    if not constraints["operation"] and REQUIRES_EXISTING_LICENSE.search(
        normalize(row["name"])
    ):
        return None
    fits, confirmed, unknown = engine._fit(row, constraints)
    if not fits or unknown:
        return None
    reason, evidence = compatibility_evidence(row, astra=astra, platforms=platforms)
    if not evidence:
        return None
    item = engine._item(row, confirmed, [], constraints)
    item["reason"] = reason
    item["source"] = evidence[-1]["url"]
    item["evidence"].extend(evidence)
    return item


def available_first(items: list[dict]) -> list[dict]:
    available = [item for item in items if item["available"] and item["price"]]
    return available or items


@dataclass(frozen=True)
class CompanionOffer:
    purpose: str
    phrase: str
    name: str
    description: str
    reason: str
    source: str
    feature: str = ""


FILE_BACKUP = CompanionOffer(
    "backup",
    "handy backup",
    "Новософт Handy Backup (лицензии), стоимость 1 лицензии версии Standard",
    "%Тип лицензии: полная версия%Минимальная покупка: от 1 шт.%",
    "Резервное копирование локальных документов и папок на ПК Windows.",
    HANDY_SOURCE,
)
BACKUP_NAS = CompanionOffer(
    "nas",
    "ts-233",
    "Сетевое хранилище QNAP 2 disks TS-233",
    "%SATA%отсеки%",
    "Если нужна отдельная сетевая папка для резервных копий, TS-233 доступен по SMB. Проверьте комплектацию дисками.",
    QNAP_SOURCE,
    "Файловый сервер, Microsoft networking (CIFS/SMB)",
)
SCANNER_OS = CompanionOffer(
    "scanner-os",
    "astra linux special edition 1.8",
    "Astra Linux Special Edition 1.8,%«Орел»%для рабочей станции%",
    "%Платформа: Linux%Тип лицензии: полная версия%Минимальная покупка: от 1 шт.%",
    "Вариант ОС для нового развёртывания Сканера-ВС 7 на x86-64. Сверьте нужный уровень защищённости.",
    SCANNER_SOURCE,
    "Инструкция по установке Сканера-ВС 7 на Astra Linux 1.7 и 1.8",
)
OFFICE_PROTECTION = CompanionOffer(
    "security",
    "kaspersky small office security",
    "Kaspersky Small Office Security новая лицензия на 5 ПК%",
    "%Платформа:%Windows%Тип лицензии: полная версия%",
    "Защита до пяти устройств малого офиса. Для Windows 11 нужен поддерживаемый дистрибутив.",
    KSOS_SOURCE,
)
HOME_PROTECTION = CompanionOffer(
    "security",
    "kaspersky standard",
    "Kaspersky Standard лицензия Base Download Pack Russian Edition%",
    "%Платформа:%Windows 11%Тип лицензии: полная версия%Тип клиента: физлицо%",
    "Защита домашнего ПК. В фиде указаны Windows 11 и лицензия для физлиц.",
    "",
)
REMOTE_SUPPORT = CompanionOffer(
    "remote",
    "anydesk",
    "AnyDesk (лицензия на 1 год), Solo",
    "%Платформа: Windows%Тип лицензии: полная версия%",
    "Удалённая поддержка ПК Windows. Solo рассчитана на одного пользователя.",
    ANYDESK_SOURCE,
)
SIGNATURE_TOKEN = CompanionOffer(
    "key",
    "рутокен lite",
    "Актив «Рутокен Lite», Rutoken (без упаковки), 1010",
    "%Платформа: Windows%Тип лицензии: полная версия%",
    "Пассивный носитель ключевого контейнера для Windows. Подходит, если нужен локальный USB-токен.",
    RUTOKEN_SOURCE,
)
SIGNATURE_TIMESTAMP = CompanionOffer(
    "timestamp",
    "tsp client",
    "Лицензия на право использования ПО КриптоПро TSP Client из состава СКЗИ КриптоПро CSP, версии 5.0%",
    "%Платформа: Windows%Тип лицензии: полная версия%Тип клиента: юрлицо%",
    "Лицензия на узел Windows, где подпись получает метку времени. Нужна для форматов подписи с меткой времени.",
    TSP_SOURCE,
)
SIGNATURE_HARDWARE_TOKEN = CompanionOffer(
    "hardware-key",
    "рутокен эцп flash",
    "Актив «Рутокен ЭЦП Flash», Rutoken (без упаковки), 2.0 128КБ 32ГБ, серт. ФСТЭК",
    "%Платформа: Windows%Тип лицензии: полная версия%",
    "Выберите этот носитель вместо Рутокен Lite, если закрытый ключ должен оставаться в памяти токена при подписании.",
    RUTOKEN_ACTIVE_SOURCE,
    "Рутокен ЭЦП 2.0 Flash поддерживает в КриптоПро CSP 5.0 режим ФКН без защиты канала.",
)


def workstation_offers(primary: dict) -> tuple[CompanionOffer, ...]:
    if office_2024_source(primary):
        return OFFICE_PROTECTION, FILE_BACKUP, REMOTE_SUPPORT
    if normalize(primary["name"]).startswith(
        ("microsoft 365 для семьи", "microsoft 365 персональный")
    ):
        return HOME_PROTECTION, FILE_BACKUP
    if is_anydesk_windows(primary):
        return OFFICE_PROTECTION, FILE_BACKUP
    if is_kaspersky_windows_office(primary):
        return FILE_BACKUP, REMOTE_SUPPORT, BACKUP_NAS
    return ()


def scanner_primary_evidence(
    primary: dict, query: str, context: str, requested: str
) -> dict | None:
    platform = normalize(primary["platform"])
    query = normalize(query)
    title = normalize(primary["name"])
    if (
        requested
        or "сканер-вс 7" not in title
        or "основная лицензия" not in title
        or "astra linux special edition" not in platform
        or "1.8" not in platform
        or context
        or re.search(r"windows|виндоус|macos|arm(?:64)?|aarch64|\b1\.7\b", query)
    ):
        return None
    return catalog_evidence("platform", primary["platform"], primary["url"])


def csp_offers(
    primary: dict, query: str, context: str, requested: str
) -> tuple[CompanionOffer, ...]:
    title = normalize(primary["name"])
    server = title == "криптопро csp 5.0: серверная лицензия скзи"
    workplace = csp_scope(title) == "workplace" and title.startswith(
        (
            "криптопро csp 5.0: годовая лицензия",
            "криптопро csp (бессрочная лицензия скзи",
        )
    )
    if (
        requested
        or not (server or workplace)
        or "windows" not in normalize(primary["platform"])
        or re.search(
            r"\b(?:linux|macos|mac|астра|линукс|arm64)\b", query, re.IGNORECASE
        )
    ):
        return ()
    platforms = windows_platforms(query) or windows_platforms(PROFILES.get(context, ""))
    supported = (
        {"windows server 2016", "windows server 2019"}
        if server
        else {
            "windows 10",
            "windows 11",
        }
    )
    if platforms and platforms - supported:
        return ()
    text = normalize(query)
    if re.search(r"\b(?:без|не нужен|не нужно)\s+(?:usb|токен\w*|рутокен\w*)", text):
        return (SIGNATURE_TIMESTAMP,)
    if re.search(r"неизвлекаем\w*\s+ключ|аппаратн\w*\s+ключ|\bфкн\b", text):
        return SIGNATURE_HARDWARE_TOKEN, SIGNATURE_TIMESTAMP
    return SIGNATURE_TOKEN, SIGNATURE_TIMESTAMP, SIGNATURE_HARDWARE_TOKEN


def workstation_purposes(requested: str, query: str) -> set[str]:
    text = normalize(requested)
    if any(stem in text for stem in ("резервн", "бэкап")):
        return {"backup"}
    if any(stem in text for stem in ("защит", "антивирус")):
        return {"security"}
    purposes = {"backup", "security", "remote"} if not text else set()
    if not text and not re.search(
        r"\bбез\s+(?:nas|сетев\w*\s+хранилищ)", normalize(query)
    ):
        purposes.add("nas")
    return purposes


def workstation_primary_evidence(primary: dict) -> dict[str, str]:
    office = office_2024_source(primary)
    if office:
        return office
    if is_anydesk_windows(primary):
        return {
            "kind": "vendor",
            "field": "AnyDesk · ОС",
            "value": "Windows 11",
            "url": ANYDESK_SOURCE,
        }
    if is_kaspersky_windows_office(primary):
        return {
            "kind": "vendor",
            "field": "Kaspersky Small Office Security · Системные требования",
            "value": "Windows 11 с поддерживаемым дистрибутивом",
            "url": KSOS_SOURCE,
        }
    return catalog_evidence("platform", primary["platform"], primary["url"])


def items_for_offers(
    engine: SearchEngine,
    direct: list[dict],
    offers: tuple[CompanionOffer, ...],
    purposes: set[str],
    constraints: dict,
    primary_evidence: dict[str, str],
    *,
    limit: int,
) -> list[dict]:
    seen = {item["group"] for item in direct}
    items = []
    with closing(engine.connect()) as db:
        for offer in offers:
            if offer.purpose not in purposes:
                continue
            rows = db.execute(
                """SELECT p.* FROM catalog JOIN products p ON p.rowid = catalog.rowid
                   WHERE catalog MATCH ? AND p.name LIKE ? AND p.description LIKE ?
                   ORDER BY p.available DESC, p.price IS NULL, p.price, p.sku""",
                (f'name : "{offer.phrase}"', offer.name, offer.description),
            )
            for row in rows:
                if offer is SCANNER_OS and not X64.search(row["name"]):
                    continue
                matched, confirmed, unknown = engine._fit(row, constraints)
                if not matched or unknown or row["group_id"] in seen:
                    continue
                item = engine._item(row, confirmed, [], constraints)
                item["reason"] = offer.reason
                item["source"] = offer.source or row["url"]
                item["evidence"].append(
                    catalog_evidence("description", row["description"], row["url"])
                )
                item["evidence"].append(primary_evidence)
                item["evidence"].extend(offer_evidence(offer))
                seen.add(row["group_id"])
                items.append(item)
                break
            if len(items) == limit:
                break
    return available_first(items)


def offer_evidence(offer: CompanionOffer) -> list[dict[str, str]]:
    if not offer.feature:
        return []
    facts = [
        {
            "kind": "vendor",
            "field": "Документация",
            "value": offer.feature,
            "url": offer.source,
        }
    ]
    if offer is BACKUP_NAS:
        facts.append(
            {
                "kind": "vendor",
                "field": "Handy Backup Standard",
                "value": "Поддерживаются общие сетевые папки и NAS",
                "url": HANDY_NAS_SOURCE,
            }
        )
    return facts


def compatible_infrastructure_items(
    engine: SearchEngine,
    rows,
    seen: set[str],
    constraints: dict,
    *,
    astra: bool,
    platforms: set[str],
    limit: int,
) -> list[dict]:
    ordered = sorted(
        rows,
        key=lambda row: (
            purchase_priority(row),
            row["price"] or float("inf"),
            row["sku"],
        ),
    )
    items = []
    for row in ordered:
        group = row["group_id"]
        if group in seen:
            continue
        item = compatible_item(
            engine, row, constraints, astra=astra, platforms=platforms
        )
        if item is None:
            continue
        seen.add(group)
        items.append(item)
        if len(items) == limit:
            break
    return available_first(items)


def specialized_offers(
    primary: dict, query: str, context: str, requested: str
) -> tuple[tuple[CompanionOffer, ...], dict[str, str]] | None:
    scanner_evidence = scanner_primary_evidence(primary, query, context, requested)
    if scanner_evidence:
        return (SCANNER_OS,), scanner_evidence
    signature_offers = csp_offers(primary, query, context, requested)
    if signature_offers:
        server = csp_scope(normalize(primary["name"])) == "server"
        return signature_offers, {
            "kind": "vendor",
            "field": "КриптоПро CSP 5.0 · поддерживаемые ОС",
            "value": "Windows Server 2016/2019 x64" if server else "Windows 10/11",
            "url": CRYPTO_SOURCE,
        }
    return None


def companion_items(
    engine: SearchEngine,
    direct: list[dict],
    query: str,
    requested: str,
    context: str,
    *,
    limit: int,
) -> list[dict]:
    if not direct:
        return []
    primary = direct[0]
    constraints = parse_intent(requested)[1]
    if constraints["conflict"]:
        return []
    specialized = specialized_offers(primary, query, context, requested)
    if specialized:
        offers, evidence = specialized
        return items_for_offers(
            engine,
            direct,
            offers,
            {offer.purpose for offer in offers},
            constraints,
            evidence,
            limit=limit,
        )
    astra = "операционн" in normalize(
        primary["category"]
    ) and "astra linux" in normalize(primary["name"])
    if astra and re.search(
        r"\b(?:arm64|aarch64|armv\d+|risc-v)\b", query, re.IGNORECASE
    ):
        return []
    platforms = windows_infrastructure(primary, query, context)
    if not astra and not platforms:
        return []
    offers = workstation_offers(primary)
    if offers:
        return (
            items_for_offers(
                engine,
                direct,
                offers,
                workstation_purposes(requested, query),
                constraints,
                workstation_primary_evidence(primary),
                limit=limit,
            )
            if "windows 11" in platforms
            else []
        )
    seen = {item["group"] for item in direct}
    with closing(engine.connect()) as db:
        rows = (
            astra_candidates(db, requested, primary, query)
            if astra
            else windows_candidates(db, primary, requested)
        )
        return compatible_infrastructure_items(
            engine,
            rows,
            seen,
            constraints,
            astra=astra,
            platforms=platforms,
            limit=limit,
        )
