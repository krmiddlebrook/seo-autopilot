"""SQLite state: crawled pages, Brand DNA, the topic queue, articles and run logs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    title TEXT, h1 TEXT, description TEXT, text TEXT,
    kind TEXT, lastmod TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT UNIQUE NOT NULL,
    source TEXT, score REAL DEFAULT 0,
    impressions INTEGER, clicks INTEGER, position REAL,
    volume INTEGER, kd REAL, ranking_page TEXT,
    status TEXT DEFAULT 'queued',  -- queued | used | skipped
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE NOT NULL,
    topic_id INTEGER REFERENCES topics(id),
    keyword TEXT, slug TEXT UNIQUE, h1 TEXT,
    meta_title TEXT, meta_description TEXT, excerpt TEXT,
    markdown TEXT, html TEXT, schema_json TEXT,
    score INTEGER, score_report TEXT,
    image_path TEXT, image_url TEXT, image_alt TEXT, image_credit TEXT,
    status TEXT,  -- pending_review | approved | published | rejected | failed
    remote_id TEXT, remote_url TEXT, publish_status TEXT,
    indexnow_at TEXT,
    created_at TEXT, published_at TEXT
);
CREATE TABLE IF NOT EXISTS refreshes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER, keyword TEXT, target_url TEXT,
    mode TEXT,  -- content | meta
    remote_id TEXT, base_hash TEXT,
    old_title TEXT, new_title TEXT, old_description TEXT, new_description TEXT,
    old_body TEXT, new_body TEXT, body_format TEXT,
    ops TEXT, diff TEXT, report TEXT,
    status TEXT,  -- pending_review | applied | rejected | failed
    created_at TEXT, applied_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS keyword_map (
    query TEXT PRIMARY KEY,  -- each query has exactly one page
    url TEXT NOT NULL,
    source TEXT,  -- gsc | article | manual
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS url_inspections (
    url TEXT PRIMARY KEY,
    verdict TEXT,  -- PASS | NEUTRAL | FAIL | '' when the check failed
    coverage_state TEXT, last_crawl TEXT,
    checked_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS conversions (
    url TEXT, date TEXT, count REAL, source TEXT,
    PRIMARY KEY (url, date, source)
);
CREATE TABLE IF NOT EXISTS fact_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER REFERENCES articles(id),
    page_url TEXT, kind TEXT,  -- changed | removed | page_gone
    old_fact TEXT, new_fact TEXT, old_values TEXT, new_values TEXT,
    quote TEXT,  -- the article's own sentence that repeats the old fact
    status TEXT DEFAULT 'open',  -- open | patched (stored copy fixed, live post not) | fixed | dismissed
    note TEXT, created_at TEXT, resolved_at TEXT,
    UNIQUE(article_id, page_url, quote, old_values)
);
CREATE TABLE IF NOT EXISTS page_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT, kind TEXT, primary_query TEXT,
    queries TEXT, findings TEXT, proposal TEXT,  -- JSON
    report TEXT,  -- JSON: the main search's baseline and the change since the page's previous audit
    report_path TEXT,
    status TEXT DEFAULT 'open',  -- open | applied | dismissed
    note TEXT, created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, status TEXT, article_id INTEGER, log TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(articles)")}
        if "unique_value" not in cols:  # databases created before the unique-value check
            self.conn.execute("ALTER TABLE articles ADD COLUMN unique_value TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- key/value -------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), now()),
        )
        self.conn.commit()

    # -- pages -----------------------------------------------------------
    def upsert_page(self, page: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO pages(url,title,h1,description,text,kind,lastmod,fetched_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET title=excluded.title, h1=excluded.h1, description=excluded.description, "
            "text=excluded.text, kind=excluded.kind, lastmod=excluded.lastmod, fetched_at=excluded.fetched_at",
            (
                page["url"], page.get("title", ""), page.get("h1", ""), page.get("description", ""),
                page.get("text", ""), page.get("kind", "page"), page.get("lastmod", ""), now(),
            ),
        )
        self.conn.commit()

    def delete_page(self, url: str) -> None:
        self.conn.execute("DELETE FROM pages WHERE url=?", (url,))
        self.conn.commit()

    def pages(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM pages ORDER BY url")]

    # -- topics ----------------------------------------------------------
    def add_topic(self, keyword: str, source: str, score: float = 0.0, **extra: Any) -> bool:
        """Insert a topic; returns False if the keyword is already known. Re-seen topics refresh their metrics."""
        keyword = keyword.strip().lower()
        cols = ["keyword", "source", "score", "created_at", *extra.keys()]
        vals = [keyword, source, score, now(), *extra.values()]
        updates = ", ".join(f"{c}=excluded.{c}" for c in ["score", *extra.keys()])
        cur = self.conn.execute(
            f"INSERT INTO topics({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
            f"ON CONFLICT(keyword) DO UPDATE SET {updates} WHERE topics.status='queued'",
            vals,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def topics(self, status: str | None = "queued", limit: int = 100) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM topics WHERE status=? ORDER BY score DESC, id LIMIT ?", (status, limit)
            )
        else:
            rows = self.conn.execute("SELECT * FROM topics ORDER BY score DESC, id LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def topic(self, topic_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        return dict(row) if row else None

    def set_topic_status(self, topic_id: int, status: str) -> None:
        self.conn.execute("UPDATE topics SET status=? WHERE id=?", (status, topic_id))
        self.conn.commit()

    # -- articles --------------------------------------------------------
    def insert_article(self, article: dict[str, Any]) -> int:
        article = {**article, "created_at": now()}
        cols = list(article)
        cur = self.conn.execute(
            f"INSERT INTO articles({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [article[c] for c in cols],
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_article(self, article_id: int, **values: Any) -> None:
        if not values:
            return
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE articles SET {sets} WHERE id=?", [*values.values(), article_id])
        self.conn.commit()

    def article(self, article_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        return dict(row) if row else None

    def articles(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM articles WHERE status=? ORDER BY id DESC", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM articles ORDER BY id DESC")
        return [dict(r) for r in rows]

    def known_titles(self) -> Iterable[str]:
        for r in self.conn.execute("SELECT keyword, h1, slug FROM articles WHERE status != 'rejected'"):
            yield from (v for v in (r["keyword"], r["h1"], (r["slug"] or "").replace("-", " ")) if v)
        for r in self.conn.execute("SELECT keyword FROM topics WHERE status='used'"):
            yield r["keyword"]
        for r in self.conn.execute("SELECT title, h1, url FROM pages"):
            yield from (v for v in (r["title"], r["h1"]) if v)
            yield r["url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " ")

    # -- refreshes -------------------------------------------------------
    def insert_refresh(self, refresh: dict[str, Any]) -> int:
        refresh = {**refresh, "created_at": now()}
        cols = list(refresh)
        cur = self.conn.execute(
            f"INSERT INTO refreshes({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            [refresh[c] for c in cols],
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_refresh(self, refresh_id: int, **values: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE refreshes SET {sets} WHERE id=?", [*values.values(), refresh_id])
        self.conn.commit()

    def refresh(self, refresh_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM refreshes WHERE id=?", (refresh_id,)).fetchone()
        return dict(row) if row else None

    def refreshes(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute("SELECT * FROM refreshes WHERE status=? ORDER BY id DESC", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM refreshes ORDER BY id DESC")
        return [dict(r) for r in rows]

    def recent_refresh(self, url: str, since: str) -> dict[str, Any] | None:
        """A refresh of this page that is still waiting for review, or was created or applied since `since`."""
        row = self.conn.execute(
            "SELECT * FROM refreshes WHERE target_url=? AND status != 'rejected' AND status != 'failed' "
            "AND (status='pending_review' OR created_at >= ? OR applied_at >= ?) ORDER BY id DESC LIMIT 1",
            (url, since, since),
        ).fetchone()
        return dict(row) if row else None

    # -- keyword map -----------------------------------------------------
    def map_owner(self, query: str) -> str | None:
        row = self.conn.execute("SELECT url FROM keyword_map WHERE query=?", (query.strip().lower(),)).fetchone()
        return row["url"] if row else None

    def map_set(self, query: str, url: str, source: str, replace: bool = True) -> bool:
        """Assign a query to its one page. With replace=False an existing owner is kept."""
        query = query.strip().lower()
        if not replace and self.map_owner(query):
            return False
        self.conn.execute(
            "INSERT INTO keyword_map(query, url, source, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(query) DO UPDATE SET url=excluded.url, source=excluded.source, updated_at=excluded.updated_at",
            (query, url, source, now()),
        )
        self.conn.commit()
        return True

    def map_delete(self, query: str) -> None:
        self.conn.execute("DELETE FROM keyword_map WHERE query=?", (query.strip().lower(),))
        self.conn.commit()

    def keyword_map(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM keyword_map ORDER BY url, query")]

    # -- index tracking & conversions ------------------------------------
    def save_inspection(self, url: str, verdict: str, coverage_state: str, last_crawl: str = "", error: str = "") -> None:
        self.conn.execute(
            "INSERT INTO url_inspections(url, verdict, coverage_state, last_crawl, checked_at, error) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET verdict=excluded.verdict, coverage_state=excluded.coverage_state, "
            "last_crawl=excluded.last_crawl, checked_at=excluded.checked_at, error=excluded.error",
            (url, verdict, coverage_state, last_crawl, now(), error),
        )
        self.conn.commit()

    def inspections(self) -> dict[str, dict[str, Any]]:
        return {r["url"]: dict(r) for r in self.conn.execute("SELECT * FROM url_inspections")}

    def add_conversions(self, url: str, day: str, count: float, source: str) -> None:
        self.conn.execute(
            "INSERT INTO conversions(url, date, count, source) VALUES(?,?,?,?) "
            "ON CONFLICT(url, date, source) DO UPDATE SET count=excluded.count",
            (url, day, count, source),
        )
        self.conn.commit()

    def clear_conversions(self, source: str, since: str) -> None:
        """Drop a source's rows from `since` on, including its channel-tagged rows (`source:channel`)."""
        self.conn.execute("DELETE FROM conversions WHERE (source = ? OR source LIKE ? || ':%') AND date >= ?",
                          (source, source, since))
        self.conn.commit()

    def conversions(self, since: str = "") -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM conversions WHERE date >= ? ORDER BY date", (since,))]

    # -- fact issues -----------------------------------------------------
    def add_fact_issue(self, article_id: int, page_url: str, kind: str, old_fact: str, new_fact: str,
                       old_values: list[str], new_values: list[str], quote: str) -> int | None:
        """Queue an article sentence that quotes a fact its source page no longer states. None if already queued."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO fact_issues(article_id,page_url,kind,old_fact,new_fact,old_values,new_values,quote,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (article_id, page_url, kind, old_fact, new_fact, json.dumps(old_values), json.dumps(new_values), quote,
             now()),
        )
        self.conn.commit()
        return int(cur.lastrowid) if cur.rowcount else None

    @staticmethod
    def _issue(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["old_values"], d["new_values"] = json.loads(d["old_values"] or "[]"), json.loads(d["new_values"] or "[]")
        return d

    def fact_issues(self, status: str | None = "open", article_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM fact_issues WHERE 1=1", []
        if status:
            sql, args = sql + " AND status=?", [*args, status]
        if article_id is not None:
            sql, args = sql + " AND article_id=?", [*args, article_id]
        return [self._issue(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def fact_issue(self, issue_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM fact_issues WHERE id=?", (issue_id,)).fetchone()
        return self._issue(row) if row else None

    def set_fact_issue(self, issue_id: int, status: str, note: str = "") -> None:
        resolved = now() if status in ("fixed", "dismissed") else None
        self.conn.execute("UPDATE fact_issues SET status=?, note=?, resolved_at=? WHERE id=?",
                          (status, note, resolved, issue_id))
        self.conn.commit()

    # -- runs ------------------------------------------------------------
    # -- core-page audits ------------------------------------------------
    AUDIT_COLUMNS = ("url", "kind", "primary_query", "queries", "findings", "proposal", "report", "report_path",
                     "status", "note", "created_at", "resolved_at")

    def insert_audit(self, audit: dict[str, Any]) -> int:
        row = {"status": "open", "created_at": now(), **audit}
        cols = [c for c in self.AUDIT_COLUMNS if c in row]
        cur = self.conn.execute(f"INSERT INTO page_audits({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                [row[c] for c in cols])
        self.conn.commit()
        return int(cur.lastrowid)

    def update_audit(self, audit_id: int, **values: Any) -> None:
        bad = set(values) - set(self.AUDIT_COLUMNS)
        if bad:
            raise ValueError(f"Unknown audit columns: {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in values)
        self.conn.execute(f"UPDATE page_audits SET {sets} WHERE id=?", [*values.values(), audit_id])
        self.conn.commit()

    def audit(self, audit_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM page_audits WHERE id=?", (audit_id,)).fetchone()
        return dict(row) if row else None

    def audits(self, status: str | None = None, url: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM page_audits WHERE 1=1", []
        if status:
            sql, args = sql + " AND status=?", [*args, status]
        if url:
            sql, args = sql + " AND url=?", [*args, url]
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at, status, log) VALUES(?, 'running', '')", (now(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, log: str, article_id: int | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, log=?, article_id=? WHERE id=?",
            (now(), status, log, article_id, run_id),
        )
        self.conn.commit()
