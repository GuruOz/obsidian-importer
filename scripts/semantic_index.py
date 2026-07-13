#!/usr/bin/env python3
"""Optional semantic (embedding) search layer over the vault, fused with the BM25
lexical index for the chat assistant.

Off by default: with EMBED_BASE_URL / EMBED_MODEL unset, build_searcher() returns
the plain LexicalIndex and nothing here runs. When configured (recommended: a local
Ollama server, `ollama pull nomic-embed-text`), chunk vectors are embedded once and
persisted to SQLite (SEMANTIC_DB, default /data/semantic_index.db) so they survive
restarts and only changed notes are re-embedded. Search queries are answered by
Reciprocal-Rank-Fusion of the BM25 ranking and the cosine ranking, so paraphrased
questions with no keyword overlap can still surface the right note.

Single-writer by design: only the vault-qa web process syncs/writes this DB. The
nightly ingestion agent uses BM25 only (lexical_index) - it needs to find existing
topic notes, which are already-indexed prior-day notes, and keeping it write-free
avoids any cross-container DB contention.
"""
import hashlib
import os
import sqlite3
import struct
import sys
import threading
import time

import lexical_index

EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "").strip()
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "").strip()
SEMANTIC_DB = os.environ.get("SEMANTIC_DB", "/data/semantic_index.db")
_EMBED_BATCH = 64
_RRF_K = 60            # Reciprocal Rank Fusion constant (standard default)
_FUSE_DEPTH = 50       # how deep each ranked list feeds the fusion


def embeddings_enabled():
    return bool(EMBED_BASE_URL and EMBED_MODEL)


def _log(msg):
    print(f"[semantic] {msg}", file=sys.stderr, flush=True)


