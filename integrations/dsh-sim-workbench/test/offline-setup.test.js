import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { cp, mkdir, mkdtemp, readFile, rename, rm, stat, symlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { OfflineBundleSetupError, prepareOfflineBundle } from '../offline-setup.js';

const sha = value => createHash('sha256').update(value).digest('hex');
const MODEL = 'models/embeddinggemma-300M-Q8_0.gguf';
const PROFILE = 'dependency-profile.json';
const PROOF = 'offline-verification.json';
const RECEIPT = 'plugin-offline-setup-receipt.json';
const paths = { pythonPath: 'python/python.exe', simPython: 'python/python.exe', mcpPythonPath: 'python/python.exe',
  nodePath: 'node/node.exe', qmdDir: 'qmd/node_modules/@tobilu/qmd', embedModelPath: MODEL };

async function fixture(t) {
  const parent = path.dirname(fileURLToPath(import.meta.url));
  const root = await mkdtemp(path.join(parent, 'offline-setup-test-'));
  t.after(async () => { assert.equal(path.dirname(path.resolve(root)), parent); await rm(root, { recursive: true, force: true }); });
  const f = { root, bundle: path.join(root, 'offline bundle with spaces'), data: path.join(root, 'persistent data'), calls: [] };
  await mkdir(f.bundle); await mkdir(f.data);
  const contents = {
    'node/node.exe': 'fixture-node-executable', 'initialize.mjs': '// fixture initializer',
    'python/python.exe': 'fixture-single-python', 'qmd/node_modules/@tobilu/qmd/index.js': '// fixture QMD',
    [MODEL]: 'fixture-model-weights', 'manifests/python/manifest-minimal.json': '{"minimal":true}',
  };
  const files = [];
  for (const [relative, body] of Object.entries(contents)) {
    const filename = path.join(f.bundle, relative); await mkdir(path.dirname(filename), { recursive: true }); await writeFile(filename, body);
    files.push({ path: relative, bytes: Buffer.byteLength(body), sha256: sha(body) });
  }
  f.manifest = { schema: 'sim-advisor-offline-bundle/v1', version: '0.2.2', files };
  f.saveManifest = () => writeFile(path.join(f.bundle, 'CONTENTS.json'), JSON.stringify(f.manifest));
  await f.saveManifest();
  f.produceConfiguration = async (argv, overrides = {}) => {
    const dataDir = argv[argv.indexOf('--data-dir') + 1];
    await mkdir(dataDir, { recursive: true });
    const raw = await readFile(path.join(f.bundle, 'CONTENTS.json'), 'utf8');
    const externalPaths = {};
    for (const [key, flag] of Object.entries({ comsolHome: '--comsol-home', docRoot: '--doc-root', applications: '--applications' })) {
      if (argv.includes(flag)) externalPaths[key] = argv[argv.indexOf(flag) + 1];
    }
    const addon = await stat(path.join(f.bundle, 'addon-pageindex')).then(() => true, () => false);
    const configuredAt = `2026-11-08T00:00:${String(f.calls.length).padStart(2, '0')}.000Z`;
    const profile = { schema: 'sim-advisor-offline-dependencies/v1', bundleRoot: f.bundle, paths: { ...paths },
      externalPaths, manifestSha256: sha(raw), configuredAt };
    const model = f.manifest.files.find(file => file.path === MODEL);
    if (!model) delete profile.paths.embedModelPath;
    if (addon) profile.paths.pageindexPython = paths.pythonPath;
    const qmdReport = path.join(dataDir, 'offline-checks', `report-${f.calls.length}.json`);
    await mkdir(path.dirname(qmdReport), { recursive: true });
    await writeFile(qmdReport, JSON.stringify({ ok: true, network: { attempts: [] }, keyword_only: true, lexical: [{ filepath: 'fluid.md' }] }));
    const proof = { verified: true, bundleRoot: f.bundle, singlePython: true, pageindexAddon: addon, configuredAt,
      checkedFiles: f.manifest.files.filter(file => !file.path.endsWith('.gguf')).length, embeddingVerified: false, modelSha256: null,
      core: { version: [3, 12, 14], prefix: path.join(f.bundle, 'python') }, qmdReport };
    await writeFile(path.join(dataDir, PROFILE), JSON.stringify({ ...profile, ...overrides.profile }));
    await writeFile(path.join(dataDir, PROOF), JSON.stringify({ ...proof, ...overrides.proof }));
  };
  f.run = async (command, argv, options) => {
    f.calls.push({ command, argv, options }); await f.produceConfiguration(argv); return 'fixture initialization complete\n';
  };
  f.prepare = extra => prepareOfflineBundle({ bundleRoot: f.bundle, dataDir: f.data, run: f.run, ...extra });
  f.addon = async revision => {
    const addon = path.join(f.bundle, 'addon-pageindex'); await mkdir(addon, { recursive: true });
    const body = '# fixture PageIndex addon';
    const relative = 'Lib/site-packages/pageindex/__init__.py';
    await mkdir(path.dirname(path.join(addon, relative)), { recursive: true }); await writeFile(path.join(addon, relative), body);
    const basePython = f.manifest.files.find(file => file.path === 'manifests/python/manifest-minimal.json');
    await writeFile(path.join(addon, 'CONTENTS.json'), JSON.stringify({ schema: 'sim-advisor-offline-addon/v1', revision,
      requires_base_python_manifest_sha256: basePython.sha256, files: [{ path: relative, bytes: Buffer.byteLength(body), sha256: sha(body) }] }));
  };
  return f;
}

test('fresh setup uses bundle Node with fixed argv, explicit limits, and validated result', async t => {
  const f = await fixture(t), progress = [];
  const comsol = path.join(f.root, 'COMSOL with spaces & literal characters'); await mkdir(comsol);
  const result = await f.prepare({ externalPaths: { comsolHome: comsol }, onProgress: event => progress.push(event.phase) });
  assert.equal(result.ready, true); assert.equal(result.reused, false);
  assert.equal(result.options.pythonPath, path.join(f.bundle, 'python/python.exe'));
  assert.equal(result.options.mcpPythonPath, result.options.pythonPath);
  assert.equal(result.status.dependency_bundle, f.bundle);
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.calls[0], { command: path.join(f.bundle, 'node/node.exe'),
    argv: [fileURLToPath(new URL('../../offline-dependencies/initialize.mjs', import.meta.url)), '--bundle-root', f.bundle,
      '--verifier', fileURLToPath(new URL('../../../scripts/verify_offline_qmd.mjs', import.meta.url)), '--data-dir', f.data, '--comsol-home', comsol],
    options: { cwd: f.bundle, timeoutMs: 600000, maxOutput: 4 * 1024 * 1024, shell: false } });
  assert.deepEqual(progress, ['checking', 'initializing', 'ready']);
  const receipt = JSON.parse(await readFile(result.receiptPath, 'utf8'));
  assert.equal(receipt.schema, 'sim-advisor-plugin-offline-setup/v1'); assert.equal(receipt.addonManifestSha256, null);
  assert.match(receipt.verificationSha256, /^[a-f0-9]{64}$/);
});

