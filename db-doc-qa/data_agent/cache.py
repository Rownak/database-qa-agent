"""Building blocks shared by the opt-in caches (Sprint 9).

The caches only skip repeated work and must never decide whether a question is answered, so
everything here is process memory, thread-safe and free of ADK and network code (numpy only).
It is used by the response cache in :mod:`data_agent.agent` and is meant to be reused by the
prefix and retrieval caches. It holds:

* :class:`TTLStore`, a bounded store with a time-to-live and least-recently-used eviction, and
  a process-wide registry of named stores with :func:`drop` (end of a session);
* fingerprints of the data file, the document file and the configuration, which go into every
  key so a changed input can never reach an old entry;
* :func:`normalize`, :func:`entity_tokens` and :func:`cosine_best` for matching questions.

Keys are tuples whose first element is the *scope id* (the session id, or ``""`` for entries
shared by every session of the process).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import numpy as np

# Bumped when a cached value changes shape or the code changes what an answer depends on.
CACHE_SCHEMA_VERSION = 1

_MISSING = object()


class TTLStore:
    """A bounded in-memory store with a time-to-live and LRU eviction.

    It is the storage of one cache. Reads and writes take a lock, so concurrent sessions of
    one process can share it. Expired entries are removed when they are met, not by a timer.

    Attributes:
        max_entries: Entries kept; the least recently used one is dropped beyond this.
        ttl_s: Default time-to-live in seconds.
    """

    def __init__(self, max_entries: int = 1000, ttl_s: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        """Creates an empty store.

        Args:
            max_entries: Maximum number of entries (at least 1).
            ttl_s: Default time-to-live of an entry, in seconds.
            clock: Monotonic clock in seconds; tests pass a fake one.
        """
        self.max_entries = max(1, int(max_entries))
        self.ttl_s = ttl_s
        self._clock = clock
        self._data: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()  # key -> (expires_at, value)
        self._lock = threading.Lock()

    def now(self) -> float:
        """Returns the store's clock, so callers can age entries on the same time base."""
        return self._clock()

    def get(self, key: tuple, default: Any = None) -> Any:
        """Returns the live value for ``key`` and marks it recently used, else ``default``."""
        with self._lock:
            item = self._data.get(key, _MISSING)
            if item is _MISSING:
                return default
            if item[0] <= self._clock():
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return item[1]

    def put(self, key: tuple, value: Any, ttl_s: float | None = None) -> None:
        """Stores ``value`` under ``key``, replacing an older one and evicting the oldest if full.

        Args:
            key: A tuple; its first element is the scope id (see :meth:`drop_scope`).
            value: Any object. It should be treated as immutable by readers.
            ttl_s: Time-to-live for this entry; the store default when ``None``.
        """
        with self._lock:
            self._data[key] = (self._clock() + (self.ttl_s if ttl_s is None else ttl_s), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def scan(self, prefix: tuple) -> list[tuple[tuple, Any]]:
        """Returns the live ``(key, value)`` pairs whose key starts with ``prefix``.

        Reading does not change the LRU order; use :meth:`get` on the chosen key for that.
        """
        n = len(prefix)
        now = self._clock()
        with self._lock:
            return [(k, v) for k, (exp, v) in self._data.items() if k[:n] == prefix and exp > now]

    def drop_scope(self, scope_id: str) -> int:
        """Removes every entry of one scope and returns how many were removed."""
        with self._lock:
            doomed = [k for k in self._data if k and k[0] == scope_id]
            for k in doomed:
                del self._data[k]
            return len(doomed)

    def clear(self) -> None:
        """Removes every entry."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


_REGISTRY: dict[str, TTLStore] = {}
_REGISTRY_LOCK = threading.Lock()


def get_store(name: str, *, max_entries: int, ttl_s: float) -> TTLStore:
    """Returns the process-wide store called ``name``, creating it on first use.

    Agents are rebuilt per session in the evaluation, so a cache that shares entries between them
    must live here and not on the agent. A later call with other limits updates them in place.

    Args:
        name: Cache name, for example ``"response"``.
        max_entries: Capacity.
        ttl_s: Default time-to-live in seconds.
    """
    with _REGISTRY_LOCK:
        store = _REGISTRY.get(name)
        if store is None:
            store = _REGISTRY[name] = TTLStore(max_entries, ttl_s)
        else:
            store.max_entries, store.ttl_s = max(1, int(max_entries)), ttl_s
        return store


def drop(scope_id: str) -> int:
    """Removes the entries of one session from every registered store.

    Called when a session closes or is swept. An empty id (the shared scope) is ignored, so
    ending one session never clears entries shared by the others.

    Returns:
        The number of entries removed.
    """
    if not scope_id:
        return 0
    with _REGISTRY_LOCK:
        stores = list(_REGISTRY.values())
    return sum(s.drop_scope(scope_id) for s in stores)


def reset() -> None:
    """Empties the registry and the fingerprint memo (tests, and a manual full clear)."""
    with _REGISTRY_LOCK:
        for store in _REGISTRY.values():
            store.clear()
        _REGISTRY.clear()
    with _FILE_FP_LOCK:
        _FILE_FP.clear()


# --- fingerprints ---

_FILE_FP: dict[tuple[str, int, int], str] = {}
_FILE_FP_LOCK = threading.Lock()


def file_fp(path: str | Path | None) -> str | None:
    """Returns the sha256 of a file's bytes, or ``None`` if it cannot be read.

    The result is memoised by path, size and modification time, so a file is hashed once until it
    changes; a rebuilt ``data.db`` or ``docs.db`` therefore gets a new fingerprint.
    """
    if not path:
        return None
    try:
        p = Path(path)
        st = p.stat()
        memo_key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
        with _FILE_FP_LOCK:
            known = _FILE_FP.get(memo_key)
        if known is not None:
            return known
        digest = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
        with _FILE_FP_LOCK:
            _FILE_FP[memo_key] = digest.hexdigest()
        return digest.hexdigest()
    except OSError:
        return None


def cfg_fp(config: dict, *texts: str | None) -> str:
    """Fingerprints the settings and prompts that decide what an answer looks like.

    Args:
        config: The JSON form of ``AgentConfig``. ``cache`` and ``latency_trace`` are left out: they
            change how fast an answer arrives, not what it is.
        *texts: Resolved text that also shapes the answer, in a fixed order: the instruction, the
            intent prompt and the tool declarations (file contents, not file names). ``None`` is
            kept distinct from the empty string.

    Returns:
        A sha256 hex digest that also covers :data:`CACHE_SCHEMA_VERSION`.
    """
    body = {k: v for k, v in config.items() if k not in ("cache", "latency_trace")}
    payload = {"schema": CACHE_SCHEMA_VERSION, "config": body, "texts": [None if t is None else t for t in texts]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


# --- matching ---

_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalises a question for exact matching: NFKC, case-folded, whitespace collapsed."""
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def text_hash(text: str) -> str:
    """Returns the sha256 of the normalised text; keys hold this, never the text itself."""
    return hashlib.sha256(normalize(text).encode()).hexdigest()


_WORD_RE = re.compile(r"[^\W_][\w'’-]*")
_NUMBER_RE = re.compile(r"[$€£]?\d[\d,]*(?:\.\d+)?%?")
_ID_RE = re.compile(r"[^\W\d_][\w-]*\d[\w-]*")  # a letter-led word with a digit: c0042, POL-12
_FILE_RE = re.compile(r"[\w-]+\.(?:csv|xlsx?|md|txt|pdf|docx?|json|db|yaml|yml)\b", re.IGNORECASE)
_QUOTED_RE = re.compile(r"\"([^\"]+)\"|“([^”]+)”|(?<!\w)'([^']+)'(?!\w)")


def entity_tokens(text: str) -> frozenset[str]:
    """Extracts the identifier-like tokens of a question, for the entity guard.

    Embeddings barely tell "client 252" from "client 253", so two questions may be served the same
    answer only if these tokens are equal: numbers (with their currency or percent sign, so
    ``$10,000`` and ``10000`` differ on purpose), words that contain a digit, quoted strings,
    file names, and capitalised words that do not start a sentence (names).

    Args:
        text: The raw question (case matters here).

    Returns:
        The case-folded tokens.
    """
    text = unicodedata.normalize("NFKC", text)
    found: set[str] = set()
    for m in _QUOTED_RE.finditer(text):
        found.add("q:" + normalize(next(g for g in m.groups() if g)))
    for m in _FILE_RE.finditer(text):
        found.add("f:" + m.group(0).casefold())
    for m in _ID_RE.finditer(text):
        found.add("i:" + m.group(0).casefold())
    for m in _NUMBER_RE.finditer(text):
        found.add("n:" + m.group(0).rstrip(",."))
    sentence_start = True
    last_end = 0
    for m in _WORD_RE.finditer(text):
        gap = text[last_end:m.start()]
        if any(c in gap for c in ".?!:;\n"):
            sentence_start = True
        word = m.group(0)
        if not sentence_start and word[0].isupper():
            found.add("c:" + word.casefold())
        sentence_start = False
        last_end = m.end()
    return frozenset(found)


def cosine_best(query: np.ndarray, candidates: Iterable[tuple[Any, np.ndarray]]) -> tuple[Any, float] | None:
    """Finds the candidate most similar to ``query`` by cosine similarity.

    Args:
        query: A 1-D vector.
        candidates: ``(label, vector)`` pairs; vectors of another length are skipped.

    Returns:
        ``(label, similarity)`` of the best candidate, or ``None`` if there is none.
    """
    q = np.asarray(query, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    best: tuple[Any, float] | None = None
    if qn == 0:
        return None
    for label, vec in candidates:
        v = np.asarray(vec, dtype=np.float32)
        if v.shape != q.shape:
            continue
        vn = float(np.linalg.norm(v))
        if vn == 0:
            continue
        sim = float(np.dot(q, v)) / (qn * vn)
        if best is None or sim > best[1]:
            best = (label, sim)
    return best
