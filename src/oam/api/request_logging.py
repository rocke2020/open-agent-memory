"""Request-local, content-free metadata shared with synchronous handlers."""

from contextvars import ContextVar

metadata: ContextVar[dict | None] = ContextVar("request_log_metadata", default=None)


def capture_authority(authority):
    state = metadata.get()
    if state is not None:
        state["authority"] = authority
    return authority
