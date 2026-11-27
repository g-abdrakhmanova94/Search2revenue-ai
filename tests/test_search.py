import sqlite3
import tempfile
import unittest
from contextlib import closing
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch
from zipfile import ZipFile

from defusedxml.common import DTDForbidden

from app import Service, handler
from evaluate import cases, family_groups, workbook_xml
from ingest import build
from search import SearchEngine, parse_intent, product_role, quantity_evidence
from search_queue import SearchQueue

FEED = """<?xml version="1.0" encoding="utf-8"?>
<yml_catalog><shop><categories>
<category id="650">Антивирусы для организаций</category>
<category id="2942">Системы автоматизации</category>
<category id="20">Rack-серверы</category>
<category id="21">Серверное ПО</category>
<category id="22">Операционные системы</category>
<category id="23">Программы для работы с базами данных</category>
<category id="24">Офисное ПО</category>
<category id="1033">Программное обеспечение для резервного копирования</category>
<category id="1004">Внешние HDD</category>
</categories><offers>{}</offers></shop></yml_catalog>"""


def offer(
    sku,
    title,
    category,
    code,
    extra="",
    description="",
    available="true",
    vendor_path="test",
    group=None,
    price=1200,
    vendor="Test",
):
    group_attr = f' group_id="{group}"' if group else ""
    return (
        f'<offer id="{sku}" available="{available}"{group_attr}><name>{title}</name><vendor>{vendor}</vendor>'
        f"<categoryId>{category}</categoryId><vendorCode>{code}</vendorCode>"
        f"<url>https://store.softline.ru/{vendor_path}/{sku}/</url><description>{description}</description>"
        f"<price>{price}</price>{extra}</offer>"
    )


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        feed = folder / "data.xml"
        feed.write_text(
            FEED.format(
                "".join(
                    [
                        offer(
                            "11",
                            "Kaspersky Endpoint Security Расширенный продление",
                            "650",
                            "LOW",
                            '<param name="К-во узлов">150</param><param name="К-во узлов">249</param>',
                            "К-во узлов от 150 до 249",
                        ),
                        offer(
                            "12",
                            "Kaspersky Endpoint Security Расширенный продление",
                            "650",
                            "HIGH",
                            '<param name="К-во узлов">250</param><param name="К-во узлов">499</param>',
                            "К-во узлов от 250 до 499",
                        ),
                        offer(
                            "13",
                            "Kaspersky Endpoint Security Расширенный новая лицензия",
                            "650",
                            "NEW",
                        ),
                        offer(
                            "21",
                            "1С: Управление автотранспортом Проф, на 20 рабочих мест, USB",
                            "2942",
                            "USB20",
                            '<param name="Тип поставки">электронная</param>',
                        ),
                        offer(
                            "22",
                            "1С: Управление автотранспортом Проф, на 20 рабочих мест, электронная поставка",
                            "2942",
                            "ESD20",
                        ),
                        offer(
                            "23",
                            "1С: Управление автотранспортом Стандарт, на 20 рабочих мест, электронная поставка",
                            "2942",
                            "STANDARD20",
                        ),
                        offer("31", "Сервер PRO Rack R2", "20", "RACK"),
                        offer("32", "Сервер лицензий, ПО на сервер", "21", "SOFT"),
                        offer(
                            "33",
                            "Astra Linux Special Edition с модулем СУБД",
                            "22",
                            "ASTRAOS",
                            vendor_path="astralinux",
                        ),
                        offer("34", "СУБД PostgreSQL для Astra Linux", "23", "PG"),
                    ]
                )
            ),
            encoding="utf-8",
        )
        index = folder / "catalog.sqlite"
        stats = build(feed, index)
        self.assertEqual(stats["indexed"], 10)
        self.engine = SearchEngine(index)
        self.service = Service(self.engine, folder / "events.sqlite")

    def make_service(self, *offers):
        folder = Path(self.tmp.name)
        feed = folder / "extra.xml"
        index = folder / "extra.sqlite"
        feed.write_text(FEED.format("".join(offers)), encoding="utf-8")
        build(feed, index)
        return Service(SearchEngine(index), folder / "extra-events.sqlite")

    def test_one_card_per_family_and_second_priority_only_as_fallback(self):
        service = self.make_service(
            offer("501", "AnyDesk Standard", "21", "A1", group="remote"),
            offer("502", "AnyDesk Standard", "21", "A2", group="remote"),
            offer("503", "AnyDesk Enterprise", "21", "A3", group="enterprise", price=0),
            offer(
                "504",
                "AnyDesk Enterprise",
                "21",
                "A4",
                group="enterprise",
                available="false",
            ),
        )
        items = service.search("AnyDesk")["items"]
        self.assertEqual([x["group"] for x in items], ["remote"])
        fallback = service.search("A3")["items"]
        self.assertEqual([x["group"] for x in fallback], ["enterprise"])
        self.assertIsNone(fallback[0]["price"])
        self.assertFalse(service.search("A4")["items"][0]["available"])

    def test_family_reference_resolves_every_offer_in_its_catalog_group(self):
        service = self.make_service(
            offer(
                "501",
                "AnyDesk Standard",
                "21",
                "A1",
                group="remote",
                vendor_path="anydesk",
            ),
            offer(
                "502",
                "Legacy subscription",
                "21",
                "A2",
                group="remote",
                vendor_path="anydesk",
            ),
            offer(
                "503",
                "Unrelated item",
                "21",
                "A3",
                group="different",
                vendor_path="anydesk",
            ),
        )
        groups = family_groups(
            service.engine, "https://store.softline.ru/anydesk/anydesk/"
        )
        self.assertEqual(groups, {"remote"})
        self.assertIn(service.search("A2")["items"][0]["group"], groups)

    def test_named_software_rejects_adjacent_products_in_direct_hits(self):
        service = self.make_service(
            offer("501", "Microsoft Office для дома и бизнеса 2024", "24", "O1"),
            offer("502", "Microsoft Office для дома 2024", "24", "O2"),
            offer("503", "Microsoft 365 для семьи", "24", "O3"),
            offer("504", "Kaspersky Security для Microsoft Office 365", "650", "S1"),
            offer("505", "Kaspersky Small Office Security", "650", "S2"),
        )
        for query, sku in (
            ("Microsoft Office для дома и бизнеса", "O1"),
            ("365", "O3"),
            ("kaspersky small office security", "S2"),
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    [item["code"] for item in service.search(query)["items"]], [sku]
                )

    def test_anydesk_distinguishes_base_plan_from_addon_in_the_same_family(self):
        service = self.make_service(
            offer(
                "501", "AnyDesk Standard Custom Namespace", "21", "A1", group="remote"
            ),
            offer("502", "AnyDesk Standard", "21", "A2", group="remote"),
            offer("503", "AnyDesk Solo", "21", "A3", group="remote"),
        )
        self.assertEqual(service.search("AnyDesk Standard")["items"][0]["code"], "A2")
        self.assertEqual(
            service.search("AnyDesk Standard Custom Namespace")["items"][0]["code"],
            "A1",
        )

    def test_base_license_search_respects_upgrade_and_support_prerequisites(self):
        service = self.make_service(
            offer(
                "501",
                "НПО Эшелон Сканер-ВС 7, основная лицензия",
                "21",
                "SCAN",
                group="scanner",
            ),
            offer(
                "502",
                "Сканер-ВС, обновление до 7-й версии",
                "21",
                "UPDATE",
                group="scanner",
            ),
            offer(
                "505",
                "Сканер-ВС 7, дополнительный IP к лицензии",
                "21",
                "EXTRA",
                group="scanner",
            ),
            offer(
                "503",
                "Киберпротект Кибер Бэкап Стандартная редакция, лицензия",
                "1033",
                "BASE",
                group="backup",
            ),
            offer(
                "504",
                "Кибер Бэкап, сертификат на сопровождение",
                "1033",
                "SUPPORT",
                group="backup",
            ),
        )
        for query, code in (
            ("Сканер-ВС", "SCAN"),
            ("Сканер-ВС обновление", "UPDATE"),
            ("Сканер-ВС дополнительный IP", "EXTRA"),
            ("кибер бэкап", "BASE"),
            ("кибер бэкап сопровождение", "SUPPORT"),
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    [item["code"] for item in service.search(query)["items"]], [code]
                )

    def test_workstation_companions_respect_offer_type_and_audience(self):
        service = self.make_service(
            offer(
                "501",
                "Microsoft Office для дома и бизнеса 2024",
                "24",
                "O1",
                description="Платформа: Windows",
            ),
            offer(
                "502",
                "Microsoft 365 для семьи",
                "24",
                "F1",
                description="Платформа: Windows 11",
            ),
            offer(
                "503",
                "Microsoft 365 Персональный",
                "24",
                "F2",
                description="Платформа: Windows 11",
            ),
            offer(
                "504",
                "Новософт Handy Backup (лицензии), стоимость 1 лицензии версии Standard",
                "1033",
                "B1",
                description="Платформа: Windows • Тип лицензии: полная версия • Минимальная покупка: от 1 шт.",
            ),
            offer(
                "505",
                "Новософт Handy Backup (лицензии), стоимость 1 лицензии версии Standard",
                "1033",
                "B10",
                description="Платформа: Windows • Тип лицензии: полная версия • Минимальная покупка: от 10 шт.",
                price=1,
            ),
            offer(
                "506",
                "Kaspersky Small Office Security новая лицензия на 5 ПК",
                "650",
                "S1",
                description="Платформа: Windows 10 • Тип лицензии: полная версия",
            ),
            offer(
                "507",
                "Kaspersky Standard лицензия Base Download Pack Russian Edition",
                "650",
                "S2",
                description="Платформа: Windows 11 • Тип лицензии: полная версия • Тип клиента: физлицо",
            ),
            offer(
                "508",
                "AnyDesk (лицензия на 1 год), Solo",
                "21",
                "R1",
                description="Платформа: Windows • Тип лицензии: полная версия",
            ),
        )
        for query, codes in (
            ("Microsoft Office 2024 для дома и бизнеса", ["S1", "B1", "R1"]),
            (
                "Microsoft Office 2024 для дома и бизнеса, антивирус уже есть",
                ["B1", "R1"],
            ),
            ("365", ["S2", "B1"]),
            ("AnyDesk", ["S1", "B1"]),
            ("Kaspersky Small Office Security", ["B1", "R1"]),
            (
                "Microsoft Office 2024 для дома и бизнеса и резервное копирование",
                ["B1"],
            ),
            ("Microsoft Office 2024 для дома и бизнеса для Mac", []),
        ):
            with self.subTest(query=query):
                result = service.search(query)
                self.assertEqual(
                    [item["code"] for item in result["recommendations"]], codes
                )
                self.assertTrue(
                    all(item["evidence"] for item in result["recommendations"])
                )
                if query == "Microsoft Office 2024 для дома и бизнеса":
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["top_four_count"], 4)
                    self.assertEqual(service.analytics()["four_results"], 1)

    def test_softline_acceptance_handles_negated_editions_and_compatible_alternatives(
        self,
    ):
        service = self.make_service(
            offer(
                "501",
                "Microsoft Office для дома и бизнеса 2024",
                "24",
                "O1",
                description="Платформа: Windows 11",
            ),
            offer("502", "Microsoft 365 для семьи", "24", "M1"),
            offer("503", "Microsoft 365 Персональный", "24", "M2"),
            offer(
                "504",
                "Astra Linux Special Edition 1.8",
                "22",
                "OS",
                vendor_path="astralinux",
            ),
        )
        personal = service.search("Microsoft 365 не семейная")
        self.assertEqual([item["code"] for item in personal["items"]], ["M2"])
        office = "Microsoft Office 2021 для дома и бизнеса"
        alternative = service.search(f"{office} для Windows 11")
        self.assertEqual(alternative["items"], [])
        self.assertEqual(alternative["top_four_count"], 0)
        self.assertEqual([item["code"] for item in alternative["alternatives"]], ["O1"])
        self.assertIn(
            "Windows 11", alternative["alternatives"][0]["evidence"][0]["value"]
        )
        self.assertEqual(service.search(f"{office} для Linux")["alternatives"], [])
        typo = service.search("Astra Linxu спец редакция")
        self.assertEqual(typo["corrections"].get("linxu"), "linux")
        self.assertEqual([item["code"] for item in typo["items"]], ["OS"])

    def test_csp_search_filters_addons_and_other_major_versions(self):
        service = self.make_service(
            offer(
                "501", "КриптоПро CSP 5.0: годовая лицензия", "21", "CSP5", group="csp"
            ),
            offer(
                "502", "КриптоПро CSP 4.0: годовая лицензия", "21", "CSP4", group="csp"
            ),
            offer(
                "503",
                "КриптоПро CSP TLS: расширение для КриптоПро CSP версии 5.0",
                "21",
                "TLS5",
                group="tls",
            ),
            offer(
                "504",
                "КриптоПро TSP Client из состава КриптоПро CSP 5.0",
                "21",
                "TSP5",
                group="tsp",
            ),
        )
        for query, code in (
            ("КриптоПро CSP 5.0", "CSP5"),
            ("КриптоПро CSP 4.0", "CSP4"),
            ("КриптоПро CSP 5.0 TLS", "TLS5"),
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    [item["code"] for item in service.search(query)["items"]], [code]
                )

    def test_anydesk_alternative_uses_full_license_and_preserves_required_edition(self):
        service = self.make_service(
            offer(
                "501",
                "AnyDesk (лицензия на 1 год), Advanced",
                "21",
                "ADV",
                description="Платформа: Windows • Тип лицензии: полная версия",
            ),
            offer("502", "AnyDesk Standard Custom Namespace", "21", "ADD"),
            offer("503", "Remote Support Standard", "21", "UNRELATED"),
        )
        result = service.search("AnyDesk Standard")
        self.assertEqual(result["items"], [])
        self.assertEqual([item["code"] for item in result["alternatives"]], ["ADV"])
        self.assertIn("100", result["alternatives"][0]["reason"])
        self.assertEqual(service.search("AnyDesk Ultimate")["items"], [])
        self.assertEqual(
            service.search("AnyDesk Standard для Linux")["alternatives"], []
        )
        windows = service.search("AnyDesk Standard для Windows 11")["alternatives"]
        self.assertEqual([item["code"] for item in windows], ["ADV"])
        self.assertIn("Операционная система подтверждена", windows[0]["confirmed"])

    def test_anydesk_windows_version_can_use_vendor_evidence(self):
        service = self.make_service(
            offer(
                "501",
                "AnyDesk (лицензия на 1 год), Solo",
                "21",
                "SOLO",
                description="Платформа: Windows • Тип лицензии: полная версия",
            ),
        )
        items = service.search("AnyDesk для Windows 11")["items"]
        self.assertEqual([item["code"] for item in items], ["SOLO"])
        self.assertEqual(items[0]["evidence"][0]["kind"], "vendor")
        self.assertEqual(items[0]["evidence"][0]["value"], "Windows 11")
        self.assertEqual(service.search("AnyDesk для macOS")["items"], [])

    def test_csp_server_companions_respect_platform_and_license_type(self):
        tsp_name = (
            "Лицензия на право использования ПО КриптоПро TSP Client из состава "
            "СКЗИ КриптоПро CSP, версии 5.0 на одном рабочем месте"
        )
        service = self.make_service(
            offer(
                "501",
                "КриптоПро CSP 5.0: серверная лицензия СКЗИ",
                "21",
                "CSP5",
                description="Платформа: Windows • Тип лицензии: полная версия",
            ),
            offer(
                "502",
                "Актив «Рутокен Lite», Rutoken (без упаковки), 1010",
                "21",
                "USB",
                description="Платформа: Windows • Тип лицензии: полная версия",
            ),
            offer(
                "503",
                tsp_name,
                "21",
                "TSP",
                description="Платформа: Windows • Тип лицензии: полная версия • Тип клиента: юрлицо",
            ),
            offer(
                "504",
                tsp_name,
                "21",
                "UP",
                description="Платформа: Windows • Тип лицензии: обновление • Тип клиента: юрлицо",
                price=1,
            ),
        )
        query = "КриптоПро CSP 5.0"
        result = service.search(query, context="windows-server")
        self.assertEqual([item["code"] for item in result["items"]], ["CSP5"])
        companions = result["recommendations"]
        self.assertEqual([item["code"] for item in companions], ["USB", "TSP"])
        self.assertTrue(
            all(
                any(evidence["kind"] == "vendor" for evidence in item["evidence"])
                for item in companions
            )
        )
        for incompatible_query, context in (
            (query, "windows-office"),
            (f"{query} для Windows Server 2022", ""),
            (f"{query} для Linux", ""),
        ):
            with self.subTest(query=incompatible_query, context=context):
                self.assertEqual(
                    service.search(incompatible_query, context=context)[
                        "recommendations"
                    ],
                    [],
                )

    def test_astra_backup_companion_requires_matching_platform_and_intent(self):
        service = self.make_service(
            offer(
                "501",
                "Astra Linux Special Edition 1.8 x86-64",
                "22",
                "OS1",
                vendor_path="astralinux",
            ),
            offer(
                "502",
                "Группа Астра RuBackup OneClick лицензия",
                "1033",
                "B1",
                description="Тип лицензии: полная версия",
            ),
        )
        preview = service.suggest("Astra Linux")
        self.assertEqual(preview["companion_preview"]["code"], "B1")
        self.assertTrue(preview["companion_preview"]["evidence"])
        self.assertEqual(preview["top_four_count"], len(preview["items"]))
        self.assertEqual(service.analytics()["events"], 0)
        self.assertEqual(
            [item["code"] for item in service.search("Astra Linux")["recommendations"]],
            ["B1"],
        )
        self.assertEqual(
            [
                item["code"]
                for item in service.search("Astra Linux и резервное копирование")[
                    "recommendations"
                ]
            ],
            ["B1"],
        )
        for query in (
            "Astra Linux 1.6",
            "Astra Linux 1.6 и резервное копирование",
            "Astra Linux arm64",
            "Astra Linux и защита рабочих мест",
        ):
            with self.subTest(query=query):
                self.assertFalse(service.search(query)["recommendations"])

    def test_distinct_verified_companions_complete_top_four_and_follow_context(self):
        service = self.make_service(
            offer(
                "501",
                "Microsoft Office Standard",
                "24",
                "O1",
                group="standard",
                description="Платформа: Windows 10/Windows 11",
            ),
            offer(
                "502",
                "Microsoft Office Professional",
                "24",
                "O2",
                group="professional",
                description="Платформа: Windows 10/Windows 11",
            ),
            offer(
                "503",
                "Kaspersky Small Office Security",
                "650",
                "S1",
                group="kaspersky",
                description="Платформа: Windows 11",
            ),
            offer(
                "504",
                "Kaspersky Small Office Security",
                "650",
                "S2",
                group="kaspersky",
                description="Платформа: Windows 11",
            ),
            offer(
                "505",
                "Dr.Web Desktop Security",
                "650",
                "S3",
                group="drweb",
                description="Платформа: Windows 11",
            ),
            offer(
                "506",
                "Dr.Web Desktop Security old",
                "650",
                "S4",
                group="legacy",
                description="Платформа: Windows 10",
                price=400,
            ),
        )
        result = service.search("Microsoft Office", context="windows-office")
        self.assertEqual(
            {x["group"] for x in result["items"]}, {"standard", "professional"}
        )
        self.assertEqual(
            [x["group"] for x in result["recommendations"]], ["kaspersky", "drweb"]
        )
        self.assertEqual(result["top_four_count"], 4)
        self.assertTrue(all(x["evidence"] for x in result["recommendations"]))

    def test_renewal_range_uses_exact_catalog_constraints(self):
        for n, expected in ((200, "LOW"), (249, "LOW"), (250, "HIGH"), (300, "HIGH")):
            with self.subTest(n=n):
                items = self.engine.search(
                    f"Касперского расширенного продлить на {n} компов"
                )["items"]
                self.assertEqual([x["code"] for x in items], [expected])

    def test_catalog_verification_requires_search_indexes(self):
        self.engine.verify_catalog()
        incomplete = Path(self.tmp.name) / "incomplete-catalog.sqlite"
        with closing(sqlite3.connect(incomplete)) as db, db:
            db.execute("CREATE TABLE products (sku TEXT)")
            db.execute("INSERT INTO products VALUES ('1')")
            db.execute("CREATE TABLE vocabulary (term TEXT)")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "no such table: catalog"):
            SearchEngine(incomplete).verify_catalog()

    def test_no_usb_rejects_physical_key_even_when_delivery_is_electronic(self):
        items = self.engine.search("1С Автотранспорт Проф на 20 мест без USB")["items"]
        self.assertEqual([x["code"] for x in items], ["ESD20"])

    def test_astra_linux_and_software_for_astra_are_distinct_intents(self):
        os_items = self.engine.search("Astra Linux SE для рабочих станций")["items"]
        db_items = self.engine.search("СУБД для Astra Linux")["items"]
        self.assertEqual([x["code"] for x in os_items], ["ASTRAOS"])
        self.assertEqual([x["code"] for x in db_items], ["PG"])

    def test_generic_server_means_equipment(self):
        self.assertEqual(
            [x["code"] for x in self.engine.search("сервак")["items"]], ["RACK"]
        )

    def test_server_configurations_of_the_same_model_share_one_place(self):
        service = self.make_service(
            offer(
                "1", "Сервер INFERIT RS108 R1G2D24/2x16GB", "20", "RS108A", group="11"
            ),
            offer(
                "2", "Сервер INFERIT RS108 R1G2D24/4x16GB", "20", "RS108B", group="12"
            ),
            offer(
                "3", "Сервер INFERIT RS110 R1G2D24/2x16GB", "20", "RS110A", group="13"
            ),
        )
        codes = [item["code"] for item in service.engine.search("сервак")["items"]]
        self.assertEqual(len(codes), 2)
        self.assertIn("RS110A", codes)

    def test_identical_server_models_with_distinct_offer_ids_share_one_place(self):
        service = self.make_service(
            offer("1", "Rack-сервер DEPO Storm 3470S2A", "20", "DEPO1", group="11"),
            offer("2", "Rack-сервер DEPO Storm 3470S2A", "20", "DEPO2", group="12"),
            offer("3", "Rack-сервер DEPO Storm 3470G2A", "20", "DEPO3", group="13"),
        )
        items = service.engine.search("сервер для установки в стойку")["items"]
        self.assertEqual(len(items), 2)
        self.assertIn("DEPO3", [item["code"] for item in items])

    def test_server_software_does_not_return_hardware_or_training(self):
        service = self.make_service(
            offer("1", "Сервер PRO Rack R2", "20", "RACK"),
            offer("2", "Сервер лицензий, ПО на сервер", "21", "SOFT"),
            offer("3", "Учебный курс по установке на сервер", "2942", "COURSE"),
            offer("4", "UBLinux Server", "22", "SERVEROS"),
        )
        items = service.engine.search("ПО для установки на сервер")["items"]
        self.assertEqual({item["code"] for item in items}, {"SOFT", "SERVEROS"})
        items = service.engine.search("сервер для установки в стойку")["items"]
        self.assertEqual([item["code"] for item in items], ["RACK"])
        named = "КриптоПро CSP для установки на сервер"
        self.assertIsNone(product_role(parse_intent(named)[0], named))

    def test_windows_slang_targets_operating_system_and_version(self):
        service = self.make_service(
            offer("1", "Microsoft Windows 11 Pro", "22", "WIN11"),
            offer("2", "Microsoft Windows 10 Pro", "22", "WIN10"),
            offer("3", "Редактор видео для Windows 11", "2942", "EDITOR"),
        )
        items = service.engine.search("винда")["items"]
        self.assertEqual({item["code"] for item in items}, {"WIN10", "WIN11"})
        items = service.engine.search("винда 11")["items"]
        self.assertEqual([item["code"] for item in items], ["WIN11"])

    def test_no_cloud_requires_proof_of_a_local_edition(self):
        service = self.make_service(
            offer("1", "Example Suite, локальная версия", "24", "LOCAL"),
            offer("2", "Example Suite Cloud", "24", "CLOUD"),
            offer("3", "Example Suite", "24", "UNKNOWN"),
            offer("4", "Microsoft 365 Персональный", "24", "M365"),
        )
        chosen = service.search("Example Suite без облака", record=False)
        self.assertEqual([item["code"] for item in chosen["items"]], ["LOCAL"])
        self.assertEqual(chosen["items"][0]["evidence"][0]["field"], "name")
        for query in (
            "не облачная версия Microsoft 365",
            "Microsoft 365 без облачной подписки",
            "Microsoft 365 без Cloud",
        ):
            with self.subTest(query=query):
                unresolved = service.search(query, record=False)
                self.assertEqual(unresolved["status"], "needs_clarification")
                self.assertEqual(unresolved["items"], [])

    def test_external_ssd_accepts_vendor_verified_code_and_rejects_hdd(self):
        service = self.make_service(
            offer(
                "501",
                "Внешний HDD Samsung T7 1TB",
                "1004",
                "MU-PC1T0H/WW",
                vendor="Samsung",
                description="SSD, 1 ТБ, USB 3.2",
            ),
            offer("502", "Внешний HDD 2 ТБ", "1004", "HDD", description="USB 3.2"),
            offer(
                "503",
                "Внешний HDD Samsung T7 2TB",
                "1004",
                "OTHER",
                vendor="Samsung",
                description="USB 3.2",
            ),
        )
        items = service.search("Внешний SSD USB")["items"]
        self.assertEqual([item["code"] for item in items], ["MU-PC1T0H/WW"])
        self.assertEqual(items[0]["name"], "Внешний SSD Samsung T7 1 ТБ")
        self.assertEqual(
            [fact["kind"] for fact in items[0]["evidence"]], ["feed", "vendor"]
        )

    def test_branded_server_does_not_return_monitors_or_other_brands(self):
        service = self.make_service(
            offer("501", "Сервер Dell PowerEdge", "20", "R1"),
            offer("502", "Монитор Dell для сервера", "21", "D1"),
            offer("503", "Сервер Lenovo ThinkSystem", "20", "R2"),
        )
        self.assertEqual(
            [x["code"] for x in service.search("сервер Dell")["items"]], ["R1"]
        )

    def test_explicit_license_quantity_not_filled_with_incompatible_products(self):
        result = self.service.search("Касперского расширенного продлить на 999 компов")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIn("999 устройств", result["clarification"])

    def test_kaspersky_renewal_excludes_other_vendors_and_editions(self):
        limits = (
            '<param name="К-во узлов">500</param><param name="К-во узлов">999</param>'
        )
        service = self.make_service(
            offer("501", "VK Tech Расширенный продление", "24", "VK", limits),
            offer(
                "502",
                "Kaspersky Endpoint Security Стандартный продление",
                "650",
                "STD",
                limits,
            ),
            offer(
                "503",
                "Kaspersky Endpoint Security Расширенный Плюс продление",
                "650",
                "PLUS",
                limits,
            ),
            offer(
                "504",
                "Kaspersky Endpoint Security Расширенный продление",
                "650",
                "UNKNOWN",
            ),
        )
        query = "Касперского расширенного продлить на 500 компов"
        result = service.search(query)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["recommendations"], [])
        self.assertIn("500 устройств", result["clarification"])
        plus = service.search("Касперского расширенного плюс продлить на 500 компов")
        self.assertEqual([item["code"] for item in plus["items"]], ["PLUS"])

    def test_conflicting_license_requirements_prompt_for_clarification(self):
        examples = (
            ("Kaspersky: новая лицензия или продление на 200 ПК", "новую лицензию"),
            ("1С Автотранспорт Проф с USB, электронная поставка", "формат поставки"),
            ("Kaspersky продление на 200 ПК или 300 ПК", "количество"),
            ("Kaspersky продление на 0 ПК", "количество"),
        )
        for query, prompt in examples:
            with self.subTest(query=query):
                result = self.service.search(query)
                self.assertEqual(result["items"], [])
                self.assertEqual(result["status"], "needs_clarification")
                self.assertIn(prompt, result["clarification"])
        self.assertEqual(
            self.service.analytics()["statuses"]["needs_clarification"], len(examples)
        )

    def test_exact_manufacturer_code_returns_only_matching_sku(self):
        items = self.engine.search("esd20")["items"]
        self.assertEqual([x["code"] for x in items], ["ESD20"])

    def test_suggestions_do_not_increment_events(self):
        self.assertIsNone(self.service.suggest("сервер")["companion_preview"])
        self.assertEqual(self.service.analytics()["events"], 0)
        self.service.search("сервер")
        self.assertEqual(self.service.analytics()["events"], 1)

    def test_query_tokenization_does_not_interpret_fts_operators(self):
        result = self.engine.search('";DROP TABLE products; --')
        self.assertIsInstance(result["items"], list)
        with closing(self.engine.connect()) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM products").fetchone()[0], 10
            )

    def test_electronic_signature_does_not_imply_electronic_delivery(self):
        self.assertIsNone(
            parse_intent("CryptoPro CSP для электронной подписи")[1]["delivery"]
        )

    def test_confirmed_quantity_has_catalog_evidence(self):
        item = self.engine.search("Касперского расширенного продлить на 200 ПК")[
            "items"
        ][0]
        evidence = {fact["field"]: fact["value"] for fact in item["evidence"]}
        self.assertEqual(evidence["description"], "узлов от 150 до 249")
        self.assertIn("продление", evidence["name"])
        self.assertTrue(all(fact["url"] == item["url"] for fact in item["evidence"]))

    def test_quantity_evidence_preserves_the_catalog_range_at_its_upper_bound(self):
        title = "Лицензия на 150–249 узлов"
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT ? AS name, '' AS description, '{}' AS params", (title,)
            ).fetchone()
        self.assertEqual(quantity_evidence(row, 249), ("name", "150–249 узлов"))

    def test_improvement_is_checked_and_applied_only_after_approval(self):
        before = self.service.search("asrta")
        self.assertEqual(before["items"], [])
        self.service.improvements.run_pending()
        private = self.service.analytics(manager=True)
        issue = private["issues"][0]
        self.assertEqual(issue["query"], "asrta")
        self.assertIn(issue["reason"], {"no_catalog_match", "irrelevant_matches"})
        proposal = private["proposals"][0]
        self.assertTrue(proposal["report"]["passed"])
        self.assertTrue(
            all(item["passed"] for item in proposal["report"]["regressions"])
        )
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.service.search("asrta")["items"], [])
        self.assertTrue(self.service.improvements.approve(proposal["id"]))
        self.assertFalse(self.service.improvements.approve(proposal["id"]))
        after = self.service.search("asrta")
        self.assertEqual(after["rewritten_to"], "astra")
        self.assertTrue(after["items"])
        self.assertNotIn("issues", self.service.analytics())
        with closing(self.service.improvements.connect()) as db, db:
            db.execute("UPDATE search_proposal SET catalog_revision=?", ("old",))
        self.assertEqual(self.service.search("asrta")["items"], [])
        self.service.improvements.run_pending()
        rechecked = self.service.analytics(manager=True)["proposals"][0]
        self.assertEqual(rechecked["status"], "pending")
        self.assertTrue(rechecked["report"]["passed"])

    def test_private_query_details_redact_contact_data(self):
        self.service.search("неизвестныйuser@example.com")
        issue = self.service.analytics(manager=True)["issues"][0]
        self.assertEqual(issue["query"], "[скрыто]")
        self.assertEqual(issue["state"], "diagnostic")

    def test_analytics_write_lock_does_not_discard_search_results(self):
        with closing(sqlite3.connect(self.service.log_path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with self.assertLogs("app", level="WARNING"):
                result = self.service.search("ESD20")
            self.assertEqual([item["code"] for item in result["items"]], ["ESD20"])
        self.assertEqual(self.service.analytics()["events"], 0)
        self.service.search("ESD20")
        self.assertEqual(self.service.analytics()["events"], 1)

    def test_search_event_is_counted_when_diagnostics_are_unavailable(self):
        with (
            patch.object(
                self.service.improvements,
                "log_failure",
                side_effect=sqlite3.OperationalError("database is locked"),
            ) as diagnostics,
            self.assertLogs("app", level="WARNING"),
        ):
            result = self.service.search("неизвестныйuser@example.com")
        diagnostics.assert_called_once()
        self.assertEqual(result["status"], "no_results")
        self.assertEqual(self.service.analytics()["events"], 1)

    def test_queue_allows_a_search_to_wait_for_a_slot(self):
        queue = SearchQueue(workers=1, waiting=1, wait_seconds=1)
        attempting = Event()
        outcomes = []

        def request():
            attempting.set()
            with queue.slot() as admitted:
                outcomes.append(admitted)

        with queue.slot() as active:
            self.assertTrue(active)
            worker = Thread(target=request)
            worker.start()
            self.assertTrue(attempting.wait(1))
        worker.join(2)
        self.assertEqual(outcomes, [True])

    def test_snapshot_cache_keeps_responses_and_events_independent(self):
        service = Service(
            self.engine,
            Path(self.tmp.name) / "cached-events.sqlite",
            snapshot_cache=True,
        )
        with patch.object(self.engine, "search", wraps=self.engine.search) as engine:
            first = service.search("ESD20")
            first["items"][0]["code"] = "changed"
            current = service.search("ESD20")
            self.assertEqual(current["items"][0]["code"], "ESD20")
            self.assertEqual(engine.call_count, 1)
        self.assertEqual(service.analytics()["events"], 2)

    def test_saturated_search_reports_busy_and_recovers(self):
        self.service.search_queue = SearchQueue(workers=1, waiting=0)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler(self.service)) as server:
            worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.03})
            worker.start()
            try:
                with self.service.search_queue.slot() as admitted:
                    self.assertTrue(admitted)
                    with closing(
                        HTTPConnection("127.0.0.1", server.server_port)
                    ) as client:
                        client.request("GET", "/api/search?q=astra")
                        reply = client.getresponse()
                        self.assertEqual(reply.status, 503)
                        self.assertEqual(reply.getheader("Retry-After"), "1")
                        reply.read()
                        client.request("GET", "/health")
                        reply = client.getresponse()
                        self.assertEqual(reply.status, 200)
                        reply.read()
                with closing(HTTPConnection("127.0.0.1", server.server_port)) as client:
                    client.request("GET", "/api/search?q=astra")
                    reply = client.getresponse()
                    self.assertEqual(reply.status, 200)
                    reply.read()
            finally:
                server.shutdown()
                worker.join()

    def test_manager_post_checks_origin_and_hides_details_when_disabled(self):
        for enabled, expected in ((True, 409), (False, 403)):
            with ThreadingHTTPServer(
                ("127.0.0.1", 0), handler(self.service, manager_enabled=enabled)
            ) as server:
                worker = Thread(
                    target=server.serve_forever, kwargs={"poll_interval": 0.03}
                )
                worker.start()
                host = f"127.0.0.1:{server.server_port}"
                try:
                    with closing(HTTPConnection(host, timeout=4)) as client:
                        client.request("GET", "/api/analytics")
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        payload = response.read()
                        self.assertEqual(b'"issues"' in payload, enabled)
                    for origin, result in (
                        ("https://example.com", 403),
                        (f"http://{host}", expected),
                    ):
                        with closing(HTTPConnection(host, timeout=4)) as client:
                            client.request(
                                "POST",
                                "/api/improvements/approve",
                                body=b'{"id":1}',
                                headers={
                                    "Origin": origin,
                                    "Content-Type": "application/json",
                                },
                            )
                            response = client.getresponse()
                            self.assertEqual(response.status, result)
                            response.read()
                finally:
                    server.shutdown()
                    worker.join()

    def test_head_checks_the_stand_without_a_response_body_or_search_event(self):
        service = self.make_service(offer("1", "Инструмент", "21", "TOOL"))
        with ThreadingHTTPServer(("127.0.0.1", 0), handler(service)) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for path in ("/", "/health", "/app.js"):
                    with closing(
                        HTTPConnection(*server.server_address, timeout=4)
                    ) as client:
                        client.request("HEAD", path)
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertGreater(int(response.getheader("Content-Length")), 0)
                        self.assertEqual(response.read(), b"")
                self.assertEqual(service.analytics()["events"], 0)
            finally:
                server.shutdown()
                worker.join()