def _chunk_hash(chunk):
    """Stable identity for a chunk: its path + text. Persisted vectors are keyed
    on this, so unchanged chunks are never re-embedded even if a note's other
    chunks shift, and edited chunks naturally get a new key."""
    h = hashlib.sha1()
    h.update(chunk.path.encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update(chunk.text.encode("utf-8", "ignore"))
    return h.hexdigest()


def _make_embed_client():
    from openai import OpenAI
    return OpenAI(api_key=EMBED_API_KEY or "ollama", base_url=EMBED_BASE_URL)


def embed_texts(texts, client=None):
    """Embed a list of strings -> list of list[float]. Raises on endpoint failure
    (callers decide whether to degrade to lexical-only)."""
    if not texts:
        return []
    client = client or _make_embed_client()
    out = []
    for i in range(0, len(texts), _EMBED_BATCH):
        batch = texts[i:i + _EMBED_BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend([d.embedding for d in resp.data])
    return out


def _pack(vec):
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob):
    return struct.unpack(f"<{len(blob) // 4}f", blob)


class SemanticIndex:
    """SQLite-backed store of chunk embeddings, with cosine search. Thread-safe
    for the single writer (vault-qa) plus concurrent readers."""

    def __init__(self, db_path=SEMANTIC_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.ready = False
        # In-memory search matrix (populated by load()); numpy is a rank_bm25 dep.
        self._hashes = []
        self._matrix = None   # np.ndarray (n, dim), L2-normalized
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self):
        con = sqlite3.connect(self.db_path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self):
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS vecs "
                        "(hash TEXT PRIMARY KEY, path TEXT, mtime REAL, vec BLOB)")
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    def _get_meta(self, con, key):
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def needs_sync(self, chunks):
        """True if the persisted vectors don't already cover exactly this chunk
        set for the current embed model."""
        want = {_chunk_hash(c) for c in chunks}
        with self._connect() as con:
            if self._get_meta(con, "model") != EMBED_MODEL:
                return True
            have = {r[0] for r in con.execute("SELECT hash FROM vecs")}
        return want != have

    def sync(self, chunks, client=None):
        """Bring the DB in line with `chunks`: embed new ones, drop stale ones.
        Wipes and rebuilds if the embed model changed. Loads the search matrix at
        the end. Best-effort: on any embedding error it logs and leaves whatever
        was already there (ready stays as-is)."""
        with self._lock:
            try:
                client = client or _make_embed_client()
                by_hash = {}
                for c in chunks:
                    by_hash.setdefault(_chunk_hash(c), c)
                with self._connect() as con:
                    if self._get_meta(con, "model") != EMBED_MODEL:
                        con.execute("DELETE FROM vecs")
                        con.execute("INSERT OR REPLACE INTO meta VALUES ('model', ?)", (EMBED_MODEL,))
                        _log(f"embed model set to {EMBED_MODEL!r}; rebuilding index")
                    have = {r[0] for r in con.execute("SELECT hash FROM vecs")}
                    want = set(by_hash)
                    stale = have - want
                    missing = [h for h in by_hash if h not in have]
                    if stale:
                        con.executemany("DELETE FROM vecs WHERE hash=?", [(h,) for h in stale])
                    if missing:
                        _log(f"embedding {len(missing)} new/changed chunk(s)...")
                        for i in range(0, len(missing), _EMBED_BATCH):
                            batch_h = missing[i:i + _EMBED_BATCH]
                            vecs = embed_texts([by_hash[h].text for h in batch_h], client=client)
                            con.executemany(
                                "INSERT OR REPLACE INTO vecs VALUES (?,?,?,?)",
                                [(h, by_hash[h].path, by_hash[h].mtime, _pack(v))
                                 for h, v in zip(batch_h, vecs)])
                            con.commit()
                    con.commit()
                _log(f"sync complete ({len(want)} chunks indexed, {len(missing)} embedded)")
            except Exception as e:  # noqa: BLE001
                _log(f"sync failed ({e}); semantic search stays on whatever was cached")
                return
        self.load()

    def load(self):
        """Load all vectors into an L2-normalized matrix for cosine search."""
        try:
            import numpy as np
            with self._connect() as con:
                rows = con.execute("SELECT hash, vec FROM vecs").fetchall()
            if not rows:
                self._hashes, self._matrix, self.ready = [], None, False
                return
            hashes = [r[0] for r in rows]
            mat = np.array([_unpack(r[1]) for r in rows], dtype=np.float32)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._hashes = hashes
            self._matrix = mat / norms
            self.ready = True
        except Exception as e:  # noqa: BLE001
            _log(f"load failed ({e})")
            self._hashes, self._matrix, self.ready = [], None, False

    def rank(self, query, client=None, depth=_FUSE_DEPTH):
        """Return [(hash, cosine_score), ...] best-first for the query, or []
        if the index isn't ready or the query can't be embedded."""
        if not self.ready or self._matrix is None:
            return []
        try:
            import numpy as np
            qv = embed_texts([query], client=client)[0]
            q = np.array(qv, dtype=np.float32)
            n = float(np.linalg.norm(q))
            if n == 0:
                return []
            sims = self._matrix @ (q / n)
            k = min(depth, len(self._hashes))
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
            return [(self._hashes[i], float(sims[i])) for i in idx]
        except Exception as e:  # noqa: BLE001
            _log(f"query embedding failed ({e}); falling back to lexical only")
            return []


