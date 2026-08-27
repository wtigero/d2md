"""Errors raised when a document cannot be processed safely."""


class ConversionError(RuntimeError):
    """Raised when an input cannot be converted safely or usefully."""
