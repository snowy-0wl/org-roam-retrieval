"""Versioned embedding policy and bounded, offline model ownership."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import contextlib
import hashlib
import json
import os
import re
import sys
from typing import Protocol

import numpy as np


DEFAULT_MODEL = "google/embeddinggemma-2"
DEFAULT_REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"


@dataclass(frozen=True)
class Profile:
    model: str = DEFAULT_MODEL
    revision: str = DEFAULT_REVISION
    dimension: int = 256
    device: str = "cpu"
    policy: str = "search-query-to-titled-document-v1"
    dtype: str = "float32"
    normalization: str = "unit-after-truncation-v1"

    def __post_init__(self):
        if not re.fullmatch(r"[a-fA-F0-9]{40}", self.revision):
            raise ValueError("Embedding model revision must be an immutable 40-character commit")
        if not 16 <= self.dimension <= 768:
            raise ValueError("Embedding dimension must be between 16 and 768")
        if self.dtype != "float32":
            raise ValueError("EmbeddingGemma 2 requires float32 on CPU/MPS; float16 is forbidden")
        if self.device not in ("cpu", "mps"):
            raise ValueError("Supported local devices are cpu and mps")
        if self.model != DEFAULT_MODEL:
            raise ValueError("This adapter supports google/embeddinggemma-2 only; another model requires its own task contract")

    @property
    def id(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class Encoder(Protocol):
    profile: Profile
    ready: bool
    def documents(self, texts: list[str]) -> np.ndarray: ...
    def query(self, text: str) -> np.ndarray: ...
    def count_tokens(self, text: str) -> int: ...


def validate_vectors(values, dimension: int) -> np.ndarray:
    """Reject incompatible/nonfinite vectors and normalize after truncation."""
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    if vectors.ndim != 2 or vectors.shape[1] != dimension:
        raise ValueError(f"Embedding dimension mismatch: expected {dimension}")
    if not np.isfinite(vectors).all():
        raise ValueError("Embedding returned non-finite values")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms <= 1e-10):
        raise ValueError("Embedding returned a zero vector")
    return np.ascontiguousarray(vectors / norms, dtype=np.float32)


class SentenceTransformerEncoder:
    """Load one pinned text encoder lazily. No network access at runtime."""
    def __init__(self, profile: Profile):
        self.profile = profile
        self.model = None
        self.ready = False
        self.query_cache: OrderedDict[str, np.ndarray] = OrderedDict()

    def _load(self):
        if self.model is not None:
            return self.model
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from sentence_transformers import SentenceTransformer
            torch.set_num_threads(min(4, os.cpu_count() or 1))
            if self.profile.device == "mps" and not torch.backends.mps.is_available():
                raise RuntimeError("MPS is unavailable; select cpu in retrieval settings")
            model = SentenceTransformer(
                self.profile.model,
                revision=self.profile.revision,
                local_files_only=True,
                device=self.profile.device,
                model_kwargs={"dtype": torch.float32},
                config_kwargs={"vision_config": None, "audio_config": None},
            )
            if not {"SearchQuery", "Document"}.issubset(model.prompts):
                raise RuntimeError("Pinned model did not load its required SearchQuery/Document task prompts")
            if model.max_seq_length < 512:
                raise RuntimeError("Unexpected model tokenizer limit")
        self.model = model
        self.ready = True
        return model

    def count_tokens(self, text: str) -> int:
        return len(self._load().tokenizer.encode(text, add_special_tokens=True))

    def _encode(self, texts: list[str], *, query: bool) -> np.ndarray:
        model = self._load()
        prefix = model.prompts["SearchQuery"] if query else ""
        if any(len(model.tokenizer.encode(prefix + text, add_special_tokens=True)) > model.max_seq_length for text in texts):
            raise ValueError("Embedding input exceeds tokenizer limit; chunking must not truncate content silently")
        with contextlib.redirect_stdout(sys.stderr):
            vectors = model.encode(
                texts,
                prompt_name="SearchQuery" if query else None,
                prompt=None if query else "",
                batch_size=min(4, len(texts)),
                truncate_dim=self.profile.dimension,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return validate_vectors(vectors, self.profile.dimension)

    def documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, query=False)

    def query(self, text: str) -> np.ndarray:
        cached = self.query_cache.get(text)
        if cached is not None:
            self.query_cache.move_to_end(text)
            return cached
        # Whole-buffer related queries may exceed one model window.  Encode
        # every bounded part, then normalize the centroid; never discard the tail.
        from .extract import chunks
        parts = list(chunks(text))
        encoded = self._encode(parts or [text], query=True)
        value = validate_vectors(encoded.mean(axis=0), self.profile.dimension)[0]
        self.query_cache[text] = value
        if len(self.query_cache) > 64:
            self.query_cache.popitem(last=False)
        return value
