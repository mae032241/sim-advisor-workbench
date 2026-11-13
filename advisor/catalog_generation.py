"""Persistent active-citation generations, invalidated by SQLite writers.

The cache belongs to the catalog rather than a Store instance or pathname.
Readers check its triggers in the same snapshot as its value, so replacing a
catalog or rebuilding its active table cannot reuse an unrelated cached hash.
"""
from __future__ import annotations

from contextlib import closing


_TABLE = "_active_generation_cache"
_TRIGGERS = tuple("active_generation_" + event for event in ("insert", "delete", "update"))
_SCHEMA_OBJECTS = {_TABLE: ("table", _TABLE), **{name: ("trigger", "active") for name in _TRIGGERS}}


def _schema_ready(connection):
    marks = ",".join("?" for _ in _SCHEMA_OBJECTS)
    actual = {row[0]: (row[1], row[2]) for row in connection.execute(
        "SELECT name,type,tbl_name FROM sqlite_master WHERE name IN (" + marks + ")", tuple(_SCHEMA_OBJECTS))}
    return actual == _SCHEMA_OBJECTS


def _cached(connection):
    row = connection.execute("SELECT generation FROM " + _TABLE + " WHERE id=1").fetchone()
    return row[0] if row else None


def _initialize(connection):
    """Install tracking under the caller's IMMEDIATE transaction, if needed."""
    if _schema_ready(connection):
        return
    connection.execute("CREATE TABLE IF NOT EXISTS " + _TABLE + "("
                       "id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT "
                       "CHECK(generation IS NULL OR length(generation)=24))")
    connection.execute("INSERT OR IGNORE INTO " + _TABLE + " VALUES(1,NULL)")
    # A migration may have rebuilt active (which drops its triggers) while
    # retaining this table. Its old generation is then no longer authoritative.
    connection.execute("UPDATE " + _TABLE + " SET generation=NULL WHERE id=1")
    for event, name in zip(("INSERT", "DELETE", "UPDATE"), _TRIGGERS):
        changed = ""
        if event == "UPDATE":
            # UPDATE OR REPLACE can delete a conflicting primary-key/rowid
            # owner without running DELETE triggers (recursive_triggers=OFF).
            # Such a move must invalidate even when its own citation is equal.
            changed = "(OLD.citation_id IS NOT NEW.citation_id OR OLD.library_id IS NOT NEW.library_id OR " \
                      "OLD.object_id IS NOT NEW.object_id OR OLD.rowid IS NOT NEW.rowid) AND "
        # Once dirty, bulk imports do only a singleton lookup per affected row;
        # they neither increment a counter nor repeatedly write the metadata.
        connection.execute("CREATE TRIGGER IF NOT EXISTS " + name + " AFTER " + event + " ON active "
                           "WHEN " + changed + "(SELECT generation FROM " + _TABLE + " WHERE id=1) IS NOT NULL "
                           "BEGIN UPDATE " + _TABLE + " SET generation=NULL WHERE id=1; END")


def cached_generation(connection, ordered_identity_hash):
    """Return the legacy hash, scanning identities only after catalog changes.

    The caller supplies a fresh connection. A warm read uses one read snapshot
    and no active-table access. Cold readers serialize, recheck, and compute
    under a write reservation: no concurrent active write can be overwritten
    by a hash of an older snapshot. Trigger effects and cache writes follow
    the active transaction's commit/rollback semantics, including raw SQL.
    """
    with connection:
        connection.execute("BEGIN")
        if _schema_ready(connection):
            generation = _cached(connection)
            if generation is not None:
                return generation
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        _initialize(connection)
        generation = _cached(connection)
        if generation is None:
            with closing(connection.execute("SELECT citation_id FROM active ORDER BY citation_id")) as rows:
                generation = ordered_identity_hash(rows)[0][:24]
            connection.execute("INSERT INTO " + _TABLE + " VALUES(1,?) "
                               "ON CONFLICT(id) DO UPDATE SET generation=excluded.generation", (generation,))
        return generation
