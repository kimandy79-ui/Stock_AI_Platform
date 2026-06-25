"""Tests for Module 02 — DuckDB Manager.

Covers, per CODING_STANDARDS.md section 12 and the Module 02 task requirements:

- only approved roles are accepted; unknown role raises a clear error;
- paths resolve exactly to the prod/debug/simulation paths from settings;
- paths are read dynamically from settings at call time (not cached at import);
- ``connect`` returns a DuckDB connection for approved roles;
- read_only behavior is respected (file must pre-exist for read_only);
- arbitrary DB paths cannot be passed into the public API;
- no schema tables are created by Module 02;
- database directory creation is idempotent;
- the simulation prod read-only attach helper attaches only the approved prod
  path, attached prod is not writable, and the simulation DB stays writable;
- type/import smoke checks.

CRITICAL: every test that opens a DuckDB connection redirects the settings DB
paths into pytest ``tmp_path`` via ``monkeypatch.setattr(settings, ...)`` so
that no real file under the real ``data/duckdb/`` folder is created or modified.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from app.config import settings
from app.database import duckdb_manager as dbm


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect all DuckDB settings paths into ``tmp_path``.

    Returns a mapping of role -> redirected path. This guarantees no test
    touches the real ``data/duckdb/`` tree.
    """
    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"

    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)

    return {
        dbm.DB_ROLE_PROD: prod,
        dbm.DB_ROLE_DEBUG: debug,
        dbm.DB_ROLE_SIMULATION: simulation,
    }


