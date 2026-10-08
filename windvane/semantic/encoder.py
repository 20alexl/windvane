"""
The encoder the daemon keeps resident: model load, decision scoring and the
pinned model thread.

numpy and sentence-transformers are imported inside the functions that need
them, so importing this module costs nothing on a machine without the
semantic extra; ``windvane.semantic.enabled()`` decides whether the daemon
ever calls in here.
"""

import json
import re
import sys
import threading
from pathlib import Path

# The encoder discards anything past its max_seq_length (512 tokens for the
# default bge-base, ~2000 chars), so text longer than this is tokenized and
# padded at full cost and then thrown away. Capping here is behaviour-
# preserving and it bounds the allocator's high-water mark: the resident
# daemon never returns arena memory to the OS, so one oversized encode
# (a pasted log, a long single-line prompt) permanently raised its RSS.
# Applied server-side so every caller is covered, not just the batch path.
MAX_ENCODE_CHARS = 2000


def _load_model_and_templates():
    """Load the configured embedding model and template embeddings."""
    import numpy as np

    from windvane.semantic.config import load_sentence_transformer
    from windvane.capture import _get_or_build_template_cache

    model = load_sentence_transformer()

    # _get_or_build_template_cache is signature-stamped: it rebuilds the
    # template embeddings automatically when the configured model changed.
    cache = _get_or_build_template_cache()
    if cache is None:
        raise RuntimeError("could not build decision template cache")
    decision_embs = np.array(cache["decision_embeddings"])
    non_decision_embs = np.array(cache["non_decision_embeddings"])

    return model, decision_embs, non_decision_embs


def _score_text(text, model, decision_embs, non_decision_embs):
    """Score a single text against templates. Returns (score, extracted_text)."""
    import numpy as np
    from windvane.capture import DECISION_THRESHOLD, AMBIGUITY_MARGIN

    if len(text.strip()) < 15:
        return 0.0, ""

    sentences = re.split(r"(?<=[.!])\s+|\n+", text)
    sentences = [s.strip()[:MAX_ENCODE_CHARS] for s in sentences if len(s.strip()) > 15]
    if not sentences:
        # No sentence break at all (a pasted log, code, one long line) -- this
        # is the case that used to hand the encoder the entire blob.
        sentences = [text.strip()[:MAX_ENCODE_CHARS]]

    best_score = 0.0
    best_text = ""

    for sentence in sentences[:5]:
        emb = model.encode([sentence], normalize_embeddings=True)
        d_sims = np.dot(decision_embs, emb.T).flatten()
        nd_sims = np.dot(non_decision_embs, emb.T).flatten()

        best_d = float(np.max(d_sims))
        best_nd = float(np.max(nd_sims))

        if best_d >= DECISION_THRESHOLD and (best_d - best_nd) >= AMBIGUITY_MARGIN:
            score = min((best_d - 0.3) / 0.5, 1.0)
            if score > best_score:
                best_score = score
                # Word-boundary cut -- mid-sentence truncation made stored
                # decisions unreadable when resurfaced in banners.
                best_text = (
                    sentence[:300].rsplit(" ", 1)[0]
                    if len(sentence) > 300
                    else sentence
                )

    return best_score, best_text


class _ModelHolder:
    """The embedding model, loaded lazily in a background thread so the
    server can bind and serve hook events immediately. Embedding/scoring
    requests wait on `ready`; hook events never touch it.

    ``device_file``, when given, receives the device actually in use (cuda vs
    cpu) once the model is loaded, readable without importing torch."""

    def __init__(self, device_file: "Path | None" = None):
        self.ready = threading.Event()
        self.model = None
        self.decision_embs = None
        self.non_decision_embs = None
        self.device_file = device_file

    def load(self):
        try:
            (
                self.model,
                self.decision_embs,
                self.non_decision_embs,
            ) = _load_model_and_templates()
            try:
                if self.device_file is not None:
                    self.device_file.write_text(str(self.model.device))
            except Exception:
                pass
            print(f"Model loaded on {self.model.device}.", file=sys.stderr)
        except Exception as e:
            print(f"Model load failed: {e}", file=sys.stderr)
        finally:
            self.ready.set()

    def wait(self, timeout: float = 20.0) -> bool:
        self.ready.wait(timeout)
        return self.model is not None