class HybridIndex:
    """Drop-in for LexicalIndex (exposes .chunks and .search) that fuses BM25 and
    semantic rankings via Reciprocal Rank Fusion. Falls back to pure BM25 whenever
    the semantic side isn't ready or the query can't be embedded."""

    def __init__(self, lex, sem):
        self.lex = lex
        self.sem = sem
        self.chunks = lex.chunks
        self._hash_to_idx = {_chunk_hash(c): i for i, c in enumerate(lex.chunks)}

    def _allowed(self, path_prefix, from_ts, to_ts):
        prefix = (path_prefix or "").replace("\\", "/").lstrip("/") or None
        allowed = None
        if prefix or from_ts is not None or to_ts is not None:
            allowed = set()
            for i, c in enumerate(self.chunks):
                if prefix and not c.path.replace("\\", "/").startswith(prefix):
                    continue
                if from_ts is not None and (c.mtime <= 0 or c.mtime < from_ts):
                    continue
                if to_ts is not None and (c.mtime <= 0 or c.mtime >= to_ts):
                    continue
                allowed.add(i)
        return allowed

    def search(self, query, top_k=8, path_prefix=None, date_from=None, date_to=None):
        try:
            lex_order = self.lex.rank(query, path_prefix=path_prefix,
                                      date_from=date_from, date_to=date_to,
                                      depth=_FUSE_DEPTH)  # [(idx, weighted, raw)]
            sem_hashes = self.sem.rank(query)
            if not sem_hashes:
                # Nothing to fuse - defer entirely to the lexical formatter.
                return self.lex.search(query, top_k=top_k, path_prefix=path_prefix,
                                       date_from=date_from, date_to=date_to)

            from_ts = lexical_index._parse_date(date_from)
            to_ts = lexical_index._parse_date(date_to)
            if to_ts is not None:
                to_ts += 86400
            allowed = self._allowed(path_prefix, from_ts, to_ts)

            found_by = {}   # chunk idx -> set of retrievers
            rrf = {}

            def add(idx, rank, tag):
                if allowed is not None and idx not in allowed:
                    return
                w = 1.0 / (_RRF_K + rank + 1)
                # Mirror the lexical index's raw-archive demotion on the fused
                # score, or the semantic leg would re-inflate raw dumps the
                # lexical ranking already pushed down.
                if lexical_index.is_raw_archive(self.chunks[idx].path):
                    w *= lexical_index.RAW_ARCHIVE_DEMOTE
                rrf[idx] = rrf.get(idx, 0.0) + w
                found_by.setdefault(idx, set()).add(tag)

            for rank, (idx, _w, _r) in enumerate(lex_order):
                add(idx, rank, "lexical")
            for rank, (h, _score) in enumerate(sem_hashes):
                idx = self._hash_to_idx.get(h)
                if idx is not None:
                    add(idx, rank, "semantic")

            if not rrf:
                return "No relevant chunks found."
            ranked = sorted(rrf, key=lambda i: -rrf[i])[:max(1, top_k)]
            now = time.time()
            out = []
            for idx in ranked:
                c = self.chunks[idx]
                snippet = c.text[:lexical_index._SNIPPET_CHARS]
                label = c.path + (f" ({c.heading})" if c.heading else "")
                age = f", {max(0, int((now - c.mtime) / 86400))}d old" if c.mtime > 0 else ""
                via = "+".join(sorted(found_by[idx]))
                out.append(f"{label}  [{via}{age}]\n{snippet}\n---")
            return "\n".join(out)
        except Exception as e:  # noqa: BLE001
            # Never let the hybrid layer break search; fall back to lexical.
            try:
                return self.lex.search(query, top_k=top_k, path_prefix=path_prefix,
                                       date_from=date_from, date_to=date_to)
            except Exception:
                return f"Error in search_relevant: {e}"


def build_searcher(vault_dir, background_sync=True):
    """Build the search index for the chat: a LexicalIndex, wrapped in a
    HybridIndex when embeddings are configured. If the persisted vectors are
    stale, sync runs in a background thread (search serves BM25-only until it
    finishes) unless background_sync is False (CLI: sync inline or skip)."""
    lex = lexical_index.build_index(vault_dir)
    if not embeddings_enabled():
        return lex
    try:
        sem = SemanticIndex()
        if sem.needs_sync(lex.chunks):
            if background_sync:
                threading.Thread(target=sem.sync, args=(lex.chunks,), daemon=True).start()
            else:
                sem.sync(lex.chunks)
        else:
            sem.load()
        return HybridIndex(lex, sem)
    except Exception as e:  # noqa: BLE001
        _log(f"semantic layer unavailable, using lexical only: {e}")
        return lex


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Manage the semantic embedding index.")
    ap.add_argument("--sync", action="store_true", help="Embed/refresh the vault (blocking).")
    ap.add_argument("--status", action="store_true", help="Show index status.")
    ap.add_argument("--vault-dir", default=os.environ.get("VAULT_DIR", "/vault"))
    args = ap.parse_args()

    if not embeddings_enabled():
        print("Embeddings are not configured (set EMBED_BASE_URL and EMBED_MODEL).")
        return
    sem = SemanticIndex()
    if args.sync:
        lex = lexical_index.build_index(args.vault_dir)
        print(f"Syncing {len(lex.chunks)} chunks against {SEMANTIC_DB} "
              f"(model {EMBED_MODEL})...")
        sem.sync(lex.chunks)
    sem.load()
    with sem._connect() as con:
        n = con.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    print(f"Semantic index: {n} chunk vector(s), ready={sem.ready}, db={SEMANTIC_DB}")


if __name__ == "__main__":
    _cli()
