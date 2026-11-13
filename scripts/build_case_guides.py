"""Build complete case navigation from existing archives, without reparsing models."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from contextlib import closing
import sqlite3
import time
import uuid

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from advisor.case_guides import build_case_guides
from advisor.case_navigation import build_navigation
from advisor.build_control import BuildCancelled, BuildControl, build_control
from advisor.qmd_worker import suspend_workers
from scripts.normalize_tutorials import ReadOnlyLibraryStore, _offline_build_lease


def _write(path, value):
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(min(0.02 * (2 ** attempt), 0.1))
    finally:
        temporary.unlink(missing_ok=True)


def _progress_write(path, state):
    try:
        _write(path, state)
    except PermissionError:
        # Windows readers may briefly deny delete-sharing. Progress is
        # disposable display metadata, never a reason to abort valid work.
        state['progress_write_skips'] = state.get('progress_write_skips', 0) + 1


def _existing_full_navigation(store):
    """Resume valid full membership without recompiling unchanged archives."""
    from advisor.case_navigation import validated_navigation
    from advisor.index_manifest_cache import read_manifest
    from advisor.evaluation_sets import _load, _watermark
    active = read_manifest(store.root / 'search' / 'active.json', summary=True, resolve_paths=True)
    if not active:
        return None
    try:
        navigation = validated_navigation(store.root / 'search', active)
        meta = navigation['meta']
        if meta.get('build_scope') != 'all':
            return None
        registry, closed_policy, _ = _load(store.root)
        policy = closed_policy if registry is not None else store.evaluation_policy()
        with closing(store.connect()) as db:
            watermark = _watermark(db)
        if meta.get('catalog_revision_watermark') != watermark or meta.get('compiled_policy_token') != policy.token:
            return None
        return {key: meta[key] for key in ('schema', 'generation', 'build_scope', 'prepared_case_count',
                'overview_count', 'member_count', 'skipped_heldout', 'skipped_geometry_only') if key in meta} | {
                    'path': navigation['path'], 'unchanged': True, 'source_text_stored': False,
                    'qmd_rebuilt': False, 'embedding_invoked': False}
    except (RuntimeError, ValueError, OSError, sqlite3.Error):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Existing workbench root or libraries directory")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--case", action="append", dest="case_ids", help="Exact non-heldout model_case citation ID; repeat to select several")
    scope.add_argument("--all", action="store_true", help="Prepare all eligible active cases, including ordinary disabled libraries; never enables them")
    parser.add_argument("--progress-file", type=Path, help="Small JSON progress snapshot; defaults under search/runtime/case-navigation-build")
    parser.add_argument("--report", type=Path, help="Write the final coverage and failure report")
    parser.add_argument("--stop-file", type=Path, help="Create this marker to cancel cooperatively between nodes/cases; remove it before resuming")
    parser.add_argument("--no-resume", action="store_true", help="Rebuild guides even when their current-generation records already exist")
    args = parser.parse_args(argv)
    state = {'schema': 'case-navigation-build/v1', 'state': 'running', 'stage': 'starting',
             'source_parsing': False, 'embedding_invoked': False, 'library_visibility_changed': False}
    progress_path = args.progress_file
    try:
        store = ReadOnlyLibraryStore(args.data_dir)
        runtime = store.root / 'search' / 'runtime' / 'case-navigation-build'
        progress_path = progress_path or runtime / 'progress.json'
        stop_path = args.stop_file or runtime / 'stop'
        state['stop_file'] = str(stop_path)
        state['pid'] = os.getpid()
        state['scope'] = 'all' if args.all else 'selected'

        def progress(stage, event=None):
            # Never copy case lists, document bodies or complete result trees
            # into frequently inspected status snapshots.
            event = dict(event or {})
            if stage == 'membership':
                event.update(completed=event.get('processed_cases', 0), total=event.get('overview_count'),
                             case_id=event.get('current_case'))
            allowed = {'completed', 'total', 'case_id', 'built_count', 'skipped_count', 'failed_count',
                       'prepared_case_count', 'member_count', 'skipped_heldout', 'skipped_geometry_only', 'detail'}
            state.update({key: value for key, value in event.items() if key in allowed})
            state['state'] = 'running'
            state['stage'] = stage
            state['updated_at'] = datetime.now(timezone.utc).isoformat()
            _progress_write(progress_path, state)

        progress('starting')
        with build_control(BuildControl(stop_path)), _offline_build_lease(store), suspend_workers(store.root / 'search'):
            navigation = _existing_full_navigation(store) if args.all and not args.no_resume else None
            if navigation is None:
                navigation = build_navigation(store, case_ids=None if args.all else args.case_ids,
                                              progress=lambda event: progress('membership', event))
            else:
                state.update({key: navigation[key] for key in ('prepared_case_count', 'member_count',
                             'skipped_heldout', 'skipped_geometry_only') if key in navigation})
            progress('guides', {'completed': 0, 'total': navigation.get('prepared_case_count')})
            result = build_case_guides(store, None if args.all else args.case_ids,
                                       progress=lambda event: progress('guides', event), resume=not args.no_resume)
        state.update(state='partial' if result.get('failed_count') else 'completed', stage='finished',
                     navigation=navigation, guides=result, updated_at=datetime.now(timezone.utc).isoformat())
        _progress_write(progress_path, state)
        _write(args.report, state)
        print(json.dumps(state, ensure_ascii=False))
        return 0 if state['state'] == 'completed' else 2
    except BuildCancelled:
        state.update(state='cancelled', stage='stopped', updated_at=datetime.now(timezone.utc).isoformat())
        _progress_write(progress_path, state)
        _write(args.report, state)
        print(json.dumps(state, ensure_ascii=False))
        return 2
    except (OSError, ValueError, RuntimeError, PermissionError) as exc:
        state.update(state='failed', error=f'{type(exc).__name__}: {str(exc)[:1000]}',
                     updated_at=datetime.now(timezone.utc).isoformat())
        _progress_write(progress_path, state)
        _write(args.report, state)
        print(json.dumps(state, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
