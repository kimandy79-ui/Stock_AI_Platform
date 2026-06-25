"""DuckDB connection manager for the Swing Trading Stock Analyzer (Module 02).

This module is the single, centralized place that opens DuckDB connections.
Per ``CODING_STANDARDS.md`` section 8 and ``ARCHITECTURE.md`` section 5:

- Use the DuckDB manager for all DB access.
- No module opens arbitrary DB paths directly.
- Use separate DB files for the three roles: ``prod`` / ``debug`` /
  ``simulation``.
- Simulation may attach ``prod`` read-only only.

Design notes
------------
**Approved roles only.** The public API accepts a *role* string
(``prod`` / ``debug`` / ``simulation``), never a filesystem path. Callers
cannot point the manager at an arbitrary database file.

**Dynamic path resolution.** DB paths are read from :mod:`app.config.settings`
*at call time* via attribute name lookup. Paths are deliberately NOT cached at
import time (e.g. ``PROD_PATH = settings.PROD_DB_PATH``), so that:

- tests can ``monkeypatch.setattr(settings, "PROD_DB_PATH", tmp_path / ...)``;
- future environment / path overrides keep working.

The module-level ``_ROLE_TO_SETTINGS_ATTR`` map stores the *attribute names*
(strings), not the path values, which is what keeps resolution dynamic.

**Out of scope (Module 03+).** This module never creates schema tables, never
runs migrations, never calls providers, and never downloads data.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Mapping

import duckdb

from app.config import settings
from app.utils import logging_config

# Module logger (run_id-aware). Logging is configured by Module 01's
# ``logging_config.configure_logging``; here we only emit records.
_LOG = logging_config.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Approved database roles
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = "prod"
DB_ROLE_DEBUG: Final[str] = "debug"
DB_ROLE_SIMULATION: Final[str] = "simulation"

ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (
    DB_ROLE_PROD,
    DB_ROLE_DEBUG,
    DB_ROLE_SIMULATION,
)

# Map each role to the NAME of the settings attribute that holds its path.
# Storing attribute names (not the resolved paths) is what keeps path
# resolution dynamic and monkeypatch-friendly. Do NOT replace these with the
# actual ``settings.*_DB_PATH`` values.
_ROLE_TO_SETTINGS_ATTR: Final[Mapping[str, str]] = {
    DB_ROLE_PROD: "PROD_DB_PATH",
    DB_ROLE_DEBUG: "DEBUG_DB_PATH",
    DB_ROLE_SIMULATION: "SIMULATION_DB_PATH",
}

# Default alias used when attaching prod read-only to a simulation connection.
DEFAULT_PROD_ALIAS: Final[str] = "prod"

# Aliases must be safe SQL identifiers (no quoting/injection surprises).
_VALID_ALIAS_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnknownDatabaseRoleError(ValueError):
    """Raised when a caller requests a database role that is not approved."""


def _validate_role(db_role: str) -> str:
    """Return ``db_role`` if it is an approved role, else raise.

    Raises
    ------
    UnknownDatabaseRoleError
        If ``db_role`` is not one of :data:`ALLOWED_DB_ROLES`. The error message
        lists the valid roles so callers (and tests) get a clear signal. This is
        also the guard that prevents arbitrary filesystem paths from being used
        as a "role".
    """
    if db_role not in _ROLE_TO_SETTINGS_ATTR:
        raise UnknownDatabaseRoleError(
            f"Unknown database role {db_role!r}. "
            f"Valid roles: {sorted(ALLOWED_DB_ROLES)}"
        )
    return db_role


def get_database_path(db_role: str) -> Path:
    """Resolve the on-disk DuckDB path for an approved ``db_role``.

    The path is read from :mod:`app.config.settings` *at call time* (dynamic
    resolution), so tests and environment overrides that change the settings
    attribute are respected.

    Parameters
    ----------
    db_role:
        One of ``prod``, ``debug``, ``simulation``.

    Returns
    -------
    pathlib.Path
        The resolved database file path.

    Raises
    ------
    UnknownDatabaseRoleError
        If ``db_role`` is not approved.
    """
    _validate_role(db_role)
    attr_name = _ROLE_TO_SETTINGS_ATTR[db_role]
    # Dynamic read: getattr at call time, never cached at import.
    raw_path = getattr(settings, attr_name)
    return Path(raw_path)


def ensure_database_directory() -> Path:
    """Ensure the DuckDB data directory exists and return it.

    Reads ``settings.DUCKDB_DIR`` dynamically and creates it with
    ``parents=True, exist_ok=True``. Idempotent: calling it repeatedly is safe
    and never raises if the directory already exists.

    Returns
    -------
    pathlib.Path
        The ensured DuckDB directory.
    """
    duckdb_dir = Path(getattr(settings, "DUCKDB_DIR"))
    duckdb_dir.mkdir(parents=True, exist_ok=True)
    return duckdb_dir


def _ensure_parent_directory(path: Path) -> None:
    """Create the parent directory of ``path`` if needed (idempotent)."""
    path.parent.mkdir(parents=True, exist_ok=True)


def connect(
    db_role: str,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection for an approved ``db_role``.

    Parameters
    ----------
    db_role:
        One of ``prod``, ``debug``, ``simulation``.
    read_only:
        If ``True``, open the database read-only. Note DuckDB requires the
        database file to already exist when opening read-only; opening a
        non-existent file read-only raises.

    Returns
    -------
    duckdb.DuckDBPyConnection
        An open connection. The caller owns the connection and is responsible
        for closing it.

    Raises
    ------
    UnknownDatabaseRoleError
        If ``db_role`` is not approved.
    """
    path = get_database_path(db_role)
    # Ensure the containing directory exists so a fresh read-write open works.
    # For read_only opens the directory is harmless; the file existence
    # requirement is enforced by DuckDB itself.
    _ensure_parent_directory(path)

    _LOG.info(
        "opening duckdb connection role=%s read_only=%s path=%s",
        db_role,
        read_only,
        path,
    )
    return duckdb.connect(database=str(path), read_only=read_only)


