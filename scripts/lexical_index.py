"""BM25 lexical search over the vault's markdown text - a lightweight relevance
layer alongside grep_search/glob_search, for thematic or paraphrased questions
where the model doesn't know the exact wording to search for. No embeddings, no
vector store, no persisted index: rebuilt in-memory per chat session so it can
never drift stale against vault edits made outside this pipeline.
"""
import os
import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"\w+")
_HEADING_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_CHUNK_WORDS = 500
_CHUNK_OVERLAP_WORDS = 50


@dataclass
class Chunk:
    path: str       # vault-relative
    heading: str    # nearest ## heading, or ""
    text: str


def _tokenize(text):
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _chunk_note(path, content):
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
            chunks.append(Chunk(path=path, heading="", text=" ".join(window)))
            if start + _CHUNK_WORDS >= len(words):
                break
        return chunks

    chunks = []
    bounds = [h.start() for h in headings] + [len(content)]
    if headings[0].start() > 0:
        preamble = content[:headings[0].start()].strip()
        if preamble:
            chunks.append(Chunk(path=path, heading="", text=preamble))
    for i, h in enumerate(headings):
        body = content[h.start():bounds[i + 1]].strip()
        if body:
            chunks.append(Chunk(path=path, heading=h.group(1).strip(), text=body))
    return chunks


class LexicalIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self._bm25 = BM25Okapi([_tokenize(c.text) for c in chunks]) if chunks else None

    def search(self, query, top_k=8):
        """Returns a plain string (grep_search-style), never raises."""
        try:
            if not self._bm25:
                return "No relevant chunks found (vault has no indexable text)."
            scores = self._bm25.get_scores(_tokenize(query))
            ranked = sorted(zip(scores, self.chunks), key=lambda x: -x[0])[:max(1, top_k)]
            if not ranked or ranked[0][0] <= 0:
                return "No relevant chunks found."
            out = []
            for score, c in ranked:
                snippet = c.text[:400]
                label = c.path + (f" ({c.heading})" if c.heading else "")
                out.append(f"{label}  [score {score:.2f}]\n{snippet}\n---")
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
            except OSError:
                continue
            chunks.extend(_chunk_note(rel_path, content))
    return LexicalIndex(chunks)
