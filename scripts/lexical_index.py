"""BM25 lexical search over the vault's markdown text - a lightweight relevance
layer alongside grep_search/glob_search, for thematic or paraphrased questions
where the model doesn't know the exact wording to search for. No embeddings, no
vector store, no persisted index: rebuilt in-memory per chat session so it can
never drift stale against vault edits made outside this pipeline.

Ranking is BM25 relevance multiplied by a freshness factor (search engines call
this recency/freshness boosting): each note's score decays exponentially with
its file age, halving every VAULT_QA_RECENCY_HALFLIFE_DAYS, but never below
VAULT_QA_RECENCY_FLOOR of the raw score. Relevance stays the primary signal -
an old note that matches much better still wins - while ties and near-ties
resolve toward newer notes.
"""
import os
import re
import time
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"\w+")
_HEADING_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CHUNK_WORDS = 500
_CHUNK_OVERLAP_WORDS = 50

RECENCY_HALFLIFE_DAYS = float(os.environ.get("VAULT_QA_RECENCY_HALFLIFE_DAYS", "90"))
RECENCY_FLOOR = float(os.environ.get("VAULT_QA_RECENCY_FLOOR", "0.5"))


@dataclass
class Chunk:
    path: str       # vault-relative
    heading: str    # nearest ## heading, or ""
    text: str
    mtime: float = 0.0   # note file modification time (unix); 0 = unknown


def _tokenize(text):
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _chunk_note(path, content, mtime=0.0):
    """Split by top-level '## ' headings; headingless notes get overlapping
    fixed-size word windows instead."""
    headings = list(_HEADING_RE.finditer(content))
    if not headings:
        words = content.split()
        if not words:
            return []
        chunks = []
        step = _CHUNK_WORDS - _CHUNK_OVERLAP_WORDS
        for start in range(0, len(words), step):
            window = words[start:start + _CHUNK_WORDS]
            if not window:
                break
            chunks.append(Chunk(path=path, heading="", text=" ".join(window), mtime=mtime))
            if start + _CHUNK_WORDS >= len(words):
                break
        return chunks

    chunks = []
    bounds = [h.start() for h in headings] + [len(content)]
    if headings[0].start() > 0:
        preamble = content[:headings[0].start()].strip()
        if preamble:
            chunks.append(Chunk(path=path, heading="", text=preamble, mtime=mtime))
    for i, h in enumerate(headings):
        body = content[h.start():bounds[i + 1]].strip()
        if body:
            chunks.append(Chunk(path=path, heading=h.group(1).strip(), text=body, mtime=mtime))
    return chunks


def _recency_factor(mtime, now):
    """Freshness multiplier in [RECENCY_FLOOR, 1]: 1.0 for a note touched now,
    halving toward the floor every RECENCY_HALFLIFE_DAYS. Unknown mtimes get
    the floor so they never outrank a known-fresh note on recency alone."""
    if mtime <= 0:
        return RECENCY_FLOOR
    age_days = max(0.0, (now - mtime) / 86400.0)
    decay = 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)
    return RECENCY_FLOOR + (1.0 - RECENCY_FLOOR) * decay


class LexicalIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self._bm25 = BM25Okapi([_tokenize(c.text) for c in chunks]) if chunks else None

    def search(self, query, top_k=8):
        """Returns a plain string (grep_search-style), never raises. Ranking is
        BM25 * recency factor (see module docstring)."""
        try:
            if not self._bm25:
                return "No relevant chunks found (vault has no indexable text)."
            raw = self._bm25.get_scores(_tokenize(query))
            now = time.time()
            weighted = [(s * _recency_factor(c.mtime, now), s, c)
                        for s, c in zip(raw, self.chunks)]
            ranked = sorted(weighted, key=lambda x: -x[0])[:max(1, top_k)]
            if not ranked or ranked[0][1] <= 0:
                return "No relevant chunks found."
            out = []
            for score, raw_score, c in ranked:
                snippet = c.text[:400]
                label = c.path + (f" ({c.heading})" if c.heading else "")
                age = f", {max(0, int((now - c.mtime) / 86400))}d old" if c.mtime > 0 else ""
                out.append(f"{label}  [score {score:.2f}{age}]\n{snippet}\n---")
            return "\n".join(out)
        except Exception as e:
            return f"Error in search_relevant: {e}"


def build_index(vault_dir):
    """Walk vault_dir (same exclusions as vault_qa.build_vault_index), chunk every
    note, and return a LexicalIndex."""
    chunks = []
    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and d not in ("Attachments", "smart-chats"))
        for f in sorted(files):
            if not f.endswith(".md"):
                continue
            abs_path = os.path.join(root, f)
            rel_path = os.path.relpath(abs_path, vault_dir)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as fobj:
                    content = fobj.read()
                mtime = os.path.getmtime(abs_path)
            except OSError:
                continue
            chunks.extend(_chunk_note(rel_path, content, mtime=mtime))
    return LexicalIndex(chunks)
