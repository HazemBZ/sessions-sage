"""sessions-sage: OpenCode session summarizer and reflection dashboard."""

try:
    from importlib.metadata import version as _v
    __version__ = _v("sessions-sage")
except Exception:
    __version__ = "0.0.0"
