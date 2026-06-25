"""ServiceResult contract for the Swing Trading Stock Analyzer.

This module defines the single, shared result object that every service
module in the platform returns. It is the canonical interface described in
``ARCHITECTURE.md`` (section 7) and ``CODING_STANDARDS.md`` (section 7).

Module 01 (Project Skeleton) only *defines* this contract. No service logic,
database access, or provider calls live here.

Allowed ``status`` values (per CODING_STANDARDS.md section 7):

- ``success``
- ``success_with_warnings``
- ``failed``

Notes
-----
The field order, names, types, and defaults below must match the shared
context exactly. Do not add or reorder fields without a logged decision in
``DECISIONS_LOG.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

# Allowed status values. Centralized so callers can validate against the
# contract instead of hardcoding string literals.
STATUS_SUCCESS: Final[str] = "success"
STATUS_SUCCESS_WITH_WARNINGS: Final[str] = "success_with_warnings"
STATUS_FAILED: Final[str] = "failed"

ALLOWED_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS, STATUS_FAILED}
)


@dataclass
class ServiceResult:
    """Standard return object for all service modules.

    Attributes
    ----------
    status:
        One of ``success``, ``success_with_warnings``, or ``failed``.
    run_id:
        UUID4 string identifying the pipeline / service run.
    rows_processed:
        Number of rows the service produced or touched. Defaults to ``0``.
    warnings:
        Non-fatal messages. Presence of warnings typically implies a
        ``success_with_warnings`` status.
    errors:
        Fatal or recoverable-failure messages.
    metadata:
        Free-form structured context (timings, counts, parameters, etc.).
    """

    status: str
    run_id: str
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_ok(self) -> bool:
        """Return ``True`` if the run did not fail.

        A result is considered "ok" when its status is either ``success`` or
        ``success_with_warnings``. A ``failed`` status returns ``False``.
        """
        return self.status in (STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS)

    def has_valid_status(self) -> bool:
        """Return ``True`` if ``status`` is one of the allowed contract values."""
        return self.status in ALLOWED_STATUSES

    def add_warning(self, message: str) -> None:
        """Append a warning message to the result."""
        self.warnings.append(message)

    def add_error(self, message: str) -> None:
        """Append an error message to the result."""
        self.errors.append(message)
