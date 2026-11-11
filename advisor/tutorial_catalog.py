"""Tutorial source identity and cross-library links, without model-side body copies."""
from contextlib import closing
import json
from pathlib import Path

from .case_metadata import object_feature_branch
from .tutorial_migration import ensure_tutorial_association_schema


def ensure_schema(db):
    ensure_tutorial_association_schema(db)
    db.execute('CREATE INDEX IF NOT EXISTS tutorial_document_sources_sha ON tutorial_document_sources(source_sha256,owner_library_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS tutorial_model_documents(
      source_id TEXT NOT NULL,owner_library_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,
      identity TEXT,PRIMARY KEY(source_id,source_sha256))''')
    db.execute('CREATE INDEX IF NOT EXISTS tutorial_model_documents_sha ON tutorial_model_documents(source_sha256)')


def register_objects(db, objects):
    ensure_schema(db)
    for obj in objects:
        data = obj.get('data') or {}
        canonical = data.get('canonical_tutorial')
        if isinstance(canonical, dict):
            digest = canonical['source_sha256']
            category = object_feature_branch(obj) or ('procedure' if obj['type'] == 'method_step' else 'context')
            evidence = obj.get('evidence') or [{}]
            anchor = str(evidence[0].get('locator') or 'document')
            db.execute('INSERT OR REPLACE INTO tutorial_document_sources VALUES(?,?,?,?,?,?)',
                (digest, data.get('tutorial_identity'), obj['library_id'], obj['id'], anchor, category))
        if obj['type'] == 'model_case':
            for ref in data.get('tutorial_refs', []):
                if ref.get('source_sha256'):
                    db.execute('INSERT OR REPLACE INTO tutorial_model_documents VALUES(?,?,?,?)',
                        (obj['id'], obj['library_id'], ref['source_sha256'], ref.get('document_identifier') or ref.get('identity')))


def refresh_links(db):
    ensure_schema(db)
    # Historical payloads remain immutable. Migration edges seed the document
    # identity for existing model cases whose old payload has no tutorial_refs.
    db.execute('''INSERT OR IGNORE INTO tutorial_model_documents
      SELECT DISTINCT a.source_id,a.source_library_id,a.source_sha256,d.identity
      FROM tutorial_associations a JOIN case_members c ON c.citation_id=a.source_id
      JOIN tutorial_document_sources d ON d.citation_id=a.target_id
      WHERE c.local_id='model-case' ''')
    db.execute('''INSERT OR IGNORE INTO tutorial_associations
      SELECT m.source_id,m.owner_library_id,min(d.citation_id),d.owner_library_id,
             'canonical_tutorial',m.source_sha256,'constructor-links/v1'
      FROM tutorial_model_documents m JOIN active ma ON ma.citation_id=m.source_id
      JOIN tutorial_document_sources d ON d.source_sha256=m.source_sha256
      JOIN active da ON da.citation_id=d.citation_id
      WHERE ma.library_id=m.owner_library_id AND da.library_id=d.owner_library_id
      AND NOT EXISTS(SELECT 1 FROM tutorial_associations old
                     WHERE old.source_id=m.source_id AND old.source_sha256=m.source_sha256
                     AND old.target_library_id=d.owner_library_id)
      GROUP BY m.source_id,m.owner_library_id,m.source_sha256,d.owner_library_id''')


def retired_targets(db, citation_id):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='tutorial_retired_sources'").fetchone()
    if not exists:
        return None
    row = db.execute('SELECT reason FROM tutorial_retired_sources WHERE citation_id=?', (citation_id,)).fetchone()
    if not row or row[0] != 'model_tutorial_copy':
        return None
    return {r[0] for r in db.execute('SELECT DISTINCT target_library_id FROM tutorial_associations WHERE source_id=?', (citation_id,))}


def links(store, source, *, limit=8):
    owner = source['library_id']
    visible = set(store.searchable_library_ids())
    if owner not in visible:
        raise PermissionError('资料库已关闭或删除，不能新读取该来源。')
    identities = [source['id']]
    branch = object_feature_branch(source)
    with closing(store.connect()) as db:
        from .compact_api_overlay import pinned_base_authority
        base = pinned_base_authority(db, source)
        if base:
            identities.append(base['id'])
        if source.get('case_group') and source['type'] != 'model_case':
            row = db.execute("SELECT citation_id FROM case_members WHERE group_id=? AND local_id='model-case'", (source['case_group'],)).fetchone()
            if row:
                identities.append(row[0])
        placeholders = ','.join('?' for _ in identities)
        if not visible:
            return []
        owners = ','.join('?' for _ in visible)
        rows = db.execute(f'''WITH documents AS (
          SELECT source_sha256 FROM tutorial_model_documents
          WHERE source_id IN ({placeholders}) AND owner_library_id=?
          UNION SELECT source_sha256 FROM tutorial_associations
          WHERE source_id IN ({placeholders}) AND source_library_id=?
        ) SELECT DISTINCT r.id,r.payload,d.category,
          EXISTS(SELECT 1 FROM active ac WHERE ac.citation_id=r.id) AS current
          FROM documents t JOIN tutorial_document_sources d ON d.source_sha256=t.source_sha256
          JOIN revisions r ON r.id=d.citation_id AND r.library_id=d.owner_library_id
          JOIN active a ON a.citation_id=r.id AND a.library_id=r.library_id
          WHERE r.library_id IN ({owners})
          ORDER BY current DESC,CASE WHEN d.category=? THEN 0 ELSE 1 END,
                   json_extract(r.payload,'$.data.tutorial_order'),r.id LIMIT ?''',
          [*identities, owner, *identities, owner, *visible, branch, max(1, min(40, int(limit)))]).fetchall()
    output = []
    for identity, raw, category, current in rows:
        obj = json.loads(raw)
        output.append({'id': identity, 'library_id': obj['library_id'], 'title': obj['title'], 'type': obj['type'],
                       'relation': 'canonical_tutorial', 'status': 'reference_not_verified_model_agreement',
                       'feature_branch': category, 'archived': not bool(current)})
    visible_now = set(store.searchable_library_ids())
    if owner not in visible_now:
        raise PermissionError('资料库已关闭或删除，不能新读取该来源。')
    return [item for item in output if item['library_id'] in visible_now]
