"""Synthetic case metadata + native TOCs; no MPH, QMD, embeddings or models."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from advisor.case_manual_links import CaseManualLinks, normalized_name
from advisor.evaluation_sets import HoldoutPolicy
from advisor.html_tree_store import HtmlTreeRepository
from advisor.html_tree_tools import HtmlTreeTools
from advisor.library_store import LibraryStore, encoded

PACKAGE = "com.comsol.help.synthetic"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("SIM_ADVISOR_HTML_TREE_INDEX", raising=False)
    root = tmp_path / "original-manuals"
    package = root / PACKAGE
    package.mkdir(parents=True)
    (package / "toc.xml").write_text('<toc label="Synthetic manual">'
        '<topic label="The Test Physics Alpha Interface" href="alpha.html">'
        '<topic label="Wall" href="wall-alpha.html"/>'
        '<topic label="Sound Hard Boundary" href="sound-hard.html"/>'
        '</topic><topic label="The Test Physics Beta Interface" href="beta.html">'
        '<topic label="Wall" href="wall-beta.html"/></topic></toc>', encoding="utf-8")
    for name in ("alpha", "beta", "wall-alpha", "wall-beta", "sound-hard"):
        (package / (name + ".html")).write_text('<body><h1>' + name + '</h1>Original manual body.</body>', encoding="utf-8")
    store = LibraryStore(tmp_path / "catalog")
    store.register("Manuals", "user-manuals", [{"id": "help", "path": str(root)}], library_id="manuals")
    models = tmp_path / "model-source-root"
    models.mkdir()
    store.register("Case metadata", "user-models", [{"id": "models", "path": str(models)}], library_id="cases")
    store.set_enabled_many(["manuals", "cases"], True)
    repo = HtmlTreeRepository(store)
    repo.build_library("manuals")
    case_id = "lo-synthetic-case-r1"
    source = {"id": case_id, "object_id": "synthetic-case", "library_id": "cases", "type": "model_case",
              "asset_key": "models:unopened-model.mph", "asset_hash": "a" * 64, "revision_id": "r1", "case_group": "synthetic-group"}
    with closing(store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (case_id, source["object_id"], "cases", encoded(source)))
    physics_a = {"node_path": "compact:node/1", "tag": "a", "type": "TestPhysicsAlpha", "label": "Native alpha"}
    physics_b = {"node_path": "compact:node/2", "tag": "b", "type": "TestPhysicsBeta", "label": "Native beta"}

    def node(path, type, physics=None, *, kind="Feature", doc_refs=None):
        return {"node_path": path, "parent_path": physics["node_path"] if physics else None,
                "type": type, "label": type, "kind": kind, "nearest_physics": deepcopy(physics), "doc_refs": doc_refs or [],
                "source_refs": [{"source_id": case_id, "node_path": path, "source_mode": mode} for mode in ("saved", "api")],
                "read_requests": [{"case_id": case_id, "node_path": path, "source": mode} for mode in ("saved", "api")]}

    nodes = [node(physics_a["node_path"], physics_a["type"], physics_a, kind="PhysicsField"),
             node(physics_b["node_path"], physics_b["type"], physics_b, kind="PhysicsField"),
             node("compact:node/3", "Wall", physics_a), node("compact:node/4", "Wall", physics_b),
             node("compact:node/5", "SoundHard", physics_a), node("compact:node/6", "NoSuchFeature", physics_a),
             node("compact:node/7", "Wall")]

    class CaseMetadata:
        list_calls = 0
        parse_calls = 0

        def case(self, identity):
            assert identity == case_id
            return {"case_id": case_id, **{key: source[key] for key in
                ("library_id", "asset_key", "asset_hash", "revision_id", "case_group")}, "generation": "case-tree-v1"}

        def list_nodes(self, identity, offset=0, limit=100):
            self.list_calls += 1
            return {"items": deepcopy(nodes[offset:offset + limit]), "next_offset": offset + limit if offset + limit < len(nodes) else None}

    case_repo = CaseMetadata()
    tools = HtmlTreeTools(store)
    links = CaseManualLinks(store, case_repository=case_repo, html_tools=tools)
    return type("Fixture", (), dict(root=root, package=package, store=store, manual_repo=repo, case_id=case_id,
        source=source, nodes=nodes, case_repo=case_repo, html=tools, links=links, make_node=staticmethod(node), physics_a=physics_a))()


def forward(f, path, **kwargs):
    return f.links.links(f.case_id, node_path=path, **kwargs)


def test_generic_normalization_has_no_domain_alias_table():
    assert normalized_name("The Thermo Electric Field Interface") == normalized_name("ThermoElectricField")
    assert normalized_name("The Test Physics，Alpha Interface (tpa)") == "test physics alpha"


def test_same_wall_title_maps_only_inside_its_recorded_physics_interface(fixture):
    summary = fixture.links.build(fixture.case_id)
    alpha = forward(fixture, "compact:node/3")["items"]
    beta = forward(fixture, "compact:node/4")["items"]
    assert summary["node_count"] == 7
    assert len(alpha) == len(beta) == 1
    assert alpha[0]["mapping_state"] == beta[0]["mapping_state"] == "matched"
    assert alpha[0]["manual"]["doc_path"].endswith("wall-alpha.html")
    assert beta[0]["manual"]["doc_path"].endswith("wall-beta.html")
    assert alpha[0]["manual"]["node_id"] != beta[0]["manual"]["node_id"]
    assert alpha[0]["verified"] is False and alpha[0]["is_read_evidence"] is False
    assert alpha[0]["manual_read"]["arguments"]["node_id"] == alpha[0]["manual"]["node_id"]
    assert {entry["arguments"]["source"] for entry in alpha[0]["configuration_reads"]} == {"saved", "api"}
    assert alpha[0]["source_version"]["revision_id"] == "r1"


def test_missing_parent_or_missing_feature_is_unresolved_and_partial_match_is_candidate(fixture):
    partial = forward(fixture, "compact:node/5")["items"][0]
    assert partial["mapping_state"] == "candidate" and partial["confidence"] == "partial_scoped_name"
    for path in ("compact:node/6", "compact:node/7"):
        item = forward(fixture, path)["items"][0]
        assert item["mapping_state"] == "unresolved" and "manual" not in item


def test_existing_doc_reference_has_priority_but_scope_conflict_is_not_verified(fixture):
    wall = fixture.nodes[2]
    wall.update(type="UntranslatedNativeType", label="不做领域翻译", doc_refs=[{"field": "href", "value": "/" + PACKAGE + "/wall-alpha.html"}])
    match = forward(fixture, wall["node_path"])["items"][0]
    assert match["mapping_state"] == "matched" and match["confidence"] == "explicit_reference"
    wall["doc_refs"] = ["https://doc.comsol.com/6.3/doc/" + PACKAGE + "/wall-beta.html"]
    # Case metadata changes without changing its source version need an explicit
    # relation refresh, just as an explicit metadata publication would do.
    fixture.links.build(fixture.case_id)
    mismatch = forward(fixture, wall["node_path"])["items"][0]
    assert mismatch["mapping_state"] == "candidate" and mismatch["confidence"] == "scope_conflict"
    assert mismatch["verified"] is False


def test_multiple_exact_manual_targets_stay_ambiguous_and_paginate_truthfully(fixture):
    toc = fixture.package / "toc.xml"
    original = toc.read_text(encoding="utf-8")
    duplicate = '<topic label="The Test Physics Alpha Interface" href="alpha-copy.html"><topic label="Wall" href="wall-copy.html"/></topic>'
    toc.write_text(original.replace('</toc>', duplicate + '</toc>'), encoding="utf-8")
    for name in ("alpha-copy", "wall-copy"):
        (fixture.package / (name + '.html')).write_text('<body>Alternative native document.</body>', encoding="utf-8")
    fixture.manual_repo.build_library("manuals")
    first = forward(fixture, "compact:node/3", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    second = forward(fixture, "compact:node/3", offset=first["next_offset"], limit=1)
    assert second["next_offset"] is None
    assert all(p["items"][0]["mapping_state"] == "ambiguous" for p in (first, second))
    assert first["items"][0]["manual"]["node_id"] != second["items"][0]["manual"]["node_id"]


def test_manual_generation_refreshes_relations_without_reparsing_case_or_retrieval(fixture):
    previous = forward(fixture, "compact:node/3")["items"][0]["manual"]["node_id"]
    calls = fixture.case_repo.list_calls
    (fixture.package / "wall-alpha.html").write_text('<body>New manual revision.</body>', encoding="utf-8")
    fixture.manual_repo.build_library("manuals")
    refreshed = forward(fixture, "compact:node/3")["items"][0]
    assert refreshed["manual"]["node_id"] != previous
    assert fixture.case_repo.list_calls > calls and fixture.case_repo.parse_calls == 0
    assert fixture.links.reverse(refreshed["manual"]["node_id"])["items"][0]["node_path"] == "compact:node/3"
    with pytest.raises(PermissionError):
        fixture.links.reverse(previous)


def test_original_manual_root_migration_keeps_relative_links_valid(fixture, tmp_path):
    previous = forward(fixture, "compact:node/3")["items"][0]["manual"]["node_id"]
    relocated = tmp_path / "relocated-original-manuals"
    shutil.move(str(fixture.root), relocated)
    fixture.store.rebind("manuals", "help", relocated)
    assert forward(fixture, "compact:node/3")["items"][0]["manual"]["node_id"] == previous
    assert fixture.links.reverse(previous)["returned"] == 1


@pytest.mark.parametrize("target", ["case_closed", "case_deleted", "case_heldout", "manual_closed", "manual_deleted", "manual_heldout"])
def test_both_directions_apply_current_lifecycle_and_holdout_before_paging(fixture, monkeypatch, target):
    manual = forward(fixture, "compact:node/3")["items"][0]["manual"]
    if target == "case_closed":
        fixture.store.set_enabled("cases", False)
    elif target == "case_deleted":
        fixture.store.delete_library("cases")
    elif target == "case_heldout":
        monkeypatch.setattr(fixture.store, "evaluation_policy", lambda: HoldoutPolicy(citation_ids=frozenset([fixture.case_id])))
    elif target == "manual_closed":
        fixture.store.set_enabled("manuals", False)
    elif target == "manual_deleted":
        fixture.store.delete_library("manuals")
    else:
        monkeypatch.setattr(fixture.store, "evaluation_policy", lambda: HoldoutPolicy(document_relpaths=frozenset([manual["doc_path"]])))
    if target.startswith("case"):
        with pytest.raises((PermissionError, FileNotFoundError)):
            forward(fixture, "compact:node/3")
        reverse = fixture.links.reverse(manual["node_id"], limit=1)
        assert reverse["total"] == reverse["returned"] == 0 and reverse["next_offset"] is None
    else:
        forward_result = forward(fixture, "compact:node/3")
        assert all("manual" not in row for row in forward_result["items"])
        with pytest.raises(PermissionError):
            fixture.links.reverse(manual["node_id"], limit=1)


def test_constructor_reads_only_metadata_and_storage_never_copies_bodies(fixture, monkeypatch):
    original_read = Path.read_bytes

    def read_bytes(path):
        assert path.suffix.lower() not in {".html", ".htm", ".mph"}, "relationship construction must not read source bodies"
        return original_read(path)

    with monkeypatch.context() as ctx:
        ctx.setattr(Path, "read_bytes", read_bytes)
        summary = fixture.links.build(fixture.case_id)
    assert summary["is_read_evidence"] is False
    with closing(sqlite3_connect(fixture.links.path)) as db:
        text = '\n'.join(row[0] for row in db.execute('SELECT body FROM links'))
    assert "Original manual body" not in text and "unopened-model.mph" not in text
    assert not list(fixture.links.directory.rglob('*.html'))


def sqlite3_connect(path):
    import sqlite3
    return sqlite3.connect(path)


def test_public_pages_obey_byte_budget_and_do_not_drop_relation_rows(fixture):
    collected, offset = [], 0
    while True:
        result = fixture.links.links(fixture.case_id, offset=offset, limit=8)
        assert len(json.dumps(result, ensure_ascii=False).encode('utf-8')) <= 7000
        collected.extend(row['node_path'] for row in result['items'])
        if result['next_offset'] is None:
            break
        assert result['next_offset'] > offset
        offset = result['next_offset']
    assert len(collected) == len(fixture.nodes)


def test_customized_labels_cannot_override_the_exact_native_physics_or_feature_type(fixture):
    fixture.nodes[0]["label"] = "Wall"
    fixture.nodes[2]["label"] = "Sound Hard Boundary"
    matched = forward(fixture, "compact:node/3")["items"][0]
    assert matched["mapping_state"] == "matched"
    assert matched["manual"]["doc_path"].endswith("wall-alpha.html")


def test_relative_existing_doc_reference_is_resolved_only_within_the_parent_physics(fixture):
    fixture.nodes[2].update(type="OpaqueNativeType", label="Unknown label", doc_refs=[{"field": "href", "value": "wall-alpha.html"}])
    matched = forward(fixture, "compact:node/3")["items"][0]
    assert matched["mapping_state"] == "matched" and matched["confidence"] == "explicit_reference"
    assert matched["manual"]["doc_path"].endswith("wall-alpha.html")


def test_reverse_filters_heldout_case_before_offset_and_total(fixture, monkeypatch):
    second_id = "lo-zz-second-case-r1"
    second = dict(fixture.source, id=second_id, object_id="second-case", case_group="other-group")
    with closing(fixture.store.connect()) as db, db:
        db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (second_id, second["object_id"], "cases", encoded(second)))
    original_repo = fixture.case_repo
    class TwoCases:
        def case(self, identity):
            value = original_repo.case(fixture.case_id)
            return dict(value, case_id=identity, case_group=second['case_group']) if identity == second_id else value
        def list_nodes(self, identity, offset=0, limit=100):
            page = original_repo.list_nodes(fixture.case_id, offset=offset, limit=limit)
            if identity == second_id:
                for node in page['items']:
                    for ref in node['source_refs']:ref['source_id']=second_id
                    for request in node['read_requests']:request['case_id']=second_id
            return page
    links = CaseManualLinks(fixture.store, case_repository=TwoCases())
    links.build(fixture.case_id)
    links.build(second_id)
    manual = links.links(fixture.case_id, node_path='compact:node/3')['items'][0]['manual']['node_id']
    assert links.reverse(manual)['total'] == 2
    monkeypatch.setattr(fixture.store, 'evaluation_policy', lambda: HoldoutPolicy(citation_ids=frozenset([fixture.case_id])))
    page = links.reverse(manual, offset=0, limit=1)
    assert page['total'] == page['returned'] == 1 and page['next_offset'] is None
    assert page['items'][0]['case_id'] == second_id


def test_real_case_tree_repository_and_source_configuration_routes_integrate(fixture, tmp_path, monkeypatch):
    from advisor.case_tree import build_case_trees
    from test_compact_model import mph
    source = mph(tmp_path / "actual-synthetic" / "case.mph",
        '<Model><Physics tag="alpha" op="TestPhysicsAlpha" name="Customized label">'
        '<PhysicsFeature tag="wall" op="Wall" name="Wall"><propertyValue name="p:alpha" value="0.3"/>'
        '</PhysicsFeature></Physics></Model>')
    fixture.store.register("Actual synthetic case", "user-models", [{"id": "models", "path": str(source.parent)}], library_id="actual")
    fixture.store.set_enabled("actual", True)
    fixture.store.build("actual", paths=[source], model_format="compact", deep=False, modes=[])
    case = next(item for item in fixture.store.objects(["actual"]) if item["type"] == "model_case")
    build_case_trees(fixture.store, [case["id"]])
    actual = CaseManualLinks(fixture.store)
    original_read_bytes = Path.read_bytes
    def read_bytes(path):
        assert path.suffix.lower() != '.mph', "manual association may not reopen the original MPH"
        return original_read_bytes(path)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    result = actual.build(case["id"])
    assert result["counts"]["matched"] >= 2
    all_rows, offset = [], 0
    while True:
        page = actual.links(case["id"], offset=offset)
        all_rows.extend(page['items'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    wall = next(row for row in all_rows if row['native_type'] == 'Wall')
    assert wall['manual']['doc_path'].endswith('wall-alpha.html')
    assert wall['configuration_reads'][0]['arguments'] == {'case_id': case['id'], 'node_path': wall['node_path'], 'source': 'saved'}
    assert wall['source_refs'] and wall['source_version']['asset_hash'] == case['asset_hash']


def test_repeated_service_queries_reuse_only_the_manual_metadata_snapshot(fixture, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from advisor.planning_service import PlanningService
    calls = 0
    original = fixture.html.navigation_metadata
    def snapshot():
        nonlocal calls
        calls += 1
        return original()
    monkeypatch.setattr(fixture.html, 'navigation_metadata', snapshot)
    service = PlanningService(tmp_path / 'workspace', SimpleNamespace(), SimpleNamespace(store=fixture.store))
    service.case_manual_links = fixture.links
    request = {'view': 'manual_links', 'case_id': fixture.case_id, 'node_path': 'compact:node/3'}
    assert service.call_public('get_case_navigation', request)['returned'] == 1
    assert service.call_public('get_case_navigation', request)['returned'] == 1
    assert calls == 1
    fixture.store.set_enabled('manuals', False)
    assert all('manual' not in row for row in service.call_public('get_case_navigation', request)['items'])


def test_one_page_caches_case_source_and_manual_authority_within_the_request(fixture, monkeypatch):
    fixture.nodes.extend(fixture.make_node('compact:node/' + str(i), 'Wall', fixture.physics_a) for i in range(8, 128))
    fixture.links.build(fixture.case_id)
    manual_id = forward(fixture, 'compact:node/3')['items'][0]['manual']['node_id']
    counts = {'case': 0, 'source': 0, 'manual': 0}
    original_case, original_source, original_manual = fixture.case_repo.case, fixture.store.source_available, fixture.html.get_document
    def case(identity):
        counts['case'] += 1
        return original_case(identity)
    def source(identity):
        counts['source'] += 1
        return original_source(identity)
    def manual(identity, **kwargs):
        counts['manual'] += 1
        return original_manual(identity, **kwargs)
    monkeypatch.setattr(fixture.case_repo, 'case', case)
    monkeypatch.setattr(fixture.store, 'source_available', source)
    monkeypatch.setattr(fixture.html, 'get_document', manual)
    page = fixture.links.links(fixture.case_id, limit=8)
    assert page['total'] == len(fixture.nodes) > 100
    assert counts['case'] <= 3 and counts['source'] <= 6 and counts['manual'] <= 8
    counts.update(case=0, source=0, manual=0)
    reverse = fixture.links.reverse(manual_id, limit=8)
    assert reverse['total'] > 100
    assert counts['case'] <= 3 and counts['source'] <= 6 and counts['manual'] <= 3
    # Permission results expire at request completion, even while TOC metadata
    # remains cached. The next request sees the current holdout policy.
    monkeypatch.setattr(fixture.store, 'evaluation_policy', lambda: HoldoutPolicy(citation_ids=frozenset([fixture.case_id])))
    with pytest.raises(PermissionError):
        fixture.links.links(fixture.case_id)


def test_selected_page_rechecks_live_manual_permission_after_counting(fixture, monkeypatch):
    fixture.links.build(fixture.case_id)
    original_page = fixture.links._page
    def page(*args, **kwargs):
        result = original_page(*args, **kwargs)
        fixture.store.set_enabled('manuals', False)
        return result
    monkeypatch.setattr(fixture.links, '_page', page)
    with pytest.raises(PermissionError, match='authority changed'):
        forward(fixture, 'compact:node/3')


def test_many_distinct_sources_and_manual_nodes_share_request_authority(fixture, monkeypatch):
    # Repeating one source/node would miss the production regression: hundreds
    # of distinct citations previously repeated policy/SQLite/library scans.
    topics, identities = [], []
    with closing(fixture.store.connect()) as db, db:
        for index in range(110):
            identity = 'lo-distinct-' + str(index)
            source = dict(fixture.source, id=identity, object_id='distinct-' + str(index), type='model_node')
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (identity, source['object_id'], 'cases', encoded(source)))
            node = fixture.make_node('compact:node/distinct-' + str(index), 'ExactFeature' + str(index), fixture.physics_a)
            node['source_refs'] = [dict(ref, source_id=identity) for ref in node['source_refs']]
            fixture.nodes.append(node)
            identities.append(identity)
            name = 'distinct-' + str(index) + '.html'
            topics.append('<topic label="Exact Feature ' + str(index) + '" href="' + name + '"/>')
            (fixture.package / name).write_text('<body>Original body.</body>', encoding='utf-8')
    toc = fixture.package / 'toc.xml'
    toc.write_text(toc.read_text(encoding='utf-8').replace('</topic><topic label="The Test Physics Beta',
        ''.join(topics) + '</topic><topic label="The Test Physics Beta'), encoding='utf-8')
    fixture.manual_repo.build_library('manuals')
    fixture.links.build(fixture.case_id)
    counts = {'policy': 0, 'scope': 0, 'get': 0, 'manual': 0}
    for name, key in [('evaluation_policy', 'policy'), ('searchable_library_ids', 'scope'), ('get', 'get')]:
        original = getattr(fixture.store, name)
        def wrapped(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(fixture.store, name, wrapped)
    original_manual = fixture.html.get_document
    def manual(*args, **kwargs):
        counts['manual'] += 1
        return original_manual(*args, **kwargs)
    monkeypatch.setattr(fixture.html, 'get_document', manual)
    page = fixture.links.links(fixture.case_id)
    assert page['total'] == len(fixture.nodes) == 117
    assert counts['policy'] < 30 and counts['scope'] < 30
    assert counts['get'] <= 3 and counts['manual'] == 0
    # New requests must see changed exact-source exclusions before counting.
    policy = fixture.store.evaluation_policy()
    monkeypatch.setattr(fixture.store, 'evaluation_policy', lambda: HoldoutPolicy(
        citation_ids=policy.citation_ids | frozenset(identities)))
    assert fixture.links.links(fixture.case_id)['total'] == 7


def test_batch_source_authority_preserves_alias_retirement_and_object_exclusions(fixture, monkeypatch):
    from advisor.tutorial_aliases import ensure_alias_schema
    from advisor.tutorial_migration import ensure_tutorial_association_schema
    store = fixture.store
    with closing(store.connect()) as db, db:
        ensure_alias_schema(db)
        ensure_tutorial_association_schema(db)
        for identity in ('target', 'retired', 'orphan-retired', 'object-blocked', 'canonical-blocked'):
            source = dict(fixture.source, id=identity, object_id='object-' + identity)
            if identity == 'canonical-blocked':
                source['data'] = {'canonical_tutorial': {'source_sha256': 'heldout-digest'}}
            db.execute('INSERT INTO revisions VALUES(?,?,?,?)', (identity, source['object_id'], 'cases', encoded(source)))
        db.execute('INSERT INTO tutorial_aliases VALUES(?,?,?,?,?)', ('alias', 'manuals', 'target', 'cases', 'canonical'))
        db.executemany('INSERT INTO tutorial_retired_sources VALUES(?,?,?,?)',
            [(identity, 'cases', 'model_tutorial_copy', 'digest') for identity in ('retired', 'orphan-retired')])
        db.execute('INSERT INTO tutorial_associations VALUES(?,?,?,?,?,?,?)',
            ('retired', 'cases', 'target', 'manuals', 'canonical_tutorial', 'digest', 'synthetic'))
    policy = HoldoutPolicy(object_ids=frozenset(['object-object-blocked']), source_hashes=frozenset(['heldout-digest']))
    monkeypatch.setattr(store, 'evaluation_policy', lambda: policy)
    ids = {'alias', 'target', 'retired', 'orphan-retired', 'object-blocked', 'canonical-blocked', 'absent'}
    expected = {}
    for identity in ids:
        source = store.get(identity)
        expected[identity] = bool(source and store.source_available(identity) and not policy.blocks_object(source))
    assert fixture.links._source_permissions(ids, policy, set(store.searchable_library_ids())) == expected
    assert expected == {'alias': True, 'target': True, 'retired': True, 'orphan-retired': False,
                        'object-blocked': False, 'canonical-blocked': False, 'absent': False}
    store.set_enabled('manuals', False)
    actual = fixture.links._source_permissions(ids, policy, set(store.searchable_library_ids()))
    assert actual['target'] is True and actual['alias'] is actual['retired'] is False


def test_batch_manual_authority_rechecks_original_sha_on_each_request(fixture):
    fixture.links.build(fixture.case_id)
    assert forward(fixture, 'compact:node/3')['total'] == 1
    (fixture.package / 'wall-alpha.html').write_text('<body>Changed original.</body>', encoding='utf-8')
    page = forward(fixture, 'compact:node/3')
    assert page['total'] == page['returned'] == 0 and page['next_offset'] is None


def test_native_read_requests_are_not_lost_when_retrieval_reference_modes_are_sparse(fixture):
    # CaseTree has native read routes even for nodes/modes with no QMD chunk;
    # retrieval references must not become the configuration's completeness.
    fixture.nodes[2]['source_refs'] = [ref for ref in fixture.nodes[2]['source_refs'] if ref['source_mode'] == 'saved']
    item = forward(fixture, 'compact:node/3')['items'][0]
    assert {entry['arguments']['source'] for entry in item['configuration_reads']} == {'saved', 'api'}


def test_explicit_doc_reference_without_a_physics_scope_is_only_a_candidate(fixture):
    fixture.nodes[6]['doc_refs'] = [{'field': 'href', 'value': '/' + PACKAGE + '/wall-alpha.html'}]
    item = forward(fixture, 'compact:node/7')['items'][0]
    assert item['mapping_state'] == 'candidate' and item['confidence'] == 'explicit_unscoped_reference'
    assert item['verified'] is False


def structural_manual(f):
    toc = f.package / 'toc.xml'
    original = toc.read_text(encoding='utf-8')
    topics = '<topic label="Meshing" href="mesh.html"><topic label="Size" href="mesh-size.html"/>' \
        '<topic label="Free Tetrahedral" href="mesh-free.html"/></topic>' \
        '<topic label="Geometry" href="geometry.html"><topic label="Size" href="geom-size.html"/>' \
        '<topic label="Block" href="geom-block.html"/></topic>' \
        '<topic label="Solvers" href="solver.html"><topic label="Direct" href="direct.html"/></topic>' \
        '<topic label="Studies" href="study.html"><topic label="Time Dependent" href="time.html"/></topic>'
    toc.write_text(original.replace('</toc>', topics + '</toc>'), encoding='utf-8')
    for name in ('mesh', 'mesh-size', 'mesh-free', 'geometry', 'geom-size', 'geom-block', 'solver', 'direct', 'study', 'time'):
        (f.package / (name + '.html')).write_text('<body>Synthetic native structural manual.</body>', encoding='utf-8')
    f.manual_repo.build_library('manuals')


def test_structural_stages_route_mesh_geometry_study_solver_only_as_candidates(fixture):
    structural_manual(fixture)
    examples = [(130, 'MeshSequence', 'MeshSequence', 'mesh', None),
                (131, 'Size', 'MeshFeature', 'mesh', 130), (132, 'FreeTet', 'MeshFeature', 'mesh', 130),
                (140, 'Geometry', 'GeomSequence', 'geometry', None), (141, 'Block', 'GeomFeature', 'geometry', 140),
                (150, 'SolverSequence', 'SolverSequence', 'solver', None), (151, 'Direct', 'SolverFeature', 'solver', 150),
                (160, 'StudySequence', 'StudySequence', 'study', None), (161, 'TimeDependent', 'StudyFeature', 'study', 160)]
    for identity, typ, kind, stage, parent in examples:
        node = fixture.make_node('compact:node/' + str(identity), typ, kind=kind)
        node.update(stage=stage, ancestor_paths=['compact:node/' + str(parent)] if parent else [])
        fixture.nodes.append(node)
    summary = fixture.links.build(fixture.case_id)
    assert summary['contexts']['physics_subtree']['node_count'] == 6
    assert summary['contexts']['structural']['node_count'] == 9
    assert summary['contexts']['other']['node_count'] == 1
    assert summary['contexts']['structural']['counts']['matched'] == 0
    for path, expected in [(131, 'mesh-size.html'), (132, 'mesh-free.html'), (141, 'geom-block.html'), (151, 'direct.html'), (161, 'time.html')]:
        rows = forward(fixture, 'compact:node/' + str(path))['items']
        assert rows and all(row['mapping_state'] in {'candidate', 'ambiguous'} for row in rows)
        assert {row['manual']['doc_path'].rsplit('/', 1)[-1] for row in rows} == {expected}
        assert all(row['verified'] is False for row in rows)


def test_unknown_physics_uses_only_parent_candidates_without_cross_interface_wall_matching(fixture):
    fixture.nodes[0]['type'] = 'TestPhysicsAlphaExtra'
    fixture.nodes[0]['label'] = 'Untranslated parent'
    summary = fixture.links.build(fixture.case_id)
    row = forward(fixture, 'compact:node/3')['items'][0]
    assert row['mapping_state'] == 'candidate' and row['target_role'] == 'parent_physics_candidate'
    assert row['manual']['title'] == 'The Test Physics Alpha Interface'
    assert not row['manual']['doc_path'].endswith('wall-alpha.html')
    assert summary['physics_scope_counts']['candidate_parent_scope'] > 0


def test_uninformative_structural_type_stays_unresolved_when_no_chapter_is_reliable(fixture):
    node = fixture.make_node('compact:node/130', 'OpaqueObject', kind='OpaqueKind')
    node['stage'] = 'other'
    fixture.nodes.append(node)
    row = forward(fixture, 'compact:node/130')['items'][0]
    assert row['mapping_state'] == 'unresolved' and 'manual' not in row