def connect_prod(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a connection to the production database."""
    return connect(DB_ROLE_PROD, read_only=read_only)


def connect_debug(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a connection to the debug database."""
    return connect(DB_ROLE_DEBUG, read_only=read_only)


def connect_simulation(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a connection to the simulation database."""
    return connect(DB_ROLE_SIMULATION, read_only=read_only)


def _validate_alias(alias: str) -> str:
    """Return ``alias`` if it is a safe SQL identifier, else raise ValueError."""
    if not _VALID_ALIAS_RE.match(alias):
        raise ValueError(
            f"Invalid attach alias {alias!r}; must match {_VALID_ALIAS_RE.pattern}"
        )
    return alias


def attach_prod_read_only(
    connection: duckdb.DuckDBPyConnection,
    alias: str = DEFAULT_PROD_ALIAS,
) -> str:
    """Attach the production database read-only to an existing connection.

    This is the safe helper for the simulation use case described in
    ``ARCHITECTURE.md`` section 5 and ``MASTER_SPEC.md`` section 17
    (``ATTACH prod.duckdb READ_ONLY``). It deliberately accepts NO prod path
    argument: the prod path is always resolved from ``settings.PROD_DB_PATH`` at
    call time, so callers cannot attach an arbitrary database as "prod".

    This module does not implement any simulation logic; it only performs the
    read-only attach so that a simulation connection can read prod data without
    risk of writing to it.

    Parameters
    ----------
    connection:
        An already-open DuckDB connection (typically a simulation connection).
    alias:
        Schema alias for the attached prod database. Must be a valid SQL
        identifier. Defaults to ``prod``.

    Returns
    -------
    str
        The alias under which prod was attached.

    Raises
    ------
    ValueError
        If ``alias`` is not a valid identifier.
    FileNotFoundError
        If the resolved prod database file does not exist (DuckDB cannot attach
        a non-existent database read-only).
    """
    _validate_alias(alias)
    prod_path = get_database_path(DB_ROLE_PROD)
    if not prod_path.exists():
        raise FileNotFoundError(
            f"Cannot attach prod read-only: database file does not exist: {prod_path}"
        )

    # The path comes only from settings; still, escape single quotes defensively
    # before embedding in the ATTACH statement (DuckDB ATTACH takes a literal).
    safe_path = str(prod_path).replace("'", "''")
    _LOG.info("attaching prod read-only as alias=%s path=%s", alias, prod_path)
    connection.execute(f"ATTACH '{safe_path}' AS {alias} (READ_ONLY)")
    return alias


def connect_simulation_with_prod(
    read_only: bool = False,
    prod_alias: str = DEFAULT_PROD_ALIAS,
) -> duckdb.DuckDBPyConnection:
    """Open a simulation connection with prod attached read-only.

    Convenience wrapper combining :func:`connect_simulation` and
    :func:`attach_prod_read_only`. The simulation database itself is opened with
    the requested ``read_only`` flag (default read-write so simulation can write
    to ``simulation.duckdb``), while prod is always attached read-only.

    Parameters
    ----------
    read_only:
        Read-only flag for the *simulation* database connection. Prod is always
        attached read-only regardless of this value.
    prod_alias:
        Alias for the attached prod database. Defaults to ``prod``.

    Returns
    -------
    duckdb.DuckDBPyConnection
        The simulation connection with prod attached read-only. The caller owns
        and must close it.
    """
    connection = connect_simulation(read_only=read_only)
    try:
        attach_prod_read_only(connection, alias=prod_alias)
    except Exception:
        # Do not leak the connection if the attach fails.
        connection.close()
        raise
    return connection
