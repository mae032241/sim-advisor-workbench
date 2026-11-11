"""Plan, run or inspect a durable serial API enrichment batch; never solve/embed."""
import argparse
import json
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from advisor.deep_enrichment import batch_status, create_batch, run_batch
from advisor.library_store import LibraryStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True, help='Workbench data directory containing libraries/')
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan')
    plan.add_argument('--library', action='append', dest='library_ids')
    plan.add_argument('--limit', type=int)
    plan.add_argument('--checkpoint', type=Path)
    run = commands.add_parser('run')
    run.add_argument('checkpoint', type=Path)
    run.add_argument('--retry-failed', action='store_true')
    run.add_argument('--batch-size', '--api-batch-size', type=int, choices=[0, 1, 3, 5], default=5,
                     help='Models per bounded JVM process (default 5); 0/1 explicitly select one JVM per model')
    run.add_argument('--stage-only', action='store_true', help='Only cache API overlays; keep all active catalog identities unchanged')
    run.add_argument('--no-index', action='store_true', help='Explicitly defer the one final native/keyword index update')
    run.add_argument('--no-index-cleanup', action='store_true', help='Explicitly defer retention cleanup after indexing')
    status = commands.add_parser('status')
    status.add_argument('checkpoint', type=Path)
    cancel = commands.add_parser('cancel')
    cancel.add_argument('checkpoint', type=Path)
    args = parser.parse_args(argv)
    store = LibraryStore(args.data_dir / 'libraries')
    if args.command == 'plan':
        result = create_batch(store, library_ids=args.library_ids, limit=args.limit, checkpoint=args.checkpoint)
    elif args.command == 'status':
        result = batch_status(store, args.checkpoint)
    elif args.command == 'cancel':
        job_id = batch_status(store, args.checkpoint).get('last_job_id')
        if not job_id:
            raise ValueError('This batch has not started a cancellable job')
        result = store.cancel_build(job_id)
    else:
        runner = None
        if args.batch_size in {3, 5}:
            from advisor.compact_api_batch import BatchAPIRunner
            runner = BatchAPIRunner(store, args.checkpoint, batch_size=args.batch_size)
        result = run_batch(store, args.checkpoint, retry_failed=args.retry_failed, publish=not args.stage_only,
                           api_runner=runner,
                           clean_indexes=not args.no_index_cleanup,
                           modes=[] if args.no_index else ['native', 'qmd_keyword'],
                           on_progress=lambda value: print(json.dumps({key: value.get(key) for key in
                               ('id', 'status', 'stage', 'completed', 'total', 'case_counts', 'api_counts', 'progress')}, ensure_ascii=False), flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result.get('status') == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
