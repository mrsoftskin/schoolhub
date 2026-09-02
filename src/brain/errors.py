"""Exceptions used across the core library.

Everything user-facing fails loud: these carry enough detail for the CLI and
web layers to print exactly what went wrong and where.
"""

from __future__ import annotations


class BrainError(Exception):
    """Base class for all expected, user-reportable failures."""


class ConfigError(BrainError):
    """config.toml is missing, malformed, or fails validation."""


class ParseError(BrainError):
    """A source file could not be parsed into chunks."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class EmptyIndexError(BrainError):
    """A collection has no indexed chunks at all. 'all' means the whole index
    is empty - it is the reserved global-mode name, never a real collection."""

    def __init__(self, collection: str):
        self.collection = collection
        if collection == "all":
            super().__init__("No collection has any indexed chunks. Run: brain index")
        else:
            super().__init__(
                f"Collection '{collection}' has no indexed chunks. "
                f"Run: brain index --collection {collection}"
            )


class IndexBusy(BrainError):
    """An index run is already in progress in this process.

    Indexing rewrites the shared embedding store, so two runs interleaving can
    leave a vector paired with text it was not built from.
    """


class StoreOutOfSync(BrainError):
    """Chunks and vectors disagree; the index must be rebuilt.

    Carries `collection` when one is known, so a caller can offer to
    reindex exactly that collection instead of printing a CLI command at
    someone who has no terminal.
    """

    def __init__(self, message: str, collection: str | None = None):
        super().__init__(message)
        self.collection = collection


class AssistBlocked(BrainError):
    """assist_level forbids this request. Hard branch - raised before any API call."""

    def __init__(self, collections: list[str], reason: str):
        self.collections = collections
        self.reason = reason
        super().__init__(reason)


class NoRelevantResults(BrainError):
    """Every retrieved chunk scored below the similarity floor."""

    def __init__(self, floor: float, best_score: float | None = None):
        self.floor = floor
        self.best_score = best_score
        detail = f" (best score {best_score:.3f})" if best_score is not None else ""
        super().__init__(
            f"Nothing relevant indexed for this question - all chunks scored "
            f"below the similarity floor {floor}{detail}. Not calling the API."
        )


class MissingAPIKeyError(BrainError):
    """The API key for the selected key-based backend is not set. A custom
    message names the exact variable and provider; the no-arg default keeps the
    original Anthropic wording for back-compat."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or (
            "ANTHROPIC_API_KEY is not set, and [settings] backend = \"api\" "
            "bills through the Anthropic API. Put the key in .env, or switch "
            "to backend = \"subscription\" to answer through your Claude Code "
            "login instead (no key, no separate bill)."
        ))


class BackendUnavailable(BrainError):
    """The configured answer backend cannot run."""