class WorkbookSecurityTest(unittest.TestCase):
    def test_benchmark_ignores_multi_letter_columns(self):
        worksheet = """<worksheet><sheetData><row r="2">
          <c r="A2" t="inlineStr"><is><t>Astra</t></is></c>
          <c r="B2" t="inlineStr"><is><t>https://example.org/astra</t></is></c>
          <c r="AA2" t="inlineStr"><is><t>Notes</t></is></c>
          <c r="BA2" t="inlineStr"><is><t>https://example.org/notes</t></is></c>
        </row></sheetData></worksheet>"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "queries.xlsx"
            with ZipFile(path, "w") as archive:
                archive.writestr("xl/worksheets/sheet1.xml", worksheet)
            self.assertEqual(cases(path), [("Astra", "https://example.org/astra")])

    def test_workbook_rejects_document_type_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.xlsx"
            member = "xl/worksheets/sheet1.xml"
            with ZipFile(path, "w") as workbook:
                workbook.writestr(member, b"<!DOCTYPE worksheet><worksheet/>")
            with ZipFile(path) as workbook, self.assertRaises(DTDForbidden):
                workbook_xml(workbook, member)

    def test_workbook_member_size_is_checked_before_reading(self):
        workbook = Mock(spec=ZipFile)
        workbook.getinfo.return_value.file_size = 32 * 1024 * 1024 + 1
        with self.assertRaisesRegex(ValueError, "too large"):
            workbook_xml(workbook, "xl/worksheets/sheet1.xml")
        workbook.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
