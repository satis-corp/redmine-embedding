from __future__ import annotations


class RedmineError(Exception):
    """Base class for errors talking to Redmine."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RedmineAuthError(RedmineError):
    """401: token / API key missing, invalid or expired."""


class RedmineForbiddenError(RedmineError):
    """403: authenticated, but not allowed to see the resource."""


class RedmineNotFoundError(RedmineError):
    """404: missing, or invisible to the caller (Redmine does not distinguish)."""


class RedmineRedirectError(RedmineError):
    """3xx: redirects are not followed (wiki rename, redirect to the login page)."""


class AttachmentTooLargeError(RedmineError):
    """The attachment is over the size limit; it was not (fully) downloaded."""


class ForeignHostError(RedmineError):
    """``content_url`` points to another host; the API key was not sent."""