# Every model call runs on ONE long-lived thread, never on the ephemeral
# per-connection thread. PyTorch keeps per-thread state that is not released
# when a thread dies, so encoding from a fresh thread per request leaked
# ~0.73 MB each -- measured dead-linear, +146 MB per 200 requests, no plateau,
# which is what drove the daemon past 4 GB over a long session. The same
# encode loop on a single thread is flat. max_workers=1 also matches what
# actually happened before: the GIL plus torch serialized these anyway.
_MODEL_POOL = None
_MODEL_POOL_LOCK = threading.Lock()


def _model_pool():
    global _MODEL_POOL
    if _MODEL_POOL is None:
        with _MODEL_POOL_LOCK:
            if _MODEL_POOL is None:
                from concurrent.futures import ThreadPoolExecutor

                _MODEL_POOL = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="windvane-model"
                )
    return _MODEL_POOL


def _on_model_thread(fn, *args, **kwargs):
    """Run a model call on the pinned thread and wait for it."""
    return _model_pool().submit(fn, *args, **kwargs).result()


def serve_model_request(request: dict, holder: "_ModelHolder") -> bytes:
    """Answer one embed / embed_batch / score request with the loaded model.
    Returns the JSON line to send. Every model call goes through
    ``_on_model_thread``; never call the model from the request thread."""
    # Everything here needs the model. A hook-path client (a score, a single
    # embed) sends ``nowait``: it has a one- or two-second budget and a regex
    # tier behind it, so a model still loading is answered at once and the
    # caller degrades, instead of the client burning its whole budget on a
    # wait it cannot win. The daemon reloads after its idle timeout and
    # after an engine edit, and the load takes about 20 s, so the first
    # prompts of a morning hit this window: a hook's own scoring and
    # embedding calls each waited out their timeouts against the loading
    # model and the prompt hook ran past its 5 s (2026-10-08). The bulk
    # clients (the miner, search) still wait out the load.
    if request.get("nowait") and not holder.ready.is_set():
        return b'{"error": "model loading"}\n'
    if not holder.wait():
        return b'{"error": "model unavailable"}\n'
    model = holder.model
    decision_embs = holder.decision_embs
    non_decision_embs = holder.non_decision_embs

    if "embed_batch" in request:
        # Batch embedding: encode all texts in one model call. GPU takes
        # much larger batches without breaking a sweat; CPU keeps 16.
        texts = [t[:MAX_ENCODE_CHARS] for t in request["embed_batch"]]
        if texts:
            # Same cap as the bulk worker. The resident daemon normally
            # sits on cpu, but WINDVANE_DEVICE=cuda puts it on the
            # GPU -- and a 256-row batch there parks multi-GB of activations
            # in a process that never exits, which is strictly worse than
            # the transient worker's spike.
            from windvane.semantic.worker import cpu_batch_size, gpu_batch_size

            on_gpu = str(getattr(model, "device", "cpu")).startswith(
                ("cuda", "mps")
            )
            embs = _on_model_thread(
                model.encode,
                texts,
                normalize_embeddings=True,
                batch_size=gpu_batch_size() if on_gpu else cpu_batch_size(),
            )
            response = json.dumps({"embeddings": embs.tolist()}) + "\n"
        else:
            response = json.dumps({"embeddings": []}) + "\n"
    elif "embed" in request:
        # Single embedding request: return raw vector
        text = request["embed"][:MAX_ENCODE_CHARS]
        emb = _on_model_thread(model.encode, [text], normalize_embeddings=True)
        response = json.dumps({"embedding": emb[0].tolist()}) + "\n"
    else:
        # Decision scoring request
        text = request.get("text", "")
        score, extracted = _on_model_thread(
            _score_text, text, model, decision_embs, non_decision_embs
        )
        response = json.dumps({"score": score, "text": extracted}) + "\n"
    return response.encode("utf-8")
