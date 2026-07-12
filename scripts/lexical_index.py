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
from datetime import datetime

from rank_bm25 import BM25Okapi

from tzutil import APP_TZ

_TOKEN_RE = re.compile(r"\w+")
_HEADING_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CHUNK_WORDS = 500
_CHUNK_OVERLAP_WORDS = 50
_SNIPPET_CHARS = 700

RECENCY_HALFLIFE_DAYS = float(os.environ.get("VAULT_QA_RECENCY_HALFLIFE_DAYS", "90"))
RECENCY_FLOOR = float(os.environ.get("VAULT_QA_RECENCY_FLOOR", "0.5"))

# Canonical schema for the search_relevant tool. Lives here (not in vault_qa) so
# the ingestion agent, the CLI, and the chat server all expose the identical tool;
# vault_qa re-exports it for backwards compatibility.
SEARCH_RELEVANT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_relevant",
        "description": (
            "Relevance search over chunked vault text - use for thematic/paraphrased "
            "questions where you don't know the exact wording. Use grep_search instead "
            "for exact identifiers (ticket numbers, exact names/dates). Results are "
            "recency-weighted: at similar relevance, recently modified notes rank first "
            "(each hit shows its age in days). Optionally narrow by folder (path_prefix) "
            "or by note modification date (date_from/date_to, YYYY-MM-DD)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "description": "Number of chunks to return (default 8)."},
                "path_prefix": {"type": "string", "description": "Only return hits whose path starts with this (e.g. 'Work/CDLP/')."},
                "date_from": {"type": "string", "description": "Only notes modified on/after this date (YYYY-MM-DD)."},
                "date_to": {"type": "string", "description": "Only notes modified on/before this date (YYYY-MM-DD)."},
            },
            "required": ["query"]
        }
    }
}


def _parse_date(s):
    """YYYY-MM-DD -> unix seconds (Singapore midnight), or None if unparseable.

    Uses the app timezone explicitly (not the C library's local time) so date
    filtering is correct even if the OS tzdata package is missing - mtimes it's
    compared against are epoch seconds, so the boundary must be a real instant.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=APP_TZ).timestamp()
    except (ValueError, OverflowError):
        return None


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

    def rank(self, query, path_prefix=None, date_from=None, date_to=None, depth=None):
        """Return [(chunk_index, weighted_score, raw_bm25_score), ...] best-first,
        after applying the path/date filters. Shared by search() and the hybrid
        fusion layer. `depth` truncates the list; None returns everything."""
        if not self._bm25:
            return []
        raw = self._bm25.get_scores(_tokenize(query))
        now = time.time()
        prefix = (path_prefix or "").replace("\\", "/").lstrip("/") or None
        from_ts = _parse_date(date_from)
        to_ts = _parse_date(date_to)
        if to_ts is not None:
            to_ts += 86400  # make date_to inclusive of the whole day
        weighted = []
        for i, (s, c) in enumerate(zip(raw, self.chunks)):
            if prefix and not c.path.replace("\\", "/").startswith(prefix):
                continue
            if from_ts is not None and (c.mtime <= 0 or c.mtime < from_ts):
                continue
            if to_ts is not None and (c.mtime <= 0 or c.mtime >= to_ts):
                continue
            weighted.append((i, s * _recency_factor(c.mtime, now), s))
        weighted.sort(key=lambda x: -x[1])
        if depth is not None:
            weighted = weighted[:depth]
        return weighted

    def search(self, query, top_k=8, path_prefix=None, date_from=None, date_to=None):
        """Returns a plain string (grep_search-style), never raises. Ranking is
        BM25 * recency factor (see module docstring). Optional filters narrow the
        candidate pool before ranking: path_prefix (folder), and date_from/date_to
        on the note's modification time (YYYY-MM-DD strings)."""
        try:
            if not self._bm25:
                return "No relevant chunks found (vault has no indexable text)."
            ranked = self.rank(query, path_prefix=path_prefix,
                               date_from=date_from, date_to=date_to)[:max(1, top_k)]
            if not ranked:
                note = ""
                if path_prefix or date_from or date_to:
                    note = " matching the given path/date filter"
                return f"No relevant chunks found{note}."
            if ranked[0][2] <= 0:
                return "No relevant chunks found."
            now = time.time()
            out = []
            for idx, score, raw_score in ranked:
                c = self.chunks[idx]
                snippet = c.text[:_SNIPPET_CHARS]
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
