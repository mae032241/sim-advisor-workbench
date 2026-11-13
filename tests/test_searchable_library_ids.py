"""Lifecycle scope checks read metadata directly and remain fail-closed."""
from contextlib import closing
import json
import sqlite3

import pytest

from advisor.library_store import LibraryStore, encoded


def catalog(tmp_path):
    store = LibraryStore(tmp_path / "libraries")
    sources = tmp_path / "sources"
    sources.mkdir()
    for identity in ("a", "b", "closed"):
        store.register(identity, "user-models", [{"id": "main", "path": str(sources)}], library_id=identity)
        if identity != "closed":
            store.set_enabled(identity, True)
    return store


def update(store, identity, change):
    with closing(sqlite3.connect(store.db_path)) as db, db:
        body = json.loads(db.execute("SELECT body FROM libraries WHERE id=?", (identity,)).fetchone()[0])
        change(body)
        db.execute("UPDATE libraries SET body=? WHERE id=?", (encoded(body), identity))


def test_scope_reads_only_library_metadata_without_counts_payloads_or_path_resolution(tmp_path, monkeypatch):
    store = catalog(tmp_path)
    connect = store.connect
    statements = []

    def authorize(action, table, *_args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in {"active", "revisions", "_revision_payloads"} else sqlite3.SQLITE_OK

    def guarded_connect():
        db = connect()
        db.set_authorizer(authorize)
        db.set_trace_callback(statements.append)
        return db

    def forbidden(*_args, **_kwargs):
        pytest.fail("A scope check must not list objects or resolve source paths")

    monkeypatch.setattr(store, "connect", guarded_connect)
    monkeypatch.setattr(store, "libraries", forbidden)
    monkeypatch.setattr(store, "objects", forbidden)
    monkeypatch.setattr(store.paths, "resolve", forbidden)
    monkeypatch.setattr("pathlib.Path.resolve", forbidden)
    assert store.searchable_library_ids() == ["a", "b"]
    assert store.searchable_library_ids(["b", "closed", "a", "b", "unknown"]) == ["b", "a"]
    assert store.searchable_library_ids([]) == []
    assert statements == ["SELECT id,body FROM libraries"] * 3


def test_scope_rechecks_committed_flags_from_independent_connection_every_time(tmp_path):
    store = catalog(tmp_path)
    assert store.searchable_library_ids() == ["a", "b"]
    update(store, "a", lambda body: body.update(enabled=False))
    assert store.searchable_library_ids() == ["b"]
    update(store, "b", lambda body: body.update(deleted_at="2026-11-12T00:00:00Z"))
    assert store.searchable_library_ids() == []
    update(store, "a", lambda body: body.update(enabled=True))
    assert store.searchable_library_ids(["b", "a"]) == ["a"]
    update(store, "b", lambda body: body.update(deleted_at=None))
    assert store.searchable_library_ids(["b", "a"]) == ["b", "a"]


def test_legacy_defaults_and_offline_locators_remain_valid_without_physical_checks(tmp_path):
    store = catalog(tmp_path)

    def legacy(body):
        body.pop("enabled")
        body.pop("deleted_at")
        body["roots"] = [{"path": str(tmp_path / "offline" / "missing"),
                          "previous_paths": ["old-root", str(tmp_path / "previous-offline")]}, {"path": ""}]

    update(store, "a", legacy)
    assert store.searchable_library_ids(["a"]) == ["a"]


@pytest.mark.parametrize("requested", [None, [], ["a"]])
@pytest.mark.parametrize("patch", [{"id": "wrong"}, {"enabled": 1}, {"enabled": "false"},
                                   {"deleted_at": ""}, {"deleted_at": False}])
def test_invalid_unrequested_or_disabled_catalog_rows_are_not_ignored(tmp_path, requested, patch):
    store = catalog(tmp_path)
    update(store, "closed", lambda body: body.update(patch))
    with pytest.raises(ValueError, match="library"):
        store.searchable_library_ids(requested)


@pytest.mark.parametrize("raw", ["{broken", "null", "[]", '"not-a-record"'])
def test_malformed_library_body_is_rejected(tmp_path, raw):
    store = catalog(tmp_path)
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("UPDATE libraries SET body=? WHERE id='closed'", (raw,))
    with pytest.raises(ValueError):
        store.searchable_library_ids(["a"])


@pytest.mark.parametrize("roots", [None, {}, "invalid", [None], [{}], [{"path": None}],
                                  [{"path": "../outside"}], [{"path": "C:relative"}],
                                  [{"path": "bad\x00path"}], [{"path": "ok", "previous_paths": "bad"}],
                                  [{"path": "ok", "previous_paths": [None]}],
                                  [{"path": "ok", "previous_paths": [""]}],
                                  [{"path": "ok", "previous_paths": ["../outside"]}]])
def test_invalid_root_structures_and_lexical_boundaries_still_fail_closed(tmp_path, roots):
    store = catalog(tmp_path)
    update(store, "closed", lambda body: body.update(roots=roots))
    with pytest.raises(ValueError):
        store.searchable_library_ids(["a"])


def test_missing_roots_remains_invalid_catalog(tmp_path):
    store = catalog(tmp_path)
    update(store, "closed", lambda body: body.pop("roots"))
    with pytest.raises(ValueError, match="roots"):
        store.searchable_library_ids()


def test_previous_workspace_anchor_boundary_is_checked_lexically_in_original_order(tmp_path):
    store = catalog(tmp_path)
    old = tmp_path.parent / "previous-workspace"
    store.paths.previous_roots = (old, old / "nested")
    update(store, "closed", lambda body: body.update(roots=[{"path": str(old / "nested" / ".." / "outside")}]))
    with pytest.raises(ValueError, match="escapes"):
        store.searchable_library_ids(["a"])


@pytest.mark.parametrize("requested", ["a", {"a"}, [1], ("a",), False])
def test_requested_scope_input_contract_is_unchanged(tmp_path, requested):
    store = catalog(tmp_path)
    with pytest.raises(ValueError, match="list of strings"):
        store.searchable_library_ids(requested)
