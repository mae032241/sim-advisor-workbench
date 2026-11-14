"""Recheck completed constructor deliveries without changing or regenerating cards.

Preserves all original artifacts, copies only unchanged accepted card packs, and
freezes a separate source-gated catalog. No task, answer, score or model is read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from advisor.route_cards import RouteCardStore
from advisor.tool_delivery_audit import audit_rollout, locate_rollout, response_sha256
from scripts.build_case_methods import create_local_app, library_flags, now, write_json


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--library-root', type=Path, required=True)
    args = parser.parse_args(argv)
    source, output = args.build_root.resolve(), args.output.resolve()
    manifest, closed = read(source / 'manifest.json'), read(source / 'cards-manifest.json')
    expected = set(manifest['case_ids'])
    if len(expected) != len(manifest['case_ids']) or not expected:
        raise ValueError('Unique original constructor identities are required')
    for item in closed['artifacts']:
        path = (source / item['path']).resolve()
        if not path.is_relative_to(source) or sha(path) != item['sha256']:
            raise ValueError('An original constructor artifact changed')
    results = [read(path) for path in sorted((source / 'cases').glob('*/result.json'))]
    if len(results) != len(expected) or {r['case_id'] for r in results} != expected:
        raise ValueError('All original constructors must have terminated')
    accepted, packs = [], {}
    for directory in sorted((source / 'cases').iterdir()):
        result = read(directory / 'result.json')
        if (result['status'] not in {'completed', 'invalid_delivery'}
                or result.get('turn_status') != 'completed' or not result.get('card_submitted')
                or not result.get('card_count')):
            raise ValueError('The constructor did not naturally complete with saved cards')
        callbacks = [json.loads(line) for line in (directory / 'raw-audit.jsonl').read_text(encoding='utf-8').splitlines()]
        hashes = [response_sha256(row['result']) for row in callbacks]
        recorded = read(directory / 'source-manifest.json')['callback_deliveries']
        if hashes != [row['sha256'] for row in recorded]:
            raise ValueError('Callback ledger and stored raw deliveries differ')
        audit = audit_rollout(locate_rollout(result['thread_id']), result['thread_id'],
                              expected_response_sha256=hashes)
        if not audit['valid']:
            raise ValueError('Actual delivery still fails verification: ' + json.dumps(audit['issues']))
        submitted = read(directory / 'cards.json')['cards']
        if len(submitted) != result['card_count']:
            raise ValueError('Saved card count differs from constructor result')
        for item in submitted:
            path = source / 'route-method-cards' / (item['method_id'] + '.json')
            pack = read(path)
            if (pack['method_id'] != item['method_id'] or pack['card_sha256'] != item['card_sha256']
                    or response_sha256(pack['card']) != item['card_sha256']
                    or pack['card']['source_case_ids'] != [result['case_id']]):
                raise ValueError('A card changed or belongs to a different constructor')
            packs[path.name] = sha(path)
        accepted.append({'case_id': result['case_id'], 'original_status': result['status'],
                         'original_result_sha256': sha(directory / 'result.json'),
                         'card_count': result['card_count'], 'delivery_audit': audit})
    actual = {p.name for p in (source / 'route-method-cards').glob('method-*.json')}
    if set(packs) != actual:
        raise ValueError('Unaccounted card packs exist in the original store')
    output.mkdir(parents=True, exist_ok=False)
    target = output / 'route-method-cards'
    target.mkdir()
    for name in sorted(packs):
        shutil.copyfile(source / 'route-method-cards' / name, target / name)
        if sha(target / name) != packs[name]:
            raise ValueError('Copied card pack changed')
    app = None
    before = None
    try:
        app = create_local_app(output, args.library_root)
        before = library_flags(app)
        scope = manifest['scope_library_ids']
        additions = [identity for identity in scope if not before[identity]['enabled']]
        if additions:
            app.kb.store.set_enabled_many(additions, True)
        catalog = RouteCardStore(output, app.kb).freeze_catalog()
    finally:
        if app is not None:
            app.agents.close()
            if before is not None:
                current = library_flags(app)
                for enabled in (False, True):
                    changed = [key for key, row in before.items()
                               if row['enabled'] is enabled and current[key]['enabled'] != enabled]
                    if changed:
                        app.kb.store.set_enabled_many(changed, enabled)
                if library_flags(app) != before:
                    raise RuntimeError('Original library flags were not restored')
    record = {'schema': 'route-card-delivery-reverification/v1', 'created_at': now(),
              'original_build': str(source), 'original_manifest_sha256': sha(source / 'manifest.json'),
              'original_closed_manifest_sha256': sha(source / 'cards-manifest.json'),
              'auditor_sha256': sha(APP / 'advisor/tool_delivery_audit.py'),
              'constructor_rerun': False, 'card_contents_changed': False, 'private_inputs_read': False,
              'source_only': True, 'valid': True, 'catalog': catalog, 'pack_sha256': packs,
              'cases': accepted, 'library_scope_restored': True}
    write_json(output / 'verification.json', record, exclusive=True)
    print(json.dumps({'valid': True, 'source_cases': len(accepted), 'method_cards': len(packs),
                      'recoverable_syntax_errors': sum(len(r['delivery_audit'].get('execution_errors', []))
                                                       for r in accepted),
                      'card_contents_changed': False, 'catalog': catalog}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