test('weight-free base configures and reuses keyword verification without any model', async t => {
  const f = await fixture(t);
  f.manifest.files = f.manifest.files.filter(file => file.path !== MODEL);
  await rm(path.join(f.bundle, MODEL)); await f.saveManifest();
  const result = await f.prepare();
  assert.equal(result.ready, true);
  assert.equal(result.options.embedModelPath, undefined);
  assert.equal(result.verification.embeddingVerified, false);
  assert.equal((await f.prepare({ allowInitialize: false })).reused, true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].argv.includes('--verify-embedding'), false);
});

test('legacy full-bundle configuration does not require its optional GGUF on disk', async t => {
  const f = await fixture(t);
  await rm(path.join(f.bundle, MODEL));
  const result = await f.prepare();
  assert.equal(result.ready, true);
  assert.equal(result.verification.embeddingVerified, false);
  assert.equal((await f.prepare({ allowInitialize: false })).reused, true);
  assert.equal(f.calls[0].argv[0].endsWith('offline-dependencies' + path.sep + 'initialize.mjs'), true);
});

test('matching receipt reuses CPU proof and configuration without reading payload bytes', async t => {
  const f = await fixture(t); await f.prepare();
  const original = fs.createReadStream;
  fs.createReadStream = () => { throw new Error('Routine reuse must not hash payloads'); };
  syncBuiltinESMExports();
  let result;
  try { result = await f.prepare({ allowInitialize: false, onProgress: () => { throw new Error('UI observer is unavailable'); } }); }
  finally { fs.createReadStream = original; syncBuiltinESMExports(); }
  assert.equal(result.reused, true); assert.equal(f.calls.length, 1);
});

