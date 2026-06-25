# M06 Universe Snapshot — Sector Normalization Delta

Provider raw sector is normalized to a canonical internal sector before being
written to `ticker_master.sector` and `ticker_universe_snapshot.sector`.

- New public helper: `universe_snapshot.normalize_sector(raw) -> canonical`.
- Resolution: `constants.SECTOR_ALIAS_MAP` (exact, then case-insensitive),
  canonical pass-through, else raw unchanged. `None`/empty preserved.
- `apply_snapshot` public signature and `METADATA_KEYS` are unchanged.
- `sector_alias_map` in DB is seeded from the same constant for visibility;
  runtime normalization uses the constant (single source of truth).
