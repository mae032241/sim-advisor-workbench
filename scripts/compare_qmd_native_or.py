"""Experimental native term expansion + one OR/BM25 query on QMD storage.

All overrides live in this process. Production Python/SDK files and indexes are
unchanged. Reuses the paired runner's scope restoration, isolation and checks.
This is explicitly NOT official QMD searchLex or query behavior.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sys

import compare_keyword_backends as paired

sys.path.insert(0, str(paired.APP))
from advisor import library_search as search

POLICY = 'experimental-native-terms-or-qmd-bm25-v1'
NEEDLE = '    const match=compile(req.query);\n    if(match===null)return [];'
REPLACEMENT = r'''    // EXPERIMENT ONLY: native terms, one OR query; no fusion/fallback.
    if(!Array.isArray(req.experimental_native_terms)||req.experimental_native_terms.length>60||req.experimental_native_terms.some(x=>typeof x!=='string'||!x))throw new Error('Missing experimental native terms');
    const {normalizeCjkForFTS}=await import(pathToFileURL(join(req.qmd_dir,'dist/store.js')).href);
    const match=req.experimental_native_terms.map(term=>'"'+normalizeCjkForFTS(term).replaceAll('"','""')+'"').join(' OR ');
    if(!match)return [];'''


@contextmanager
def native_or_experiment():
    original_bridge = search._NODE_BRIDGE
    original_shared = search.LibrarySearch._engine_shared_keyword
    original_one = paired.one_query
    original_write = paired.write
    if original_bridge.count(NEEDLE) != 1:
        raise RuntimeError('Shared QMD query shape changed; refuse an ambiguous override')
    bridge = original_bridge.replace(NEEDLE, REPLACEMENT)

    def shared(self, request):
        request = {**request, 'experimental_native_terms': search._lex_terms(request['query'])}
        return original_shared(self, request)

    def one(provider, spec, mode):
        row = original_one(provider, spec, mode)
        row['treatment'] = POLICY if mode == 'qmd_keyword' else 'native-unchanged'
        row['native_terms'] = search._lex_terms(spec['arguments']['query'])
        row['native_match'] = ' OR '.join('"' + t.replace('"', '""') + '"' for t in row['native_terms'])
        for hit in row['results']:
            if mode == 'qmd_keyword':
                hit['retrieval']['experimental_policy'] = POLICY
        return row

    def write(path, value):
        if path.name == 'protocol.json':
            value = copy.deepcopy(value)
            value.update(experimental_policy=POLICY, query_compilation_override=True,
                         original_query_text_preserved=True, official_qmd_searchlex=False,
                         qmd_ranking='one BM25(1.5,4,1), original QMD score mapping',
                         native_ranking='unchanged BM25(0,6,1,1)', fusion=False,
                         term_function='advisor.library_search._lex_terms (same as native)',
                         cjk_handling='SDK normalizeCjkForFTS per term to match QMD index storage',
                         experimental_script_sha256=paired.sha(Path(__file__)),
                         experimental_bridge_sha256=hashlib.sha256(bridge.encode()).hexdigest())
        original_write(path, value)

    search._NODE_BRIDGE = bridge
    search.LibrarySearch._engine_shared_keyword = shared
    paired.one_query = one
    paired.write = write
    try:
        yield
    finally:
        search._NODE_BRIDGE = original_bridge
        search.LibrarySearch._engine_shared_keyword = original_shared
        paired.one_query = original_one
        paired.write = original_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--data', type=Path, default=paired.DEFAULT_DATA)
    parser.add_argument('--offline', type=Path, default=paired.DEFAULT_OFFLINE)
    args = parser.parse_args()
    baseline = args.baseline.resolve()
    protocol = json.loads((baseline / 'protocol.json').read_text(encoding='utf-8'))
    manifest, payload = paired.load_manifest(baseline / 'manifest.json')
    if hashlib.sha256(payload).hexdigest() != protocol['manifest_sha256']:
        raise RuntimeError('Baseline manifest hash mismatch')
    for name, expected in protocol['source_sha256'].items():
        if paired.sha(paired.APP / name) != expected:
            raise RuntimeError('Source changed since baseline: ' + name)
    sdk = args.offline / 'qmd/node_modules/@tobilu/qmd'
    for name, expected in protocol['sdk_sha256'].items():
        if paired.sha(sdk / name) != expected:
            raise RuntimeError('SDK changed since baseline: ' + name)
    pointer = json.loads((args.data / 'libraries/search/active.json').read_text(encoding='utf-8'))
    if pointer['generation'] != protocol['generation']:
        raise RuntimeError('Production generation changed since baseline')
    active = json.loads((args.data / 'libraries/search' / pointer['manifest']).read_text(encoding='utf-8'))
    if active['keyword_storage'] != 'shared-collections-v1':
        raise RuntimeError('This experiment supports shared QMD storage only')
    subset = {**manifest, 'queries': [q for q in manifest['queries'] if q['group'] == 'historical'],
              'experiment': POLICY, 'baseline_manifest_sha256': protocol['manifest_sha256']}
    path = args.out.resolve().with_name(args.out.name + '-manifest.json')
    if path.exists():
        raise RuntimeError('Experiment manifest already exists; choose a new output')
    if not path.is_relative_to(paired.WORKSPACE.resolve()):
        raise ValueError('Experiment output must stay in the workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    paired.write(path, subset)
    with native_or_experiment():
        result = paired.run_comparison(path, args.out, data_dir=args.data, offline=args.offline)
    if result['status'] != 'completed':
        raise RuntimeError('Experiment completed with errors; inspect output')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main()