test('receipt verification hashes every base and addon payload once, including launch programs', async t => {
  const f = await fixture(t); await f.addon(1); await f.prepare();
  const original = fs.createReadStream, reads = [];
  fs.createReadStream = (filename, ...args) => { reads.push(path.resolve(filename)); return original(filename, ...args); };
  syncBuiltinESMExports();
  try {
    assert.equal((await f.prepare({ forceVerify: true })).reused, true);
  } finally {
    fs.createReadStream = original;
    syncBuiltinESMExports();
  }
  const expected = [...f.manifest.files.map(entry => path.join(f.bundle, entry.path)),
    path.join(f.bundle, 'addon-pageindex/Lib/site-packages/pageindex/__init__.py')];
  assert.deepEqual(reads.sort(), expected.sort(), 'Full SHA verification remains, without repeating large node.exe reads');
  assert.equal(f.calls.length, 1, 'CPU embedding verification is reused, not rerun');
});

test('routine startup never initializes missing, changed or invalid configuration', async t => {
  const f = await fixture(t);
  await assert.rejects(f.prepare({ allowInitialize: false }), error => error.code === 'configuration_required' && /插件设置/.test(error.message));
  assert.equal(f.calls.length, 0);
  await f.prepare();
  f.manifest.note = 'Manifest intentionally changed'; await f.saveManifest();
  await assert.rejects(f.prepare({ allowInitialize: false }), error => error.code === 'configuration_required');
  assert.equal(f.calls.length, 1);
  await f.prepare();
  const proof = JSON.parse(await readFile(path.join(f.data, PROOF), 'utf8'));
  await writeFile(path.join(f.data, PROOF), JSON.stringify({ ...proof, verified: false }));
  await assert.rejects(f.prepare({ allowInitialize: false, forceVerify: true }), error => error.code === 'configuration_required');
  assert.equal(f.calls.length, 2, 'Even a verification request with initialization disabled cannot run embedding');
});

test('failed routine startup preserves all existing setup records instead of deleting the receipt', async t => {
  const f = await fixture(t); await f.prepare();
  const profile = JSON.parse(await readFile(path.join(f.data, PROFILE), 'utf8'));
  await writeFile(path.join(f.data, PROFILE), JSON.stringify({ ...profile, manifestSha256: '0'.repeat(64) }));
  const files = [PROFILE, PROOF, RECEIPT];
  const before = await Promise.all(files.map(name => readFile(path.join(f.data, name), 'utf8')));
  await assert.rejects(f.prepare({ allowInitialize: false }), error => error.code === 'configuration_required');
  assert.deepEqual(await Promise.all(files.map(name => readFile(path.join(f.data, name), 'utf8'))), before);
  assert.equal(f.calls.length, 1);
  // Repairing the profile externally makes the prior receipt reusable again;
  // ordinary startup did not force a new CPU embedding run by deleting it.
  await writeFile(path.join(f.data, PROFILE), JSON.stringify(profile));
  assert.equal((await f.prepare({ allowInitialize: false })).reused, true);
  assert.equal(f.calls.length, 1);
});

