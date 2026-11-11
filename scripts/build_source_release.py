"""Export a source-only tree without local Git history, corpora or experiments."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

APP = Path(__file__).resolve().parents[1]
ROOT_FILES = {'.gitattributes', '.gitignore', 'README.md', 'CONTRACT.md',
              'MODEL_INTEGRATION.md', 'requirements-mcp.txt', 'run.py', 'start.ps1',
              'agent_cli.py', 'agent_tools.py', 'external_agent_client.py',
              'library_cli.py', 'tools_cli.py', 'workbench_mcp.py'}
SOURCE_DIRECTORIES = {'advisor', 'web', 'scripts', 'tests', 'integrations', 'prompts', 'docs'}
EXCLUDED_PREFIXES = ('benchmarks/', 'docs/evaluation-sets/', 'scripts/source_release_docs/')
EXCLUDED_FILES = {
    'scripts/geometry_challenge/scenario.json', 'scripts/geometry_challenge/scenario-02.json',
    'scripts/geometry_challenge/rubric.json', 'scripts/geometry_challenge/rubric-02.json',
    'scripts/select_library_pilot.py', 'scripts/run_library_pilot.py',
    'scripts/verify_library_pilot.py', 'scripts/compare_native_qmd.py',
    'scripts/evaluate_retrieval.py', 'tests/test_library_pilot.py', 'tests/test_library_report.py',
}
FORBIDDEN_PARTS = {'.git', '.tools', '.venv', 'venv', '.model-env', '.retrieval-env',
                   'data', 'dist', '__pycache__', '.pytest_cache', 'cache', 'models', 'sessions'}
FORBIDDEN_SUFFIXES = {'.mph', '.mphbin', '.step', '.stp', '.pdf', '.gguf', '.sqlite',
                      '.sqlite3', '.db', '.zip', '.tgz', '.gz', '.exe', '.dll', '.class', '.pyc', '.key', '.pem'}
CREDENTIAL = re.compile(r'(?<![\w-])(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


def allowed(name):
    path = Path(name)
    return (not path.is_absolute() and '..' not in path.parts
            and (name in ROOT_FILES or path.parts[0] in SOURCE_DIRECTORIES)
            and not name.startswith(EXCLUDED_PREFIXES) and name not in EXCLUDED_FILES
            and not FORBIDDEN_PARTS.intersection(path.parts)
            and not any(p.startswith('.test-') for p in path.parts)
            and path.suffix.lower() not in FORBIDDEN_SUFFIXES)


def collect(root=APP, *, tracked=None):
    root = Path(root).resolve()
    if tracked is None:
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
        tracked = result.stdout.decode('utf-8').split('\0')
        # These are publication tooling, included before their first local commit.
        tracked += ['scripts/build_source_release.py', 'tests/test_source_release.py']
    files = {}
    excluded = []
    for name in sorted(set(tracked)):
        if not name:
            continue
        if not allowed(name):
            excluded.append(name)
            continue
        source = root / name
        overlay = root / 'scripts/source_release_docs' / name
        if overlay.is_file():
            source = overlay
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError('Source path escapes the workspace: ' + name)
        if not source.is_file():
            raise ValueError('Required source is missing: ' + name)
        value = source.read_bytes()
        text = value.decode('utf-8')
        if CREDENTIAL.search(text):
            raise ValueError('Credential-like material requires review in: ' + name)
        if re.search(r'[CD]:[/\\]Users[/\\]65112|D:[/\\]project[/\\]codex[/\\]comsol[/\\]debug[/\\]experiments', text, re.I):
            raise ValueError('Personal workspace path requires review in: ' + name)
        files[name] = value
    return files, excluded


def build(destination, *, root=APP):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if not destination.is_relative_to(root / 'dist'):
        raise ValueError('Release output must be a new directory below the project dist directory')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Release output directory must be empty; existing files are retained')
    files, excluded = collect(root)
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    manifest = {'schema': 'sim-advisor-source-release/v1', 'source_only': True,
                'git_history_included': False, 'files': [
                    {'path': name, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                    for name, value in sorted(files.items())],
                'excluded_categories': ['official manuals', 'model and geometry files', 'indexes',
                    'local experiments and benchmark data', 'user data', 'credentials',
                    'runtime binaries', 'offline bundles', 'local Git history'],
                'file_count': len(files), 'total_bytes': sum(map(len, files.values()))}
    (destination / 'SOURCE-MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Excluded local filenames are deliberately kept outside the public tree.
    private_report = destination.parent / (destination.name + '-exclusions.json')
    private_report.write_text(json.dumps({'excluded': excluded}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'directory': str(destination), 'file_count': len(files) + 1,
            'bytes': manifest['total_bytes'], 'excluded_count': len(excluded)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=APP / 'dist/source-release')
    args = parser.parse_args()
    print(json.dumps(build(args.out_dir), ensure_ascii=False))
