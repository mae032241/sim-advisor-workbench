"""Summarize saved retrieval outputs; overlap is not a relevance score."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import statistics
import unicodedata


def load(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if row['group'] == 'historical':
            key = (row['query_key'], row['mode'])
            if key in result:
                raise ValueError('Duplicate query/backend output')
            result[key] = row
    return result


def cards(row):
    return {re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', x['title'] + '\n' + x.get('excerpt', ''))).strip().lower()
            for x in row['results']}


def compare(pairs):
    counts = Counter()
    overlaps, jaccards, contents = [], [], []
    for a, b in pairs:
        if a['arguments'] != b['arguments']:
            raise ValueError('Different query inputs')
        if a['error'] or b['error']:
            counts['errors'] += 1
            continue
        left, right = a['ids'], b['ids']
        if not left and not right:
            counts['both_empty'] += 1
        elif left and not right:
            counts['left_only'] += 1
        elif right and not left:
            counts['right_only'] += 1
        else:
            counts['both_nonempty'] += 1
            counts['same_top1'] += left[0] == right[0]
            counts['same_ordered_top6'] += left == right
            counts['same_top6_set'] += set(left) == set(right)
            overlaps.append(len(set(left) & set(right)))
            jaccards.append(len(set(left) & set(right)) / len(set(left) | set(right)))
            ca, cb = cards(a), cards(b)
            contents.append(len(ca & cb) / len(ca | cb))
    return {'counts': dict(counts), 'both_nonempty_mean_common_top6': statistics.mean(overlaps) if overlaps else None,
            'both_nonempty_mean_id_jaccard': statistics.mean(jaccards) if jaccards else None,
            'both_nonempty_mean_content_jaccard': statistics.mean(contents) if contents else None,
            'left_median_ms': 1000 * statistics.median(a['seconds'] for a, b in pairs),
            'right_median_ms': 1000 * statistics.median(b['seconds'] for a, b in pairs),
            'both_nonempty_left_median_ms': 1000 * statistics.median(a['seconds'] for a, b in pairs if a['ids'] and b['ids']),
            'both_nonempty_right_median_ms': 1000 * statistics.median(b['seconds'] for a, b in pairs if a['ids'] and b['ids'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--experiment', required=True, type=Path)
    args = parser.parse_args()
    old, current = (load(d / 'results.jsonl') for d in (args.baseline, args.experiment))
    state = json.loads((args.experiment / 'progress.json').read_text(encoding='utf-8'))
    if state['status'] != 'completed' or state['errors']:
        raise ValueError('Experiment is not completed without errors')
    keys = [r['query_key'] for r in current.values() if r['mode'] == 'native']
    if set(current) != set(old):
        raise ValueError('Historical query/backend sets differ')
    cleanup = json.loads((args.experiment / 'cleanup.json').read_text(encoding='utf-8'))
    if (not cleanup['scope_restored'] or not cleanup['owned_worker_closed'] or cleanup['errors']
            or cleanup['heldout_retrieval_enabled']):
        raise ValueError('Scope/worker/isolation cleanup failed')
    pairs = [(current[(k, 'native')], current[(k, 'qmd_keyword')]) for k in keys]
    old_pairs = [(old[(k, 'native')], old[(k, 'qmd_keyword')]) for k in keys]
    native_changed = [k for k in keys if current[(k, 'native')]['ids'] != old[(k, 'native')]['ids']]
    summary = {'schema': 'qmd-native-or-analysis/v1', 'query_count': len(keys), 'calls': len(current),
               'errors': state['errors'], 'native_ids_changed_since_original_baseline': native_changed,
               'original_native_vs_qmd_official_lexical': compare(old_pairs),
               'new_native_vs_qmd_native_or': compare(pairs),
               'nonempty': {'native': sum(bool(a['ids']) for a, b in pairs),
                            'qmd_native_or': sum(bool(b['ids']) for a, b in pairs),
                            'qmd_official_lexical': sum(bool(old[(k, 'qmd_keyword')]['ids']) for k in keys)},
               'formerly_empty_qmd_now_nonempty': [k for k in keys if not old[(k, 'qmd_keyword')]['ids'] and current[(k, 'qmd_keyword')]['ids']],
               'formerly_nonempty_qmd_now_empty': [k for k in keys if old[(k, 'qmd_keyword')]['ids'] and not current[(k, 'qmd_keyword')]['ids']],
               'all_empty_keys': [k for k in keys if not current[(k, 'qmd_keyword')]['ids']],
               'cleanup': cleanup,
               'limitations': 'ID/content overlap is not precision or recall. One elapsed sample per query. Original QMD lexical timing belongs to an earlier run.'}
    output = args.experiment / 'analysis.json'
    if output.exists():
        raise ValueError('Analysis already exists; do not overwrite')
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    small = {key: value for key, value in summary.items() if key not in {'cleanup', 'formerly_empty_qmd_now_nonempty'}}
    print(json.dumps(small, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
