"""Single-writer derived SQLite state and generation-tagged vector cache."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unicodedata

import numpy as np

from .embedding import Profile, validate_vectors
from .extract import prepare_chunks, source_id
from .policy import extraction_policy, legacy_extraction_policy, SCOPE_POLICY, CURRENT_CONTENT_POLICIES

SCHEMA_VERSION = 2
ELIGIBLE_COVERAGE = ("text", "partial-text", "ocr-text")
# These are internal version constants, never interpolated external input.  An
# SQL membership check avoids calling Python once per passage on every status.
_ELIGIBLE_SQL = ("s.coverage IN ('text','partial-text','ocr-text') AND s.extraction_policy IN (" +
                 ",".join("'" + policy + "'" for policy in CURRENT_CONTENT_POLICIES) + ")")


class Store:
    def __init__(self, database: str, profile: Profile):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("current_extraction_policy", 1, extraction_policy, deterministic=True)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            self.db.close()
            raise ValueError(f"Unsupported retrieval schema {version}; repair must preserve this index before rebuilding")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,root_id TEXT NOT NULL,
                stamp TEXT NOT NULL,revision TEXT NOT NULL,title TEXT NOT NULL,coverage TEXT NOT NULL,details TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS excluded_sources(path TEXT PRIMARY KEY,root_id TEXT NOT NULL,
                stamp TEXT NOT NULL,reason TEXT NOT NULL,policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,title TEXT NOT NULL,
                text TEXT NOT NULL,locator TEXT NOT NULL,input TEXT NOT NULL,input_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source_id);
            CREATE TABLE IF NOT EXISTS embeddings(profile TEXT NOT NULL,input_hash TEXT NOT NULL,dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,PRIMARY KEY(profile,input_hash));
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(chunk_id UNINDEXED,title,text,tokenize='unicode61 remove_diacritics 2');
            CREATE VIRTUAL TABLE IF NOT EXISTS vocabulary USING fts5vocab(search,'row');
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,state TEXT NOT NULL,kind TEXT NOT NULL,details TEXT NOT NULL,updated REAL NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sources)")}
        if "extraction_policy" not in columns:
            self.db.execute("ALTER TABLE sources ADD COLUMN extraction_policy TEXT NOT NULL DEFAULT ''")
            for row in self.db.execute("SELECT id,path,details FROM sources").fetchall():
                policy = legacy_extraction_policy(row["path"])
                details = json.loads(row["details"] or "{}")
                details.update(extraction_policy=policy, policy_origin="legacy-inferred")
                if policy == "filename-1" and row["path"]:
                    details.update(extraction_status="unsupported-format", unsupported_format=Path(row["path"]).suffix.lower() or "extensionless")
                self.db.execute("UPDATE sources SET extraction_policy=?,details=? WHERE id=?",
                                (policy, json.dumps(details), row["id"]))
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('generation','0')")
        self.db.execute("UPDATE jobs SET state='interrupted',updated=? WHERE state IN ('running','queued')", (time.time(),))
        self.db.commit()
        self.profile = profile
        self.matrix_generation = -1
        self.matrix = np.empty((0, profile.dimension), dtype=np.float32)
        self.matrix_ids: list[str] = []
        self.matrix_roots: list[str] = []
        self.matrix_groups: list[str] = []

    def close(self):
        self.db.close()

    @property
    def generation(self) -> int:
        return int(self.db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()[0])

    def bump(self):
        self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")

    def files(self) -> dict[str, dict]:
        return {row["path"]: dict(row) for row in self.db.execute("SELECT * FROM sources")}

    def remove(self, path: str):
        row = self.db.execute("SELECT id FROM sources WHERE path=?", (path,)).fetchone()
        if row:
            with self.db:
                self.db.execute("DELETE FROM search WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (row[0],))
                self.db.execute("DELETE FROM chunks WHERE source_id=?", (row[0],))
                self.db.execute("DELETE FROM sources WHERE id=?", (row[0],))
                self.bump()

    def excluded_files(self) -> dict[str, dict]:
        return {row["path"]: dict(row) for row in self.db.execute("SELECT * FROM excluded_sources")}

    def exclude(self, path: str, root_id: str, stamp: str, reason: str):
        existing = self.db.execute("SELECT stamp,reason,policy FROM excluded_sources WHERE path=?", (path,)).fetchone()
        if existing and tuple(existing) == (stamp, reason, SCOPE_POLICY):
            return
        self.remove(path)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO excluded_sources VALUES(?,?,?,?,?)", (path, root_id, stamp, reason, SCOPE_POLICY))
            self.bump()

    def forget_exclusion(self, path: str):
        with self.db:
            if self.db.execute("DELETE FROM excluded_sources WHERE path=?", (path,)).rowcount:
                self.bump()

    def upsert(self, source: dict, root_id: str, stamp: str, revision: str):
        path = source["path"]
        identity = source_id(path)
        parts = prepare_chunks(source)
        policy = extraction_policy(path)
        details = {**source.get("details", {}), "extraction_policy": policy, "policy_origin": "extracted"}
        with self.db:
            self.db.execute("DELETE FROM excluded_sources WHERE path=?", (path,))
            self.db.execute("DELETE FROM search WHERE chunk_id IN (SELECT id FROM chunks WHERE source_id=?)", (identity,))
            self.db.execute("DELETE FROM chunks WHERE source_id=?", (identity,))
            self.db.execute("INSERT OR REPLACE INTO sources(id,path,root_id,stamp,revision,title,coverage,details,error,extraction_policy) VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                            (identity, path, root_id, stamp, revision, source["title"], source.get("coverage", "text"), json.dumps(details), policy))
            for part in parts:
                chunk_id = hashlib.sha256(f"{identity}:{revision}:{part['ordinal']}".encode()).hexdigest()
                self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?)",
                                (chunk_id, identity, part["title"], part["text"], json.dumps(part["locator"], ensure_ascii=False), part["input"], part["input_hash"]))
                self.db.execute("INSERT INTO search(chunk_id,title,text) VALUES(?,?,?)", (chunk_id, part["title"], part["text"]))
            self.bump()

    def error(self, path: str, root_id: str, stamp: str, message: str):
        self.remove(path)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sources(id,path,root_id,stamp,revision,title,coverage,details,error,extraction_policy) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (source_id(path), path, root_id, stamp, "", Path(path).stem, "error", "{}", message[:1000], extraction_policy(path)))
            self.bump()

    def missing_embeddings(self, limit: int = 4) -> list[dict]:
        return [dict(row) for row in self.db.execute(f"""
            SELECT c.input_hash,min(c.input) AS input FROM chunks c
            JOIN sources s ON s.id=c.source_id
            LEFT JOIN embeddings e ON e.input_hash=c.input_hash AND e.profile=?
            WHERE e.input_hash IS NULL AND {_ELIGIBLE_SQL}
            GROUP BY c.input_hash LIMIT ?""", (self.profile.id, limit))]

    def embed(self, rows: list[dict], values):
        vectors = validate_vectors(values, self.profile.dimension)
        if len(rows) != len(vectors):
            raise ValueError("Embedding result count mismatch")
        with self.db:
            for row, vector in zip(rows, vectors, strict=True):
                # Content-addressed cache entries cannot resurrect deleted chunks.
                self.db.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?)",
                                (self.profile.id, row["input_hash"], self.profile.dimension, vector.astype('<f4').tobytes()))
            self.bump()

    def _load_matrix(self):
        generation = self.generation
        if generation == self.matrix_generation:
            return
        rows = self.db.execute("""SELECT c.id,e.dimension,e.vector,s.root_id,c.locator,c.source_id FROM chunks c JOIN sources s ON s.id=c.source_id JOIN embeddings e
                                 ON c.input_hash=e.input_hash AND e.profile=? ORDER BY c.id""", (self.profile.id,)).fetchall()
        ids, vectors, roots, groups = [], [], [], []
        for row in rows:
            if row["dimension"] != self.profile.dimension:
                raise ValueError("Stored vector profile dimension mismatch")
            value = np.frombuffer(row["vector"], dtype='<f4')
            vectors.append(validate_vectors(value, self.profile.dimension)[0])
            ids.append(row["id"])
            roots.append(row["root_id"])
            groups.append(json.loads(row["locator"]).get("id") or row["source_id"])
        self.matrix = np.ascontiguousarray(vectors, dtype=np.float32) if vectors else np.empty((0, self.profile.dimension), dtype=np.float32)
        self.matrix_ids = ids
        self.matrix_roots = roots
        self.matrix_groups = groups
        self.matrix_generation = generation

    def vector_candidates(self, query, limit: int = 100, root_ids: list[str] | None = None) -> list[tuple[str, float]]:
        self._load_matrix()
        vector = validate_vectors(query, self.profile.dimension)[0]
        if not len(self.matrix_ids):
            return []
        scores = self.matrix @ vector
        allowed = np.array([i for i, root in enumerate(self.matrix_roots) if root_ids is None or root in root_ids], dtype=int)
        if not len(allowed):
            return []
        # Select the best passage per note/document before the candidate cap, so
        # one long book cannot crowd every other source out of hybrid retrieval.
        best = {}
        for index in allowed:
            group = self.matrix_groups[index]
            previous = best.get(group)
            if previous is None or scores[index] > scores[previous] or (scores[index] == scores[previous] and self.matrix_ids[index] < self.matrix_ids[previous]):
                best[group] = index
        indices = sorted(best.values(), key=lambda i: (-float(scores[i]), self.matrix_ids[i]))[:limit]
        return [(self.matrix_ids[i], float(scores[i])) for i in indices]

    def lexical_candidates(self, text: str, limit: int = 100, root_ids: list[str] | None = None) -> list[tuple[str, float]]:
        # Reuse FTS's tokenizer for query terms, including its Unicode/diacritic rules.
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.query USING fts5(text, tokenize='unicode61 remove_diacritics 2')")
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.query_terms USING fts5vocab(temp,query,'row')")
        self.db.execute("DELETE FROM temp.query")
        self.db.execute("INSERT INTO temp.query(text) VALUES(?)", (text,))
        total = max(1, self.db.execute("SELECT count(*) FROM chunks").fetchone()[0])
        terms = self.db.execute("""SELECT q.term,q.cnt,coalesce(v.doc,0) AS doc FROM temp.query_terms q
                                  LEFT JOIN vocabulary v ON q.term=v.term WHERE length(q.term)>1""").fetchall()
        import math
        terms = sorted(terms, key=lambda row: (-(1 + math.log(row["cnt"])) * math.log(1 + (total - row["doc"] + .5) / (row["doc"] + .5)), row["term"]))[:32]
        expression = " OR ".join('"' + row["term"].replace('"', '""') + '"' for row in terms)
        if not expression:
            return []
        where = ""
        parameters = [expression]
        if root_ids is not None:
            if not root_ids:
                return []
            where = " AND s.root_id IN (" + ",".join("?" for _ in root_ids) + ")"
            parameters.extend(root_ids)
        parameters.append(limit)
        return [(row[0], -row[1]) for row in self.db.execute(
            "WITH candidates AS MATERIALIZED ("
            "SELECT search.chunk_id,bm25(search,0,3,1) AS score,coalesce(json_extract(c.locator,'$.id'),c.source_id) AS group_id "
            "FROM search JOIN chunks c ON c.id=search.chunk_id JOIN sources s ON s.id=c.source_id WHERE search MATCH ?" + where +
            "), grouped AS (SELECT chunk_id,score,row_number() OVER(PARTITION BY group_id ORDER BY score,chunk_id) AS number FROM candidates) "
            "SELECT chunk_id,score FROM grouped WHERE number=1 ORDER BY score,chunk_id LIMIT ?", parameters)]


    def result(self, chunk_id: str) -> dict | None:
        row = self.db.execute("SELECT c.*,s.path,s.root_id,s.coverage,s.id AS source,s.stamp,s.details FROM chunks c JOIN sources s ON s.id=c.source_id WHERE c.id=?", (chunk_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "source_id": row["source"], "path": row["path"], "root_id": row["root_id"],
                "title": row["title"], "excerpt": row["text"][:600], "locator": json.loads(row["locator"]),
                "coverage": row["coverage"], "coverage_details": json.loads(row["details"] or "{}"), "indexed_stamp": row["stamp"], "stale": False}

    def coverage(self) -> dict:
        sources = self.db.execute("SELECT coverage,count(*) FROM sources GROUP BY coverage").fetchall()
        states = {row[0]: row[1] for row in sources}
        count = self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        eligible = self.db.execute("SELECT count(*) FROM chunks c JOIN sources s ON s.id=c.source_id WHERE " + _ELIGIBLE_SQL).fetchone()[0]
        embedded = self.db.execute("SELECT count(*) FROM chunks c JOIN sources s ON s.id=c.source_id JOIN embeddings e ON e.input_hash=c.input_hash AND e.profile=? WHERE " + _ELIGIBLE_SQL, (self.profile.id,)).fetchone()[0]
        pending_extraction = self.db.execute("SELECT count(*) FROM sources WHERE extraction_policy!=current_extraction_policy(path)").fetchone()[0]
        filename_chunks = self.db.execute("SELECT count(*) FROM chunks c JOIN sources s ON s.id=c.source_id WHERE s.coverage='filename-only'").fetchone()[0]
        unsupported = self.db.execute("SELECT count(*) FROM sources WHERE coverage='filename-only' AND json_extract(details,'$.extraction_status')='unsupported-format'").fetchone()[0]
        exclusions = {row[0]: row[1] for row in self.db.execute("SELECT reason,count(*) FROM excluded_sources GROUP BY reason")}
        return {"sources": sum(states.values()), "chunks": count, "eligible_chunks": eligible,
                "embedded_chunks": embedded, "pending_embedding_chunks": eligible - embedded,
                "pending_extraction_sources": pending_extraction,
                "filename_only_sources": states.get("filename-only", 0), "filename_only_chunks": filename_chunks,
                "unsupported_sources": unsupported, "excluded_sources": sum(exclusions.values()),
                "excluded_by_reason": exclusions, "error_sources": states.get("error", 0),
                "by_state": states, "profile": self.profile.id}

    def errors(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT path,error FROM sources WHERE error IS NOT NULL LIMIT 50")]

    def job(self, job_id: str, state: str, kind: str, details: dict):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)", (job_id, state, kind, json.dumps(details), time.time()))
