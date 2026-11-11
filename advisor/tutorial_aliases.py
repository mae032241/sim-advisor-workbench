"""Explicit old tutorial citation aliases without retained duplicate bodies."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from .build_control import check_cancelled
from .catalog_storage import revision_storage
from .tutorial_migration import TutorialMigrationPlan


def ensure_alias_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS tutorial_aliases(
      citation_id TEXT PRIMARY KEY,owner_library_id TEXT NOT NULL,target_id TEXT NOT NULL,
      target_library_id TEXT NOT NULL,reason TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS tutorial_aliases_owner ON tutorial_aliases(owner_library_id)")
    db.execute("CREATE INDEX IF NOT EXISTS tutorial_aliases_target_owner ON tutorial_aliases(target_library_id)")


def lookup_alias(db, citation_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tutorial_aliases'").fetchone():
        return None
    row = db.execute("SELECT citation_id,owner_library_id,target_id,target_library_id,reason "
                     "FROM tutorial_aliases WHERE citation_id=?", (citation_id,)).fetchone()
    if row is None:
        return None
    return dict(zip(("citation_id", "owner_library_id", "target_id", "target_library_id", "reason"), row))


def alias_metadata(alias):
    return {"requested_id": alias["citation_id"], "resolved_id": alias["target_id"],
            "original_owner_library_id": alias["owner_library_id"], "target_library_id": alias["target_library_id"],
            "reason": alias["reason"], "relation": "canonical_reference"}


def require_alias_readable(store, alias):
    store.require_readable_library(alias["owner_library_id"])
    store.require_readable_library(alias["target_library_id"])


def alias_available(store, alias):
    visible = set(store.searchable_library_ids())
    if not {alias["owner_library_id"], alias["target_library_id"]} <= visible:
        return False
    with closing(store.connect()) as db:
        row = db.execute("SELECT library_id FROM revisions WHERE id=?", (alias["target_id"],)).fetchone()
    visible = set(store.searchable_library_ids())
    return bool(row and row[0] == alias["target_library_id"] and
                {alias["owner_library_id"], alias["target_library_id"]} <= visible)


def resolve_alias_source(store, alias, *, reader=None):
    """Return the canonical citation explicitly, with both ownership gates."""
    require_alias_readable(store, alias)
    source = (reader or store.get)(alias["target_id"])
    if source is None:
        return None
    if source.get("library_id") != alias["target_library_id"] or source.get("id") != alias["target_id"]:
        raise ValueError("Tutorial alias does not resolve to its pinned canonical owner")
    require_alias_readable(store, alias)
    return dict(source, alias=alias_metadata(alias), read_scope="canonical_reference")


def related_alias_sources(store, alias):
    source = resolve_alias_source(store, alias)
    if source is None:
        return []
    return [{"id": source["id"], "library_id": source["library_id"], "title": source["title"],
             "type": source["type"], "relation": "canonical_reference", "status": "relinked_tutorial_reference",
             "alias": alias_metadata(alias)}]


def read_alias_evidence(store, alias, *, index=0, line=None, limit=120):
    require_alias_readable(store, alias)
    value = store.read_evidence(alias["target_id"], index=index, line=line, limit=limit)
    require_alias_readable(store, alias)
    return dict(value, id=alias["target_id"], alias=alias_metadata(alias), read_scope="canonical_reference")


def purge_aliases(db, library_id):
    """A permanent purge of either owner removes its cross-library aliases."""
    return db.execute("DELETE FROM tutorial_aliases WHERE owner_library_id=? OR target_library_id=?",
                      (library_id, library_id)).rowcount


def has_canonical_document_reference(db, source):
    identity = source["id"]
    if source.get("case_group") and source.get("type") != "model_case":
        case = db.execute("SELECT citation_id FROM case_members WHERE group_id=? AND local_id='model-case'",
                          (source["case_group"],)).fetchone()
        if case:
            identity = case[0]
    return db.execute("SELECT 1 FROM tutorial_model_documents WHERE source_id=? LIMIT 1", (identity,)).fetchone() is not None


def apply_relink(db, plan, *, source_catalog=None):
    """Replace inactive planned tutorial payloads with lightweight aliases.

    The caller owns an offline transaction and subsequent VACUUM/publication.
    A staged catalog clone is permitted only when ``source_catalog`` explicitly
    identifies the original catalog recorded in the plan. Active canonical and
    model citation IDs/payloads are untouched. No index rebuild is needed.
    """
    if not db.in_transaction:
        raise ValueError("Tutorial alias relinking requires a caller-owned transaction")
    plan = plan if isinstance(plan, TutorialMigrationPlan) else TutorialMigrationPlan(plan)
    metadata = plan.metadata()
    if metadata.get("status") != "ready":
        raise ValueError("Tutorial alias relinking requires a ready migration plan")
    expected_catalog = Path(metadata["source_catalog"]).resolve()
    actual_catalog = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), None)
    if not actual_catalog or (Path(actual_catalog).resolve() != expected_catalog and
                              (source_catalog is None or Path(source_catalog).resolve() != expected_catalog)):
        raise ValueError("Relink catalog must match the plan or explicitly name its source catalog")
    ensure_alias_schema(db)
    db.execute("DROP TABLE IF EXISTS temp.tutorial_alias_plan")
    db.execute("""CREATE TEMP TABLE tutorial_alias_plan(
      citation_id TEXT PRIMARY KEY,object_id TEXT NOT NULL,owner_library_id TEXT NOT NULL,
      target_id TEXT NOT NULL,target_library_id TEXT NOT NULL,reason TEXT NOT NULL,source_sha256 TEXT NOT NULL)""")
    with closing(sqlite3.connect(plan.path.as_uri() + "?mode=ro", uri=True)) as staged:
        staged.execute("PRAGMA query_only=ON")
        query = """SELECT r.id,r.object_id,r.library_id,a.target_id,a.target_library_id,r.reason,r.source_sha256
          FROM retirements r LEFT JOIN relations a ON a.source_id=r.id ORDER BY r.id"""
        def rows():
            for row in staged.execute(query):
                check_cancelled()
                if row[3] is None or row[4] is None:
                    raise ValueError("Every retired tutorial must have one canonical reference")
                if row[5] not in {"model_tutorial_copy", "whole_tutorial_html", "verified_html_companion"}:
                    raise ValueError("Relink plan contains an unsupported retirement reason")
                yield row
        try:
            db.executemany("INSERT INTO temp.tutorial_alias_plan VALUES(?,?,?,?,?,?,?)", rows())
        except sqlite3.IntegrityError as exc:
            raise ValueError("Every retired tutorial must have exactly one canonical reference") from exc
    expected = db.execute("SELECT COUNT(*) FROM temp.tutorial_alias_plan").fetchone()[0]
    invalid_queries = (
        ("SELECT 1 FROM temp.tutorial_alias_plan p JOIN active a ON a.citation_id=p.citation_id LIMIT 1",
         "Active tutorial revisions cannot be replaced by aliases"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p LEFT JOIN libraries l ON l.id=p.owner_library_id
          LEFT JOIN libraries t ON t.id=p.target_library_id WHERE l.id IS NULL OR t.id IS NULL LIMIT 1""",
         "Tutorial alias source/target owner is no longer registered"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p LEFT JOIN revisions r
          ON r.id=p.target_id AND r.library_id=p.target_library_id
          LEFT JOIN active a ON a.library_id=r.library_id AND a.object_id=r.object_id AND a.citation_id=r.id
          WHERE r.id IS NULL OR a.citation_id IS NULL OR p.citation_id=p.target_id LIMIT 1""",
         "Tutorial alias targets must be active canonical citations"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p LEFT JOIN revisions r
          ON r.id=p.target_id AND r.library_id=p.target_library_id WHERE r.id IS NULL LIMIT 1""",
         "Tutorial alias target canonical payload is missing"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p WHERE NOT EXISTS(
          SELECT 1 FROM tutorial_document_sources d WHERE d.citation_id=p.target_id
          AND d.owner_library_id=p.target_library_id AND d.source_sha256=p.source_sha256) LIMIT 1""",
         "Tutorial alias target does not match the canonical document hash"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p JOIN revisions r ON r.id=p.citation_id
          WHERE r.library_id<>p.owner_library_id OR r.object_id<>p.object_id LIMIT 1""",
         "Retired tutorial payload ownership changed"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p JOIN tutorial_aliases a ON a.citation_id=p.citation_id
          WHERE a.owner_library_id<>p.owner_library_id OR a.target_id<>p.target_id
          OR a.target_library_id<>p.target_library_id OR a.reason<>p.reason LIMIT 1""",
         "An existing tutorial alias has a different canonical mapping"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p WHERE NOT EXISTS(SELECT 1 FROM revisions r WHERE r.id=p.citation_id)
          AND NOT EXISTS(SELECT 1 FROM tutorial_aliases a WHERE a.citation_id=p.citation_id) LIMIT 1""",
         "Retired tutorial payload/alias is missing"),
        ("""SELECT 1 FROM temp.tutorial_alias_plan p JOIN tutorial_aliases a ON a.citation_id=p.target_id LIMIT 1""",
         "Tutorial alias chains are not permitted"),
    )
    for query, message in invalid_queries:
        check_cancelled()
        if db.execute(query).fetchone():
            raise ValueError(message)
    payloads = db.execute("SELECT COUNT(*) FROM revisions r JOIN temp.tutorial_alias_plan p ON p.citation_id=r.id").fetchone()[0]
    db.execute("""INSERT OR IGNORE INTO tutorial_aliases
      SELECT citation_id,owner_library_id,target_id,target_library_id,reason FROM temp.tutorial_alias_plan""")
    members = db.execute("DELETE FROM case_members WHERE citation_id IN (SELECT citation_id FROM temp.tutorial_alias_plan)").rowcount
    associations = db.execute("DELETE FROM tutorial_associations WHERE source_id IN (SELECT citation_id FROM temp.tutorial_alias_plan)").rowcount
    retired_metadata = db.execute("DELETE FROM tutorial_retired_sources WHERE citation_id IN (SELECT citation_id FROM temp.tutorial_alias_plan)").rowcount
    # The compressed view's INSTEAD OF trigger can materialize OLD.payload for
    # every row. Delete the verified physical representation directly so a
    # body purge never hydrates the text being removed.
    physical = "_revision_payloads" if revision_storage(db) == "catalog-revisions/zlib-v1" else "revisions"
    db.execute("DELETE FROM " + physical + " WHERE id IN (SELECT citation_id FROM temp.tutorial_alias_plan)")
    if db.execute("SELECT 1 FROM revisions WHERE id IN (SELECT citation_id FROM temp.tutorial_alias_plan) LIMIT 1").fetchone():
        raise ValueError("Retired tutorial payload removal did not complete")
    check_cancelled()
    return {"aliases": expected, "payloads_removed": payloads, "case_members_removed": members,
            "retirement_associations_removed": associations, "retired_metadata_removed": retired_metadata}
