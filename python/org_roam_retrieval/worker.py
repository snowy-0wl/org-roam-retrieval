"""Persistent NDJSON JSON-RPC process with one state-owning scheduler."""
from __future__ import annotations

from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import hashlib
import heapq
import itertools
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import uuid

from . import PROTOCOL_VERSION
from .embedding import Profile, SentenceTransformerEncoder
from .extract import document, org_batch
from .store import Store
from .policy import ORG_EXTENSIONS, exclusion_reason, extraction_policy

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


class Service:
    def __init__(self, send, encoder_factory=SentenceTransformerEncoder):
        self.send = send
        self.encoder_factory = encoder_factory
        self.store: Store | None = None
        self.encoder = None
        self.roots: list[dict] = []
        self.jobs: dict[str, dict] = {}
        self.cancelled_requests: set[int | str] = set()
        self.cancelled_jobs: set[str] = set()
        self.config = {}
        self.model_error = None
        self.tasks = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.stopping = False
        self.last_reconcile = 0.0
        self.embedding_scheduled = False
        self.unavailable_roots: list[str] = []
        self.ocr_status = {"state": "not-probed", "visual_embeddings": False}
        self.extraction_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="org-roam-extraction")
        self.extraction_queue = deque()
        self.extraction_busy = False
        self.dirty_pending = set()
        self.inflight_sources = {}
        self.model_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="org-roam-model")
        self.model_queue = []
        self.model_busy = False
        self.query_vectors = {}
        self.pending_queries = {}
        self.current_request_id = None

    def enqueue(self, priority, function, *arguments):
        self.tasks.put((priority, next(self.sequence), function, arguments))

    def event(self, method: str, params: dict):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def accept(self, message: dict):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            self.send({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                       "error": {"code": -32600, "message": "Invalid JSON-RPC request"}})
            return
        method = message["method"]
        params = message.get("params", {})
        if params is None:
            params = {}
        identity = message.get("id")
        if not isinstance(params, dict):
            self.send({"jsonrpc": "2.0", "id": identity, "error": {"code": -32602, "message": "params must be an object"}})
            return
        if method == "cancel":
            if "request_id" in params:
                self.cancelled_requests.add(params["request_id"])
            if "job_id" in params:
                self.cancelled_jobs.add(params["job_id"])
            if identity is not None:
                self.send({"jsonrpc": "2.0", "id": identity, "result": {"accepted": True, "in_flight_inference_may_finish": True}})
            return
        if method == "shutdown":
            self.stopping = True
            if identity is not None:
                self.send({"jsonrpc": "2.0", "id": identity, "result": {"stopping": True}})
            self.enqueue(-100, lambda: None)
            return
        self.enqueue(0, self.dispatch, identity, method, params)

    def dispatch(self, identity, method: str, params: dict):
        try:
            if identity in self.cancelled_requests:
                raise RpcError(-32800, "Request cancelled")
            if method != "initialize" and self.store is None:
                raise RpcError(-32002, "Worker must be initialized first")
            operation = getattr(self, f"rpc_{method}", None)
            if operation is None or method.startswith("_"):
                raise RpcError(-32601, f"Unknown method: {method}")
            self.current_request_id = identity
            result = operation(params)
            if identity in self.cancelled_requests:
                raise RpcError(-32800, "Request cancelled; in-flight work completed but its result was discarded")
            if identity is not None:
                self.send({"jsonrpc": "2.0", "id": identity, "result": result})
        except Exception as error:
            if identity is not None:
                self.send({"jsonrpc": "2.0", "id": identity,
                           "error": {"code": error.code if isinstance(error, RpcError) else -32000, "message": str(error)[:1500]}})
        finally:
            if identity not in self.pending_queries:
                self.cancelled_requests.discard(identity)
            self.current_request_id = None

    def rpc_initialize(self, params: dict):
        if self.store is not None:
            raise RpcError(-32600, "Worker is already initialized")
        database = params.get("database")
        if not isinstance(database, str) or not Path(database).is_absolute():
            raise RpcError(-32602, "database must be an absolute path")
        roots = params.get("roots", [])
        if not isinstance(roots, list):
            raise RpcError(-32602, "roots must be an array")
        identities = set()
        for root in roots:
            if not isinstance(root, dict) or not isinstance(root.get("id"), str) or not isinstance(root.get("path"), str):
                raise RpcError(-32602, "Each root needs string id and path")
            if root["id"] in identities or not Path(root["path"]).is_absolute():
                raise RpcError(-32602, "Root ids must be unique and paths absolute")
            identities.add(root["id"])
            root["path"] = str(Path(root["path"]).resolve())
        embedding = params.get("embedding") or {}
        profile = Profile(**{key: value for key, value in embedding.items() if key in Profile.__dataclass_fields__})
        self.store = Store(database, profile)
        self.encoder = self.encoder_factory(profile)
        self.roots = roots
        self.config = params
        # Recover interrupted work by reconciling filesystem state; no inference during initialize.
        self.rpc_reconcile({})
        return {"protocol": PROTOCOL_VERSION, "schema": 2, "readiness": {"lexical": True, "semantic": False},
                "profile": asdict(profile), "capabilities": {"formats": ["org", "text", "markdown", "pdf-text", "xlsx", "docx", "odt", "html", "epub", "latex-source", "bibtex", "org-archive", "ocr-text", "filename-only"],
                "tasks": ["search", "related"], "related_policy": "source-as-search-query", "offline": True}}

    def rpc_status(self, params: dict):
        return {"readiness": {"lexical": True, "semantic": bool(self.encoder.ready and not self.model_error), "model_loaded": bool(self.encoder.ready)},
                "coverage": self.store.coverage(), "generation": self.store.generation,
                "backlog": self.tasks.qsize() + len(self.model_queue) + int(self.model_busy) + len(self.extraction_queue) + int(self.extraction_busy), "jobs": [{key: value for key, value in job.items() if key != "paths"} for job in list(self.jobs.values())[-20:]],
                "errors": self.store.errors(), "degraded": self.model_error,
                "profile": asdict(self.store.profile), "unavailable_roots": self.unavailable_roots,
                "ocr": {**self.ocr_status, "configured": self.config.get("ocr", {})}}

    def rpc_reconcile(self, params: dict):
        requested = params.get("root_ids")
        if requested is not None and (not isinstance(requested, list) or any(not isinstance(x, str) for x in requested)):
            raise RpcError(-32602, "root_ids must be an array of strings")
        paths = params.get("dirty_paths", params.get("paths"))
        if paths is not None and (not isinstance(paths, list) or any(not isinstance(x, str) for x in paths)):
            raise RpcError(-32602, "dirty_paths must be an array of strings")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "state": "queued", "kind": "reconcile", "done": 0, "total": 0}
        self.jobs[job_id] = job
        self.store.job(job_id, "queued", "reconcile", job)
        self.enqueue(5, self.scan, job_id, requested, paths)
        return {"job_id": job_id, "state": "queued"}

    def progress(self, job: dict, stage: str):
        self.store.job(job["id"], job["state"], job["kind"], {key: value for key, value in job.items() if key != "paths"})
        self.event("progress", {"job_id": job["id"], "state": job["state"], "stage": stage,
                                "done": job["done"], "total": job["total"], "coverage": self.store.coverage()})

    def _root_for_path(self, path: str):
        # Most-specific root wins when roots overlap.
        return next((root for root in sorted(self.roots, key=lambda r: len(r["path"]), reverse=True)
                     if Path(path).is_relative_to(root["path"])), None)

    @staticmethod
    def stamp(path: str) -> str:
        stat = Path(path).stat()
        return f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}"

    def scan(self, job_id: str, requested, dirty_paths):
        job = self.jobs[job_id]
        if job_id in self.cancelled_jobs:
            job["state"] = "cancelled"
            self.progress(job, "cancelled")
            return
        job["state"] = "running"
        known = self.store.files()
        excluded = self.store.excluded_files()
        selected_roots = [root for root in self.roots if requested is None or root["id"] in requested]
        if requested is None and dirty_paths is None:
            configured_ids = {root["id"] for root in self.roots}
            for path, info in known.items():
                if info["root_id"] not in configured_ids:
                    self.store.remove(path)
            for path, info in excluded.items():
                if info["root_id"] not in configured_ids:
                    self.store.forget_exclusion(path)
        available = {root["id"] for root in selected_roots if Path(root["path"]).is_dir()}
        self.unavailable_roots = [root["id"] for root in selected_roots if root["id"] not in available]
        paths = set()
        if dirty_paths is not None:
            for candidate in dirty_paths:
                self.dirty_pending.discard(candidate)
                if Path(candidate).is_symlink():
                    self.store.remove(str(Path(candidate).absolute()))
                    continue
                path = str(Path(candidate).resolve())
                root = self._root_for_path(path)
                if root and root["id"] in available and not Path(candidate).is_symlink():
                    if Path(path).is_file():
                        paths.add(path)
                    else:
                        self.store.remove(path)
                        self.store.forget_exclusion(path)
        else:
            for root in selected_roots:
                if root["id"] not in available:
                    continue
                for directory, names, files in os.walk(root["path"], followlinks=False):
                    names[:] = [name for name in names if not name.startswith(".") and not Path(directory, name).is_symlink()]
                    for name in files:
                        file = Path(directory, name)
                        if name.startswith(".") or file.is_symlink() or not file.is_file():
                            continue
                        paths.add(str(file.resolve()))
            for path, info in known.items():
                if info["root_id"] in available and path not in paths:
                    self.store.remove(path)
            for path, info in excluded.items():
                if info["root_id"] in available and path not in paths:
                    self.store.forget_exclusion(path)
        changed = []
        for path in sorted(paths):
            try:
                stamp = self.stamp(path)
                root = self._root_for_path(path)
                if reason := exclusion_reason(path):
                    self.store.exclude(path, root["id"], stamp, reason)
                    continue
                if path in excluded:
                    self.store.forget_exclusion(path)
                if (path not in known or known[path]["stamp"] != stamp or known[path]["coverage"] == "error"
                        or known[path].get("extraction_policy") != extraction_policy(path)):
                    root = self._root_for_path(path)
                    changed.append((path, root["id"], stamp))
            except OSError:
                self.store.remove(path)
        changed = [part for part in changed if self.inflight_sources.get(part[0]) != part[2]]
        for path, _, stamp in changed:
            self.inflight_sources[path] = stamp
        job["paths"] = {path: stamp for path, _, stamp in changed}
        job["total"] = len(changed)
        self.progress(job, "extracting")
        orgs = [part for part in changed if Path(part[0]).suffix.lower() in ORG_EXTENSIONS]
        other = [part for part in changed if Path(part[0]).suffix.lower() not in ORG_EXTENSIONS]
        job["pending"] = (len(orgs) + 23) // 24 + len(other)
        for offset in range(0, len(orgs), 24):
            self.enqueue(5, self.ingest_org, job_id, orgs[offset:offset+24])
        for part in other:
            self.enqueue(5, self.ingest_other, job_id, part)
        self.enqueue(6, self.finish_scan, job_id)
        self.last_reconcile = time.monotonic()

    def _commit_document(self, part, source):
        path, root_id, stamp = part
        if not Path(path).exists():
            self.store.remove(path)
            return
        # An edit during extraction is retried; never commit content to the wrong revision.
        if self.stamp(path) != stamp:
            self.enqueue(5, self.rpc_reconcile, {"dirty_paths": [path]})
            return
        revision = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        source["path"] = path
        if reason := exclusion_reason(path):
            self.store.exclude(path, root_id, stamp, reason)
            return
        details = source.get("details", {})
        if "ocr" in details:
            self.ocr_status = {key: value for key, value in details.items() if key.startswith("ocr") or key in ("missing_ocr_languages", "visual_embeddings")}
        self.store.upsert(source, root_id, stamp, revision)
        self.schedule_embeddings()

    def submit_extraction(self, job_id, function, success, failure):
        self.extraction_queue.append((job_id, function, success, failure))
        self.pump_extraction()

    def pump_extraction(self):
        if self.extraction_busy or self.stopping:
            return
        while self.extraction_queue:
            job_id, function, success, failure = self.extraction_queue.popleft()
            if job_id in self.cancelled_jobs:
                self.extraction_done(job_id)
                continue
            self.extraction_busy = True
            future = self.extraction_executor.submit(function)
            future.add_done_callback(lambda done: self.enqueue(1, self.complete_extraction, job_id, done, success, failure))
            return

    def complete_extraction(self, job_id, future, success, failure):
        self.extraction_busy = False
        if job_id not in self.cancelled_jobs:
            try:
                success(future.result())
            except Exception as error:
                failure(error)
        self.extraction_done(job_id)
        self.pump_extraction()

    def extraction_done(self, job_id):
        job = self.jobs[job_id]
        job["pending"] = max(0, job.get("pending", 0) - 1)
        self.progress(job, "extracting")
        if not job["pending"]:
            self.finish_scan(job_id)

    def release_sources(self, parts):
        for path, _, stamp in parts:
            if self.inflight_sources.get(path) == stamp:
                self.inflight_sources.pop(path, None)

    def ingest_org(self, job_id, parts):
        job = self.jobs[job_id]
        by_path = {part[0]: part for part in parts}
        def success(result):
            sources, errors = result
            for source in sources:
                part = by_path.get(str(Path(source["path"]).resolve()))
                if part:
                    try:
                        self._commit_document(part, source)
                    except Exception as error:
                        self.store.error(*part, str(error))
            for error in errors:
                part = by_path.get(str(Path(error["path"]).resolve()))
                if part:
                    self.store.error(*part, error["message"])
            job["done"] += len(parts)
            self.release_sources(parts)
        def failure(error):
            for part in parts:
                self.store.error(*part, f"Org extraction failed: {error}")
            job["done"] += len(parts)
            self.release_sources(parts)
        self.submit_extraction(job_id, lambda: org_batch(list(by_path), self.config["emacs"], self.config["extractor"]), success, failure)

    def ingest_other(self, job_id, part):
        job = self.jobs[job_id]
        def success(source):
            self._commit_document(part, source)
            job["done"] += 1
            self.release_sources([part])
        def failure(error):
            self.store.error(*part, str(error))
            job["done"] += 1
            self.release_sources([part])
        self.submit_extraction(job_id, lambda: document(part[0], self.config.get("ocr")), success, failure)

    def finish_scan(self, job_id):
        job = self.jobs[job_id]
        if job.get("pending", 0):
            return
        job["state"] = "cancelled" if job_id in self.cancelled_jobs else "complete"
        if job["state"] == "cancelled":
            for path, stamp in job.get("paths", {}).items():
                if self.inflight_sources.get(path) == stamp:
                    self.inflight_sources.pop(path, None)
        self.progress(job, job["state"])
        if job["state"] == "complete":
            self.schedule_embeddings()

    def schedule_embeddings(self):
        if not self.embedding_scheduled and not self.model_error and self.store.missing_embeddings(1):
            self.embedding_scheduled = True
            self.enqueue(10, self.embed_batch)

    def submit_inference(self, priority, function, success, failure):
        heapq.heappush(self.model_queue, (priority, next(self.sequence), function, success, failure))
        self.pump_inference()

    def pump_inference(self):
        if self.model_busy or not self.model_queue or self.stopping:
            return
        _, _, function, success, failure = heapq.heappop(self.model_queue)
        self.model_busy = True
        future = self.model_executor.submit(function)
        future.add_done_callback(lambda done: self.enqueue(1, self.complete_inference, done, success, failure))

    def complete_inference(self, future, success, failure):
        self.model_busy = False
        try:
            success(future.result())
        except Exception as error:
            failure(error)
        self.pump_inference()

    def embed_batch(self):
        rows = self.store.missing_embeddings(2)
        if not rows:
            self.embedding_scheduled = False
            return
        self.event("status", {"stage": "semantic-warming", "coverage": self.store.coverage()})
        def success(values):
            self.embedding_scheduled = False
            self.store.embed(rows, values)
            self.event("status", {"stage": "embedding", "coverage": self.store.coverage(), "readiness": {"semantic": True}})
            self.schedule_embeddings()
        def failure(error):
            self.embedding_scheduled = False
            self.model_error = f"Semantic retrieval unavailable: {error}. Run retrieval setup/doctor; lexical search remains available."
            self.event("status", {"stage": "degraded", "degraded": self.model_error, "coverage": self.store.coverage()})
        self.submit_inference(10, lambda: self.encoder.documents([row["input"] for row in rows]), success, failure)

    def rpc_search(self, params: dict):
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RpcError(-32602, "Search text must be nonempty")
        if len(text.encode()) > 512_000:
            raise RpcError(-32602, "Search snapshot exceeds 512 KB; select a node or region")
        task, mode = params.get("task", "search"), params.get("mode", "hybrid")
        if task not in ("search", "related") or mode not in ("hybrid", "lexical", "semantic"):
            raise RpcError(-32602, "Unknown search task or mode")
        limit = params.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise RpcError(-32602, "limit must be between 1 and 100")
        start = time.perf_counter()
        root_ids = params.get("root_ids")
        if root_ids is not None and (not isinstance(root_ids, list) or any(not isinstance(root, str) for root in root_ids)):
            raise RpcError(-32602, "root_ids must be an array of root identifiers")
        candidate_count = max(200, limit * 20)
        lexical = self.store.lexical_candidates(text, candidate_count, root_ids) if mode != "semantic" else []
        semantic = []
        degraded = self.model_error
        semantic_pending = False
        query_id = self.current_request_id
        if mode != "lexical" and self.store.coverage()["embedded_chunks"]:
            vector = self.query_vectors.get(text)
            if vector is not None:
                semantic = self.store.vector_candidates(vector, candidate_count, root_ids)
            elif not self.model_error:
                query_id = query_id if query_id is not None else uuid.uuid4().hex
                if query_id not in self.pending_queries:
                    snapshot = dict(params)
                    self.pending_queries[query_id] = snapshot
                    def success(vector):
                        self.query_vectors[text] = vector
                        if len(self.query_vectors) > 64:
                            self.query_vectors.pop(next(iter(self.query_vectors)))
                        self.pending_queries.pop(query_id, None)
                        if query_id not in self.cancelled_requests:
                            response = self.rpc_search(snapshot)
                            self.event("search-update", {"request_id": query_id, "result": response})
                        self.cancelled_requests.discard(query_id)
                    def failure(error):
                        self.pending_queries.pop(query_id, None)
                        if query_id not in self.cancelled_requests:
                            response = self.rpc_search({**snapshot, "mode": "lexical"})
                            response["degraded"] = f"Semantic query unavailable: {error}"
                            self.event("search-update", {"request_id": query_id, "result": response})
                        self.cancelled_requests.discard(query_id)
                    self.submit_inference(0, lambda: self.encoder.query(text), success, failure)
                semantic_pending = True
                degraded = "Semantic query warming; lexical results are ready"
        elif mode != "lexical":
            degraded = degraded or "Semantic index warming; showing lexical results"
        ranks = {}
        scores = {}
        for kind, candidates in (("lexical", lexical), ("semantic", semantic)):
            for rank, (identity, _score) in enumerate(candidates, start=1):
                ranks.setdefault(identity, {})[kind] = rank
                scores[identity] = scores.get(identity, 0) + 1 / (60 + rank)
        root_ids = params.get("root_ids")
        source = params.get("source") or {}
        source_path = str(Path(source["path"]).resolve()) if source.get("path") else None
        source_node = source.get("node_id")
        scope = params.get("scope", "query")
        grouped, results = set(), []
        for identity in sorted(scores, key=lambda key: (-scores[key], key)):
            result = self.store.result(identity)
            if result is None or (root_ids and result["root_id"] not in root_ids):
                continue
            if source_path and result["path"] == source_path:
                if scope in ("file", "region") or (scope == "node" and result["locator"].get("id") == source_node):
                    continue
            group = result["locator"].get("id") or result["source_id"]
            if group in grouped:
                continue
            grouped.add(group)
            indexed_stamp = result.pop("indexed_stamp", None)
            root_unavailable = result["root_id"] in self.unavailable_roots
            result["stale"] = (root_unavailable or
                               result.get("coverage_details", {}).get("extraction_policy") != extraction_policy(result["path"]))
            if not root_unavailable:
                try:
                    current_stamp = self.stamp(result["path"])
                except OSError:
                    self.store.remove(result["path"])
                    continue
                if current_stamp != indexed_stamp:
                    result["stale"] = True
                    if result["path"] not in self.dirty_pending:
                        self.dirty_pending.add(result["path"])
                        self.enqueue(5, self.rpc_reconcile, {"dirty_paths": [result["path"]]})
            result["ranks"] = ranks[identity]
            results.append(result)
            if len(results) == limit:
                break
        return {"results": results, "generation": self.store.generation, "coverage": self.store.coverage(),
                "degraded": degraded, "semantic_pending": semantic_pending, "request_id": query_id,
                "indexing": any(job["state"] in ("queued", "running") for job in self.jobs.values()),
                "task_policy": "source-as-search-query" if task == "related" else "search-query",
                "timings": {"search_ms": (time.perf_counter() - start) * 1000}}

    def rpc_repair(self, params: dict):
        if params.get("action", "rebuild") != "rebuild":
            raise RpcError(-32602, "Supported repair action is rebuild")
        plan = {"action": "rebuild", "preserves": ["source files", "Org-roam graph", "embedding cache"],
                "removes": ["derived source/chunk index"]}
        if params.get("dry_run", True):
            return {"plan": plan}
        # Existing extraction jobs are cancelled; cache reuse makes rebuilding idempotent.
        self.cancelled_jobs.update(self.jobs)
        for path in list(self.store.files()):
            self.store.remove(path)
        self.model_error = None
        return {"plan": plan, **self.rpc_reconcile({})}

    def run(self):
        try:
            while not self.stopping:
                try:
                    _, _, function, arguments = self.tasks.get(timeout=1)
                except queue.Empty:
                    if (self.store is not None and time.monotonic() - self.last_reconcile > 60
                            and not any(job["state"] in ("queued", "running") for job in self.jobs.values())):
                        self.rpc_reconcile({})
                    continue
                try:
                    function(*arguments)
                except Exception as error:
                    logging.exception("Retrieval task failed: %s", type(error).__name__)
                    self.event("status", {"stage": "error", "error": str(error)[:1000]})
        finally:
            self.extraction_executor.shutdown(wait=False, cancel_futures=True)
            self.model_executor.shutdown(wait=False, cancel_futures=True)
            if self.store is not None:
                self.store.close()


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")
    output = sys.stdout
    output_lock = threading.Lock()
    def send(message):
        encoded = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with output_lock:
            output.write(encoded + "\n")
            output.flush()
    service = Service(send)
    thread = threading.Thread(target=service.run, name="org-roam-retrieval-state", daemon=True)
    thread.start()
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message exceeds 4 MiB limit"}})
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid UTF-8 JSON"}})
                continue
            service.accept(message)
            if service.stopping:
                break
    finally:
        service.stopping = True
        service.enqueue(-100, lambda: None)
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
