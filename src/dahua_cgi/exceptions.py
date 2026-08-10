"""
Exception hierarchy for the Dahua CGI SDK.

Applications using this SDK should catch DahuaError (or one of its
subclasses) rather than exceptions raised by third-party libraries.
"""

from __future__ import annotations


class DahuaError(Exception):
    """Base class for all SDK exceptions."""


class TransportError(DahuaError):
    """Raised when communication with the recorder fails."""


class AuthenticationError(DahuaError):
    """Raised when authentication with the recorder fails."""


class RecorderConnectionError(TransportError):
    """Raised when the recorder cannot be reached."""


class InvalidResponseError(DahuaError):
    """Raised when the recorder returns an unexpected response."""