# --------------------------------------------------------------------------- #
# Role validation
# --------------------------------------------------------------------------- #
class TestRoleValidation:
    """Only approved roles are accepted; unknown roles fail clearly."""

    def test_allowed_roles_exact(self) -> None:
        assert dbm.ALLOWED_DB_ROLES == ("prod", "debug", "simulation")

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_get_database_path_accepts_approved_roles(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        assert dbm.get_database_path(role) == tmp_db_paths[role]

    @pytest.mark.parametrize("bad_role", ["", "PROD", "production", "unknown", "test"])
    def test_unknown_role_raises_clear_error(self, bad_role: str) -> None:
        with pytest.raises(dbm.UnknownDatabaseRoleError) as exc_info:
            dbm.get_database_path(bad_role)
        # Clear error: message names the offending role and lists valid roles.
        message = str(exc_info.value)
        assert repr(bad_role) in message
        assert "prod" in message and "debug" in message and "simulation" in message

    def test_unknown_role_is_value_error_subclass(self) -> None:
        # ValueError subclass keeps it catchable by generic callers.
        assert issubclass(dbm.UnknownDatabaseRoleError, ValueError)


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #
class TestPathResolution:
    """Paths resolve exactly to settings, and dynamically at call time."""

    def test_paths_match_settings_exactly(self, tmp_db_paths: dict[str, Path]) -> None:
        assert dbm.get_database_path("prod") == settings.PROD_DB_PATH
        assert dbm.get_database_path("debug") == settings.DEBUG_DB_PATH
        assert dbm.get_database_path("simulation") == settings.SIMULATION_DB_PATH

    def test_returns_pathlib_path(self, tmp_db_paths: dict[str, Path]) -> None:
        assert isinstance(dbm.get_database_path("prod"), Path)

    def test_paths_read_dynamically_not_cached_at_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changing settings after import must change resolved paths.

        This is the regression guard against caching paths at import time
        (e.g. ``PROD_PATH = settings.PROD_DB_PATH`` or a dict built from
        ``settings.*`` at module load).
        """
        first = tmp_path / "first" / "prod.duckdb"
        second = tmp_path / "second" / "prod.duckdb"

        monkeypatch.setattr(settings, "PROD_DB_PATH", first, raising=True)
        assert dbm.get_database_path("prod") == first

        # Re-point settings; the manager must observe the new value live.
        monkeypatch.setattr(settings, "PROD_DB_PATH", second, raising=True)
        assert dbm.get_database_path("prod") == second

    def test_role_map_stores_attr_names_not_paths(self) -> None:
        # Internal guard: the role map holds settings attribute *names*, which is
        # what keeps resolution dynamic. If someone swaps in Path values here,
        # dynamic resolution and monkeypatching would silently break.
        for value in dbm._ROLE_TO_SETTINGS_ATTR.values():
            assert isinstance(value, str)
            assert hasattr(settings, value)


# --------------------------------------------------------------------------- #
# Arbitrary path rejection
# --------------------------------------------------------------------------- #
class TestArbitraryPathRejection:
    """Callers cannot pass arbitrary filesystem paths through the public API."""

    def test_public_api_has_no_path_parameter(self) -> None:
        import inspect

        for fn in (dbm.get_database_path, dbm.connect):
            params = set(inspect.signature(fn).parameters)
            # The only role-selecting parameter is ``db_role``; there is no
            # ``path``/``database`` parameter to inject an arbitrary file.
            assert "path" not in params
            assert "database" not in params
            assert "db_role" in params

    def test_filesystem_path_as_role_is_rejected(self, tmp_path: Path) -> None:
        arbitrary = str(tmp_path / "evil.duckdb")
        with pytest.raises(dbm.UnknownDatabaseRoleError):
            dbm.get_database_path(arbitrary)
        with pytest.raises(dbm.UnknownDatabaseRoleError):
            dbm.connect(arbitrary)


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
class TestConnections:
    """connect() returns DuckDB connections and respects read_only."""

    @pytest.mark.parametrize("role", ["prod", "debug", "simulation"])
    def test_connect_returns_connection(
        self, role: str, tmp_db_paths: dict[str, Path]
    ) -> None:
        conn = dbm.connect(role)
        try:
            assert isinstance(conn, duckdb.DuckDBPyConnection)
            # A trivial query confirms the connection is live.
            assert conn.execute("SELECT 1").fetchone() == (1,)
        finally:
            conn.close()
        # The file was created under tmp_path, not the real data tree.
        assert tmp_db_paths[role].exists()

    def test_role_specific_helpers(self, tmp_db_paths: dict[str, Path]) -> None:
        for connect_fn, role in (
            (dbm.connect_prod, "prod"),
            (dbm.connect_debug, "debug"),
            (dbm.connect_simulation, "simulation"),
        ):
            conn = connect_fn()
            try:
                assert isinstance(conn, duckdb.DuckDBPyConnection)
            finally:
                conn.close()
            assert tmp_db_paths[role].exists()

    def test_connect_creates_parent_directory(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # The duckdb dir does not exist yet; connect() must create it.
        assert not tmp_db_paths["prod"].parent.exists()
        conn = dbm.connect_prod()
        try:
            assert tmp_db_paths["prod"].parent.is_dir()
        finally:
            conn.close()

    def test_read_only_requires_existing_file(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # DuckDB read_only=True requires the file to already exist.
        # Step 1: create the DB file with a read-write connection, then close.
        rw = dbm.connect_prod(read_only=False)
        rw.close()
        assert tmp_db_paths["prod"].exists()

        # Step 2: reopening read-only now succeeds.
        ro = dbm.connect_prod(read_only=True)
        try:
            assert ro.execute("SELECT 1").fetchone() == (1,)
            # Step 3: writes through a read-only connection must fail.
            with pytest.raises(duckdb.Error):
                ro.execute("CREATE TABLE should_fail (x INTEGER)")
        finally:
            ro.close()

    def test_read_only_on_missing_file_raises(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # Opening a non-existent database read-only raises (file must pre-exist).
        assert not tmp_db_paths["debug"].exists()
        with pytest.raises(duckdb.Error):
            dbm.connect_debug(read_only=True)


# --------------------------------------------------------------------------- #
# No schema creation (Module 02 scope guard)
# --------------------------------------------------------------------------- #
class TestNoSchemaCreation:
    """Module 02 must not create any schema tables (that is Module 03)."""

    def test_fresh_connection_has_no_tables(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        conn = dbm.connect_prod()
        try:
            rows = conn.execute("SHOW TABLES").fetchall()
            assert rows == []
        finally:
            conn.close()

    def test_known_core_tables_absent(self, tmp_db_paths: dict[str, Path]) -> None:
        forbidden = [
            "schema_versions",
            "pipeline_runs",
            "ticker_master",
            "daily_prices",
            "daily_features",
        ]
        conn = dbm.connect_prod()
        try:
            table_names = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
            assert table_names.isdisjoint(forbidden)
        finally:
            conn.close()

    def test_manager_source_has_no_table_ddl(self) -> None:
        # Static guard: the manager source must not contain CREATE TABLE DDL.
        source = Path(dbm.__file__).read_text(encoding="utf-8")
        upper = source.upper()
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper


# --------------------------------------------------------------------------- #
# Directory creation idempotency
# --------------------------------------------------------------------------- #
class TestEnsureDatabaseDirectory:
    """ensure_database_directory() is idempotent and uses settings.DUCKDB_DIR."""

    def test_creates_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "duckdb"
        monkeypatch.setattr(settings, "DUCKDB_DIR", target, raising=True)
        assert not target.exists()
        result = dbm.ensure_database_directory()
        assert result == target
        assert target.is_dir()

    def test_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "duckdb"
        monkeypatch.setattr(settings, "DUCKDB_DIR", target, raising=True)
        # Calling repeatedly must not raise.
        dbm.ensure_database_directory()
        dbm.ensure_database_directory()
        assert target.is_dir()

    def test_reads_dir_dynamically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = tmp_path / "a"
        second = tmp_path / "b"
        monkeypatch.setattr(settings, "DUCKDB_DIR", first, raising=True)
        assert dbm.ensure_database_directory() == first
        monkeypatch.setattr(settings, "DUCKDB_DIR", second, raising=True)
        assert dbm.ensure_database_directory() == second


# --------------------------------------------------------------------------- #
# Simulation prod read-only attach
# --------------------------------------------------------------------------- #
class TestSimulationAttach:
    """The prod read-only attach helper is safe and path-locked to settings."""

    def _create_prod_with_marker(self, prod_path: Path) -> None:
        """Create the prod DB with one marker table/row to read back later."""
        conn = dbm.connect_prod(read_only=False)
        try:
            conn.execute("CREATE TABLE marker (id INTEGER)")
            conn.execute("INSERT INTO marker VALUES (42)")
        finally:
            conn.close()
        assert prod_path.exists()

    def test_attach_uses_only_settings_prod_path(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # Pre-create prod (required for read-only attach) with a marker row.
        self._create_prod_with_marker(tmp_db_paths["prod"])

        sim = dbm.connect_simulation(read_only=False)
        try:
            alias = dbm.attach_prod_read_only(sim)
            assert alias == "prod"
            # The attached schema reads exactly the settings prod DB's data.
            value = sim.execute("SELECT id FROM prod.marker").fetchone()
            assert value == (42,)
        finally:
            sim.close()

    def test_attach_helper_takes_no_prod_path_argument(self) -> None:
        import inspect

        params = set(inspect.signature(dbm.attach_prod_read_only).parameters)
        # Only ``connection`` and ``alias`` — no way to pass an arbitrary prod
        # path; prod is always resolved from settings.PROD_DB_PATH.
        assert params == {"connection", "alias"}

    def test_writing_to_attached_prod_fails(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        self._create_prod_with_marker(tmp_db_paths["prod"])

        sim = dbm.connect_simulation(read_only=False)
        try:
            dbm.attach_prod_read_only(sim)
            # Prod is attached read-only: writes to it must fail.
            with pytest.raises(duckdb.Error):
                sim.execute("INSERT INTO prod.marker VALUES (99)")
        finally:
            sim.close()

    def test_simulation_db_remains_writable(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        self._create_prod_with_marker(tmp_db_paths["prod"])

        sim = dbm.connect_simulation(read_only=False)
        try:
            dbm.attach_prod_read_only(sim)
            # The simulation DB itself stays writable while prod is read-only.
            sim.execute("CREATE TABLE sim_local (x INTEGER)")
            sim.execute("INSERT INTO sim_local VALUES (7)")
            assert sim.execute("SELECT x FROM sim_local").fetchone() == (7,)
        finally:
            sim.close()

    def test_attach_missing_prod_raises_file_not_found(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        # Prod file does not exist yet -> read-only attach cannot succeed.
        assert not tmp_db_paths["prod"].exists()
        sim = dbm.connect_simulation(read_only=False)
        try:
            with pytest.raises(FileNotFoundError):
                dbm.attach_prod_read_only(sim)
        finally:
            sim.close()

    def test_invalid_alias_rejected(self, tmp_db_paths: dict[str, Path]) -> None:
        self._create_prod_with_marker(tmp_db_paths["prod"])
        sim = dbm.connect_simulation(read_only=False)
        try:
            with pytest.raises(ValueError):
                dbm.attach_prod_read_only(sim, alias="bad alias;")
        finally:
            sim.close()

    def test_connect_simulation_with_prod_convenience(
        self, tmp_db_paths: dict[str, Path]
    ) -> None:
        self._create_prod_with_marker(tmp_db_paths["prod"])
        sim = dbm.connect_simulation_with_prod(read_only=False)
        try:
            # Prod attached read-only and readable; sim still writable.
            assert sim.execute("SELECT id FROM prod.marker").fetchone() == (42,)
            with pytest.raises(duckdb.Error):
                sim.execute("INSERT INTO prod.marker VALUES (1)")
            sim.execute("CREATE TABLE sim_only (a INTEGER)")
        finally:
            sim.close()


# --------------------------------------------------------------------------- #
# Type / import smoke
# --------------------------------------------------------------------------- #
class TestImportSmoke:
    """Import and basic symbol presence smoke checks."""

    def test_public_symbols_present(self) -> None:
        for name in (
            "get_database_path",
            "connect",
            "connect_prod",
            "connect_debug",
            "connect_simulation",
            "ensure_database_directory",
            "attach_prod_read_only",
            "connect_simulation_with_prod",
            "ALLOWED_DB_ROLES",
            "UnknownDatabaseRoleError",
        ):
            assert hasattr(dbm, name)

    def test_module_has_docstring(self) -> None:
        assert dbm.__doc__ is not None and dbm.__doc__.strip()
