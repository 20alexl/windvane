"""The optional semantic tier: the sentence-transformers encoder the daemon
keeps resident, the bulk embedding worker and the embedding configuration.

Everything here is optional. Without the ``semantic`` extra (numpy and
sentence-transformers) the daemon never loads an encoder, answers a scoring
or embedding request with "no semantic tier", and every caller uses its
regex tier. Importing this package imports nothing outside the standard
library; the heavy imports happen inside the functions that need them.

The tier is on when the plugin config sets ``"semantic": true`` or the
environment sets ``WINDVANE_SEMANTIC=1``, and the extra is installed.
"""

import importlib.util
import os

from windvane.semantic.config import (  # noqa: F401  (stdlib only; the hooks' seam)
    DEFAULT_MODEL,
    LEGACY_SIGNATURE,
    embed_signature,
    get_embed_config,
    load_sentence_transformer,
)
from windvane.semantic.encoder import (  # noqa: F401  (the daemon's seam)
    MAX_ENCODE_CHARS,
    _ModelHolder,
    _on_model_thread,
    _score_text,
    serve_model_request,
)

_EXTRA_MODULES = ("numpy", "sentence_transformers")
_AVAILABLE = None


def available() -> bool:
    """Is the semantic extra installed? Looks the modules up without
    importing them (sentence-transformers pulls in torch, seconds of work),
    so a hook can ask cheaply. Cached for the process."""
    global _AVAILABLE
    if _AVAILABLE is None:
        try:
            _AVAILABLE = all(
                importlib.util.find_spec(name) is not None for name in _EXTRA_MODULES
            )
        except (ImportError, ValueError):
            _AVAILABLE = False
    return _AVAILABLE


def imports() -> bool:
    """Does the extra actually import (not just exist on disk)? Imports
    sentence-transformers and with it torch, seconds of work: for a setup
    check, never for a hook."""
    try:
        import numpy  # noqa: F401
        import sentence_transformers  # noqa: F401
    except Exception:
        return False
    return True


def requested() -> bool:
    """Did the user ask for the semantic tier? ``WINDVANE_SEMANTIC`` decides
    when set (``1`` on, ``0`` off: a test run or a one-off process on a
    machine whose settings have the row on), else the plugin config row."""
    env = os.environ.get("WINDVANE_SEMANTIC", "").strip()
    if env == "1":
        return True
    if env == "0":
        return False
    try:
        from windvane.config import plugin_config

        value = (plugin_config() or {}).get("semantic")
    except Exception:
        return False
    if isinstance(value, str):
        # userConfig values can arrive as strings: "false" must read as off.
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def enabled() -> bool:
    """Requested AND installed: the only case in which the daemon loads an
    encoder and the scoring/embedding clients make a round trip."""
    return requested() and available()


def numpy_module():
    """numpy when the semantic tier is on, else None. The one door through
    which code outside this package reaches numpy: vectors exist only with
    the tier, and without it nothing needs (or imports) numpy."""
    if not enabled():
        return None
    try:
        import numpy

        return numpy
    except Exception:
        return None


__all__ = [
    "DEFAULT_MODEL",
    "LEGACY_SIGNATURE",
    "MAX_ENCODE_CHARS",
    "available",
    "embed_signature",
    "get_embed_config",
    "load_sentence_transformer",
    "enabled",
    "numpy_module",
    "requested",
    "serve_model_request",
]
