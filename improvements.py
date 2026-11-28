"""Local failure diagnostics and manager-approved search improvements."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import Lock

from search import SearchEngine, has_purchase_constraints, normalize, parse_intent, words

REASONS = {
    "missing_product": "В запросе не распознано название или тип товара",
    "no_catalog_match": "По словам запроса не найдено совпадений в каталоге",
    "constraints_filtered": "Найденные варианты отсеялись по условиям запроса",
    "irrelevant_matches": "Совпадения были, но не прошли проверку релевантности",
    "conflicting_constraints": "В запросе указаны противоречивые условия",
}
RETAIN_SECONDS = 30 * 86400
SENSITIVE = re.compile(r"\S+@\S+|(?:\+?\d[\d ()-]{7,}\d)|https?://\S+", re.IGNORECASE)
REGRESSIONS = (
    "astra linux",
    "сервер",
    "Microsoft Office для дома и бизнеса",
    "anydesk",
    "резервное копирование cyberprotect",
    "KL4867RASFR",
    "Касперского расширенного продлить на 200 ПК",
    "Касперского расширенного продлить на 300 ПК",
    "1С Автотранспорт Проф на 20 мест без USB",
    "Kaspersky продление на 200 ПК или 300 ПК",
)


def key_for(query: str) -> str:
    return " ".join(normalize(query).split())[:240]


def safe_key(query: str) -> str:
    return SENSITIVE.sub("[скрыто]", key_for(query))


def signature(result: dict) -> dict:
    return {
        "items": [item["sku"] for item in result["items"]],
        "clarification": result["clarification"],
        "intent": result["intent"],
    }


class ImprovementQueue:
    def __init__(self, engine: SearchEngine, database: Path) -> None:
        self.engine = engine
        self.database = database
        self.lock = Lock()
        with closing(self.connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_issue (
                    id INTEGER PRIMARY KEY,
                    query TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    last_seen INTEGER NOT NULL,
                    hits INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    UNIQUE(query, reason)
                );
                CREATE TABLE IF NOT EXISTS search_proposal (
                    id INTEGER PRIMARY KEY,
                    query TEXT NOT NULL UNIQUE,
                    replacement TEXT NOT NULL,
                    report TEXT NOT NULL,
                    catalog_revision TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )

    def connect(self, *, timeout: float = 5) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=timeout)
        db.row_factory = sqlite3.Row
        return db

    def log_failure(self, query: str, reason: str) -> None:
        key = safe_key(query)
        eligible = (
            reason in {"no_catalog_match", "irrelevant_matches"}
            and "[скрыто]" not in key
        )
        state = "queued" if eligible else "diagnostic"
        now = int(time.time())
        with closing(self.connect(timeout=0.25)) as db, db:
            db.execute(
                "DELETE FROM search_issue WHERE last_seen < ?", (now - RETAIN_SECONDS,)
            )
            db.execute(
                """INSERT INTO search_issue(query,reason,last_seen,hits,state) VALUES (?,?,?,1,?)
                   ON CONFLICT(query,reason) DO UPDATE SET
                    hits=search_issue.hits+1, last_seen=excluded.last_seen""",
                (key, reason, now, state),
            )

    def replacement(self, query: str) -> str | None:
        with closing(self.connect(timeout=0.25)) as db:
            row = db.execute(
                "SELECT replacement FROM search_proposal WHERE query=? AND status='approved' AND catalog_revision=?",
                (key_for(query), self.revision()),
            ).fetchone()
            return row["replacement"] if row else None

    def _suggest(self, query: str) -> str | None:
        tokens, constraints = parse_intent(query)
        clean_words = words(query)
        if not tokens or len(tokens) > 8 or has_purchase_constraints(constraints):
            return None
        with closing(self.engine.connect()) as db:
            unfamiliar = [
                token
                for token in tokens
                if not db.execute(
                    "SELECT 1 FROM vocabulary WHERE term=?", (token,)
                ).fetchone()
            ]
            if len(unfamiliar) != 1:
                return None
            word = unfamiliar[0]
            if len(word) < 4 or not word.isalpha() or clean_words.count(word) != 1:
                return None
            choices = {
                word[:i] + word[i + 1] + word[i] + word[i + 2 :]
                for i in range(len(word) - 1)
                if word[i] != word[i + 1]
            }
            matches = sorted(
                candidate
                for candidate in choices
                if db.execute(
                    "SELECT 1 FROM vocabulary WHERE term=?", (candidate,)
                ).fetchone()
            )
        if len(matches) != 1:
            return None
        # Replace the misspelled term without changing punctuation, word order or other words.
        return re.sub(rf"\b{re.escape(word)}\b", matches[0], query, count=1)

    def revision(self) -> str:
        stat = self.engine.database.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def _check(self, query: str, replacement: str) -> dict:
        old = self.engine.search(query)
        new = self.engine.search(query, rewrite=replacement)
        canonical = self.engine.search(replacement)
        expected = signature(old)
        found = signature(new)
        recovered = (
            not expected["items"]
            and bool(found["items"])
            and found["intent"] == expected["intent"]
            and found["items"] == signature(canonical)["items"]
            and not found["clarification"]
        )
        regressions = []
        for sample in REGRESSIONS:
            active = self.replacement(sample)
            before = self.engine.search(sample, rewrite=active)
            after = self.engine.search(
                sample, rewrite=replacement if key_for(sample) == query else active
            )
            regressions.append(
                {"query": sample, "passed": signature(before) == signature(after)}
            )
        return {
            "passed": recovered and all(item["passed"] for item in regressions),
            "before": len(expected["items"]),
            "after": len(found["items"]),
            "regressions": regressions,
            "top": [
                {"sku": item["sku"], "name": item["name"], "url": item["url"]}
                for item in new["items"]
            ],
        }

    def run_pending(self, limit: int = 4) -> None:
        with self.lock:
            with closing(self.connect()) as db, db:
                db.execute(
                    "UPDATE search_proposal SET status='stale' WHERE status IN ('pending', 'approved') AND catalog_revision<>?",
                    (self.revision(),),
                )
                db.execute(
                    """UPDATE search_issue SET state='queued' WHERE state='reviewed'
                       AND query IN (SELECT query FROM search_proposal WHERE status='stale')"""
                )
                pending = db.execute(
                    "SELECT id, query FROM search_issue WHERE state='queued' ORDER BY id LIMIT ?",
                    (limit,),
                ).fetchall()
            for row in pending:
                self._process(row)

    def _process(self, row: sqlite3.Row) -> None:
        query = row["query"]
        substitute = self._suggest(query)
        state = "no_candidate"
        revision = self.revision()
        result = self._check(query, substitute) if substitute else None
        with closing(self.connect()) as db, db:
            if substitute and result:
                status = (
                    "pending"
                    if result["passed"] and revision == self.revision()
                    else "failed"
                )
                db.execute(
                    """INSERT OR IGNORE INTO search_proposal
                       (query,replacement,report,catalog_revision,status) VALUES (?,?,?,?,?)
                       ON CONFLICT(query) DO UPDATE SET replacement=excluded.replacement,
                         report=excluded.report, catalog_revision=excluded.catalog_revision,
                         status=excluded.status WHERE search_proposal.status='stale'""",
                    (
                        query,
                        substitute,
                        json.dumps(result, ensure_ascii=False),
                        revision,
                        status,
                    ),
                )
                state = "reviewed"
            db.execute("UPDATE search_issue SET state=? WHERE id=?", (state, row["id"]))

    def approve(self, proposal_id: int) -> bool:
        with closing(self.connect()) as db, db:
            update = db.execute(
                """UPDATE search_proposal SET status='approved'
                   WHERE id=? AND status='pending' AND catalog_revision=?""",
                (proposal_id, self.revision()),
            )
            return update.rowcount == 1

    def details(self) -> dict:
        cutoff = int(time.time()) - RETAIN_SECONDS
        with closing(self.connect()) as db:
            issues = db.execute(
                """SELECT query,reason,hits,state FROM search_issue
                   WHERE last_seen>=? ORDER BY last_seen DESC, id DESC LIMIT 30""",
                (cutoff,),
            ).fetchall()
            proposals = db.execute(
                """SELECT p.id,p.query,p.replacement,p.status,p.report
                   FROM search_proposal p WHERE p.status='approved' OR EXISTS (
                     SELECT 1 FROM search_issue i WHERE i.query=p.query AND i.last_seen>=?
                   ) ORDER BY p.id DESC LIMIT 20""",
                (cutoff,),
            ).fetchall()
        return {
            "issues": [
                {**dict(row), "explanation": REASONS[row["reason"]]} for row in issues
            ],
            "proposals": [
                {**dict(row), "report": json.loads(row["report"])} for row in proposals
            ],
        }
