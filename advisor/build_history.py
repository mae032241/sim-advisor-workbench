"""Keep job outcomes without another copy of per-object retrieval metadata."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .build_control import check_cancelled

_STATS = {"object_count", "indexed_objects", "unindexed_objects", "truncated_objects",
          "indexed_characters", "character_budget_per_object", "policy"}


def compact_index_diagnostic(value):
    if not isinstance(value, dict):
        return value
    result = {key: item for key, item in value.items() if key not in {"mapping", "representation", "last_build"}}
    representation = value.get("representation")
    if isinstance(representation, dict):
        stats = {key: item for key, item in representation.items() if key in _STATS}
        if stats:
            result["representation"] = stats
    if "last_build" in value:
        result["last_build"] = compact_index_diagnostic(value["last_build"])
    if "mapping" in value or isinstance(representation, dict) and ("objects" in representation or any(k not in _STATS for k in representation)):
        result["details_storage"] = "generation-native-object-lookup"
    return result


def compact_job_indexes(job):
    result = dict(job)
    if "indexes" in result:
        result["indexes"] = compact_index_diagnostic(result["indexes"])
    return result


def compact_build_history(path, *, on_progress=None):
    """Drop only bulk index diagnostics; retain job items/errors/progress.

    The offline caller holds the catalog/index maintenance lease. SQLite
    changes are transactional; cancellation rolls back the diagnostics edit.
    VACUUM is a separate final step, leaving valid data if it cannot complete.
    """
    path = Path(path).resolve(strict=True)
    removed = changed = 0
    with closing(sqlite3.connect(path, timeout=30)) as db:
        check_cancelled()
        with db:
            db.execute("BEGIN IMMEDIATE")
            for identity, raw in db.execute("SELECT id,body FROM builds ORDER BY rowid"):
                check_cancelled()
                original = json.loads(raw)
                result = compact_job_indexes(original)
                if result == original:
                    continue
                encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                removed += len(raw.encode('utf-8')) - len(encoded.encode('utf-8'))
                db.execute("UPDATE builds SET body=? WHERE id=?", (encoded, identity))
                changed += 1
                if on_progress:
                    on_progress({'phase': 'history_compaction', 'changed_jobs': changed, 'removed_json_bytes': removed})
            check_cancelled()
        check_cancelled()
        if changed:
            # Register catalog view functions before SQLite rebuilds its schema.
            from .catalog_storage import register_catalog_functions
            register_catalog_functions(db)
            db.execute("VACUUM")
    return {'changed_jobs': changed, 'removed_json_bytes': removed}