test('copied legacy data reuses existing CPU proof without initializer and never reads the old root', async t => {
  const f = await fixture(t); await f.prepare();
  const receiptFile = path.join(f.data, RECEIPT);
  const legacy = JSON.parse(await readFile(receiptFile, 'utf8')); delete legacy.qmdReportRelativePath;
  await writeFile(receiptFile, JSON.stringify(legacy));
  const old = f.data, copy = path.join(f.root, 'copied workspace');
  await cp(old, copy, { recursive: true });
  const proof = JSON.parse(await readFile(path.join(old, PROOF), 'utf8'));
  await writeFile(proof.qmdReport, 'old-location poison');
  f.data = copy;
  const result = await f.prepare({ run: async () => { throw new Error('Migration must not run CPU embedding smoke'); } });
  assert.equal(result.reused, true); assert.equal(f.calls.length, 1);
  const after = JSON.parse(await readFile(path.join(copy, RECEIPT), 'utf8'));
  assert.equal(after.dataDir, copy); assert.match(after.qmdReportRelativePath, /^offline-checks\//);
  const next = path.join(f.root, 'moved again'); await rename(copy, next); f.data = next;
  assert.equal((await f.prepare({ run: async () => { throw new Error('No initialization after second move'); } })).reused, true);
});

test('a forged relative proof locator or modified copied proof cannot reuse the CPU receipt', async t => {
  const f = await fixture(t); await f.prepare();
  const copy = path.join(f.root, 'copied workspace'); await cp(f.data, copy, { recursive: true }); f.data = copy;
  const receiptPath = path.join(copy, RECEIPT), receipt = JSON.parse(await readFile(receiptPath, 'utf8'));
  receipt.qmdReportRelativePath = '../outside.json'; await writeFile(receiptPath, JSON.stringify(receipt));
  await assert.rejects(f.prepare({ run: async () => { throw new Error('Invalid proof must not be reused'); } }));
});

test('bundle move and different data path trigger initialization rather than stale reuse', async t => {
  const f = await fixture(t); await f.prepare();
  const old = f.bundle; f.bundle = path.join(f.root, 'relocated bundle with spaces'); await rename(old, f.bundle);
  assert.equal((await f.prepare()).reused, false); assert.equal(f.calls.length, 2);
  f.data = path.join(f.root, 'different persistent data');
  assert.equal((await f.prepare()).reused, false); assert.equal(f.calls.length, 3);
});

test('external paths are preserved unless explicitly overridden; stale profile can be repaired', async t => {
  const f = await fixture(t), first = path.join(f.root, 'manuals one'), second = path.join(f.root, 'manuals two');
  await mkdir(first); await mkdir(second);
  await f.prepare({ externalPaths: { docRoot: first } });
  assert.equal((await f.prepare()).reused, true); assert.equal(f.calls.length, 1);
  const changed = await f.prepare({ externalPaths: { docRoot: second } });
  assert.equal(changed.options.docRoot, second); assert.equal(changed.reused, false);
  await writeFile(path.join(f.data, PROFILE), 'invalid old JSON');
  assert.equal((await f.prepare()).reused, false); assert.equal(f.calls.length, 3);
});

test('a missing saved external directory is reported rather than silently cleared', async t => {
  const f = await fixture(t), old = path.join(f.root, 'saved manuals'); await mkdir(old);
  await f.prepare({ externalPaths: { docRoot: old } }); await rm(old, { recursive: true });
  const progress = [], repaired = await f.prepare({ onProgress: event => progress.push(event.detail) });
  assert.equal(repaired.reused, false); assert.equal(f.calls.length, 2);
  assert.equal(repaired.options.docRoot, undefined);
  assert.equal(repaired.warnings.length, 1); assert.match(repaired.warnings[0], /docRoot/);
  assert.ok(progress.includes(repaired.warnings[0]));
});

test('base and addon CONTENTS signatures force reconfiguration; addon stays on same Python', async t => {
  const f = await fixture(t); await f.prepare();
  f.manifest.note = 'same files, revised manifest'; await f.saveManifest();
  assert.equal((await f.prepare()).reused, false);
  await f.addon(1);
  const extended = await f.prepare();
  assert.equal(extended.reused, false); assert.equal(extended.options.pageindexPython, extended.options.pythonPath);
  assert.equal((await f.prepare()).reused, true);
  await f.addon(2); assert.equal((await f.prepare()).reused, false);
  assert.equal(f.calls.length, 4);
});

test('corrupt ordinary payload and addon payload invalidate a previously successful receipt', async t => {
  for (const addon of [false, true]) await t.test(addon ? 'addon payload' : 'base payload', async t => {
    const f = await fixture(t); if (addon) await f.addon(1); await f.prepare();
    const file = addon ? 'addon-pageindex/Lib/site-packages/pageindex/__init__.py' : MODEL;
    await writeFile(path.join(f.bundle, file), 'tampered bytes');
    await assert.rejects(f.prepare({ forceVerify: true }), error => error instanceof OfflineBundleSetupError && error.code === 'integrity_failed');
    assert.equal(f.calls.length, 1); await assert.rejects(stat(path.join(f.data, RECEIPT)), { code: 'ENOENT' });
  });
});

test('preflight rejects tampered launch programs before execution and removes stale receipt', async t => {
  for (const program of ['node/node.exe', 'initialize.mjs']) await t.test(program, async t => {
    const f = await fixture(t); await f.prepare();
    const previous = await readFile(path.join(f.bundle, program));
    await writeFile(path.join(f.bundle, program), Buffer.alloc(previous.length, 88));
    await assert.rejects(f.prepare({ forceVerify: true }), error => error.code === 'integrity_failed' && /SHA256/.test(error.message));
    assert.equal(f.calls.length, 1); await assert.rejects(stat(path.join(f.data, RECEIPT)), { code: 'ENOENT' });
  });
});

test('initializer failure is readable, hides raw subprocess output, and retains no success receipt', async t => {
  const f = await fixture(t); await f.prepare();
  f.manifest.note = 'force fresh initialization'; await f.saveManifest();
  await assert.rejects(f.prepare({ run: async () => { throw new Error('RAW_SECRET_TOKEN from stderr'); } }), error => {
    assert.equal(error.code, 'initialization_failed'); assert.doesNotMatch(error.message, /RAW_SECRET/); return true;
  });
  await assert.rejects(stat(path.join(f.data, RECEIPT)), { code: 'ENOENT' });
});

test('zero exit alone cannot establish readiness without matching profile and CPU verification', async t => {
  for (const overrides of [{ proof: { verified: false } }, { proof: { singlePython: false } },
    { proof: { configuredAt: 'stale-proof-time' } }, { profile: { manifestSha256: '0'.repeat(64) } }]) {
    await t.test(JSON.stringify(overrides), async t => {
      const f = await fixture(t);
      await assert.rejects(f.prepare({ run: async (command, argv, options) => {
        f.calls.push({ command, argv, options }); await f.produceConfiguration(argv, overrides); return 'success but invalid proof';
      } }), error => error.code === 'invalid_result');
      await assert.rejects(stat(path.join(f.data, RECEIPT)), { code: 'ENOENT' });
    });
  }
});

test('receipt cannot substitute for actual profile, verification, or QMD proof', async t => {
  for (const filename of [PROFILE, PROOF]) await t.test(filename, async t => {
    const f = await fixture(t); await f.prepare(); await rm(path.join(f.data, filename));
    assert.equal((await f.prepare()).reused, false); assert.equal(f.calls.length, 2);
  });
  await t.test('CPU proof modified after prior success', async t => {
    const f = await fixture(t); await f.prepare();
    const proof = JSON.parse(await readFile(path.join(f.data, PROOF), 'utf8'));
    await writeFile(proof.qmdReport, JSON.stringify({ ok: false }));
    assert.equal((await f.prepare()).reused, false); assert.equal(f.calls.length, 2);
  });
});

test('strict absolute paths, outside data directory, manifest schema and entries are required', async t => {
  const f = await fixture(t);
  for (const extra of [{ bundleRoot: 'relative' }, { dataDir: 'relative' }, { dataDir: path.join(f.bundle, 'state') },
    { externalPaths: { arbitraryEnvironment: 'SECRET' } }, { externalPaths: { docRoot: 'relative' } }]) await assert.rejects(f.prepare(extra));
  const original = structuredClone(f.manifest);
  for (const mutate of [manifest => { manifest.schema = 'wrong'; }, manifest => { manifest.files[0].path = '../outside/node.exe'; },
    manifest => { manifest.files[0].path = 'C:\\outside\\node.exe'; }, manifest => { manifest.files.push({ ...manifest.files[0] }); },
    manifest => { manifest.files[0].sha256 = 'not a hash'; }, manifest => { manifest.files[0].sha256 = [manifest.files[0].sha256]; }]) {
    f.manifest = structuredClone(original); mutate(f.manifest); await f.saveManifest();
    await assert.rejects(f.prepare(), error => error.code === 'invalid_manifest');
  }
  assert.equal(f.calls.length, 0);
});

test('canonical containment rejects a junction escaping the selected bundle', async t => {
  const f = await fixture(t), outside = path.join(f.root, 'outside node'); await mkdir(outside);
  await writeFile(path.join(outside, 'node.exe'), 'fixture-node-executable');
  await rm(path.join(f.bundle, 'node'), { recursive: true });
  try { await symlink(outside, path.join(f.bundle, 'node'), process.platform === 'win32' ? 'junction' : 'dir'); }
  catch (error) { if (['EPERM', 'EACCES', 'ENOTSUP'].includes(error.code)) { t.skip('OS does not allow test directory links'); return; } throw error; }
  await assert.rejects(f.prepare(), error => error.code === 'integrity_failed' && /越界/.test(error.message));
  assert.equal(f.calls.length, 0);
});
