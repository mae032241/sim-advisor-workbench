"""Tutorial audits compare source/index semantics across object storage codecs."""
from contextlib import closing
import sqlite3

import pytest

from advisor import object_index
from scripts.verify_tutorial_normalization import _native_retained, audit
from test_object_index import historical


def retained(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute("ATTACH DATABASE ':memory:' AS audit_plan")
        db.execute("CREATE TABLE audit_plan.retirements(id TEXT PRIMARY KEY)")
        return _native_retained(db, "models")


def test_v1_search_cache_removal_preserves_audited_locators_and_statistics(tmp_path):
    path = tmp_path / "objects.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        historical(db, metadata="object_metadata")
    before = retained(path)
    object_index.slim_object_index(path, dry_run=False, offline=True)
    after = retained(path)
    assert before == after and after["count"] == 1
    assert "representation" in after["samples"][0]
    assert "text" not in after["samples"][0]
    with closing(sqlite3.connect(path)) as db:
        assert "search_input" not in {row[1] for row in db.execute("PRAGMA table_info(objects)")}


def test_old_physical_cache_baseline_requests_a_new_baseline_without_claiming_content_loss():
    with pytest.raises(ValueError, match="older physical-cache format"):
        audit("not-opened", "not-opened", baseline={"schema": "tutorial-normalization-audit/v1"})
