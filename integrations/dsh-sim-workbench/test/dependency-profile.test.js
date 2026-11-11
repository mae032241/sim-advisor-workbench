import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { loadDependencyProfile } from '../dependency-profile.js';

async function fixture(t) {
  const parent = path.dirname(fileURLToPath(import.meta.url));
  const root = await mkdtemp(path.join(parent, 'profile-test-'));
  const data = path.join(root, 'user data'), bundle = path.join(root, 'offline dependencies');
  await mkdir(data); await mkdir(bundle);
  t.after(async () => { assert.equal(path.dirname(path.resolve(root)), parent); await rm(root, { recursive: true, force: true }); });
  const save = value => writeFile(path.join(data, 'dependency-profile.json'), JSON.stringify(value));
  return { root, data, bundle, save };
}

test('missing profile preserves existing installation discovery', async t => {
  const f = await fixture(t);
  assert.deepEqual(await loadDependencyProfile(f.data), { options: {}, status: {} });
});

test('profile rebases offline runtime paths and leaves external COMSOL roots explicit', async t => {
  const f = await fixture(t);
  await mkdir(path.join(f.bundle, 'python-core')); await writeFile(path.join(f.bundle, 'python-core/python.exe'), 'fixture');
  await mkdir(path.join(f.bundle, 'qmd'));
  await f.save({ schema: 'sim-advisor-offline-dependencies/v1', bundleRoot: f.bundle,
    paths: { pythonPath: 'python-core/python.exe', qmdDir: 'qmd', mcpPythonPath: 'python-core/python.exe' },
    externalPaths: { comsolHome: path.join(f.root, 'COMSOL') } });
  const profile = await loadDependencyProfile(f.data);
  assert.equal(profile.options.pythonPath, path.join(f.bundle, 'python-core/python.exe'));
  assert.equal(profile.options.comsolHome, path.join(f.root, 'COMSOL'));
  assert.equal(profile.status.mcp_python, profile.options.pythonPath);
  assert.equal(profile.status.dependency_bundle, f.bundle);
});

test('invalid manifest, unknown fields, moved roots and escaping paths are errors', async t => {
  const f = await fixture(t);
  const base = { schema: 'sim-advisor-offline-dependencies/v1', bundleRoot: f.bundle, paths: {} };
  for (const value of [ { ...base, paths: { pythonPath: '../outside.exe' } }, { ...base, paths: { arbitraryEnv: 'token' } },
    { ...base, bundleRoot: path.join(f.root, 'moved') }, { ...base, manifestSha256: 'a'.repeat(64) },
    { ...base, externalPaths: { docRoot: 'relative-path' } } ]) {
    await f.save(value);
    await assert.rejects(loadDependencyProfile(f.data));
  }
});

test('optional legacy embedding path does not require loading or statting weights', async t => {
  const f = await fixture(t);
  await f.save({ schema: 'sim-advisor-offline-dependencies/v1', bundleRoot: f.bundle,
    paths: { embedModelPath: 'models/not-installed.gguf' } });
  assert.equal((await loadDependencyProfile(f.data)).options.embedModelPath, path.join(f.bundle, 'models/not-installed.gguf'));
});
