/** Configure a selected offline bundle through its existing Node initializer. */
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, realpath, rename, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadDependencyProfile } from './dependency-profile.js';

export class OfflineBundleSetupError extends Error {
  constructor(message, code = 'offline_setup_failed') { super(message); this.name = 'OfflineBundleSetupError'; this.code = code; }
}

const RECEIPT = 'plugin-offline-setup-receipt.json';
const EXTERNAL = { comsolHome: '--comsol-home', docRoot: '--doc-root', applications: '--applications' };
const PATHS = { pythonPath: 'python/python.exe', simPython: 'python/python.exe', mcpPythonPath: 'python/python.exe',
  nodePath: 'node/node.exe', qmdDir: 'qmd/node_modules/@tobilu/qmd', embedModelPath: 'models/embeddinggemma-300M-Q8_0.gguf' };
const SHA = /^[a-f0-9]{64}$/;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const inside = (root, candidate) => { const rel = path.relative(root, candidate); return rel === '' || (rel !== '..' && !rel.startsWith('..' + path.sep) && !path.isAbsolute(rel)); };
const samePath = (a, b) => process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
const digestText = text => createHash('sha256').update(text).digest('hex');
const fail = (message, code) => { throw new OfflineBundleSetupError(message, code); };

function absolute(value, label) {
  if (typeof value !== 'string' || !value || /[\0\r\n]/.test(value) || !path.isAbsolute(value)) fail(`${label} 必须是本机绝对路径。`, 'invalid_path');
  return path.resolve(value);
}

async function canonicalMissing(value) {
  const missing = []; let current = value;
  while (true) {
    try { return path.join(await realpath(current), ...missing.reverse()); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      const parent = path.dirname(current); if (parent === current) throw error;
      missing.push(path.basename(current)); current = parent;
    }
  }
}

async function jsonFile(filename, limit = 32 * 1024 * 1024) {
  const info = await stat(filename);
  if (!info.isFile() || info.size > limit) throw new Error('invalid JSON file size');
  const raw = await readFile(filename, 'utf8');
  return { value: JSON.parse(raw), sha256: digestText(raw) };
}

async function containedFile(root, relative) {
  const filename = path.resolve(root, relative);
  if (!inside(root, filename)) fail('离线包文件路径越界。', 'invalid_manifest');
  let actual;
  try { actual = await realpath(filename); }
  catch { fail(`离线包文件缺失：${relative}`, 'integrity_failed'); }
  if (!inside(root, actual) || !(await stat(actual)).isFile()) fail(`离线包文件链接越界或类型无效：${relative}`, 'integrity_failed');
  return actual;
}

function entries(manifest, schema) {
  if (!object(manifest) || manifest.schema !== schema || !Array.isArray(manifest.files) || !manifest.files.length || manifest.files.length > 100000) {
    fail('离线包 CONTENTS.json 版本或文件清单无效。', 'invalid_manifest');
  }
  const found = new Set();
  return manifest.files.map(entry => {
    if (!object(entry) || typeof entry.path !== 'string' || !entry.path || entry.path.length > 32768 || /[\0\r\n:]/.test(entry.path)
        || path.posix.isAbsolute(entry.path) || path.win32.isAbsolute(entry.path) || !Number.isSafeInteger(entry.bytes) || entry.bytes < 0
        || typeof entry.sha256 !== 'string' || !SHA.test(entry.sha256)) {
      fail('离线包清单含无效路径、大小或 SHA256。', 'invalid_manifest');
    }
    const normalized = entry.path.replaceAll('\\', '/');
    if (normalized.split('/').some(part => !part || part === '.' || part === '..') || found.has(normalized.toLowerCase())) {
      fail('离线包清单含越界或重复路径。', 'invalid_manifest');
    }
    found.add(normalized.toLowerCase());
    return { ...entry, path: normalized };
  });
}

async function verifyEntry(root, entry) {
  const filename = await containedFile(root, entry.path);
  if ((await stat(filename)).size !== entry.bytes) fail(`离线包文件大小不符：${entry.path}`, 'integrity_failed');
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(filename)) hash.update(chunk);
  if (hash.digest('hex') !== entry.sha256) fail(`离线包文件 SHA256 不符：${entry.path}`, 'integrity_failed');
  return filename;
}

async function inspectBundle(root, { verifyPrograms = false } = {}) {
  const filename = await containedFile(root, 'CONTENTS.json');
  let base;
  try { base = await jsonFile(filename); }
  catch { fail('无法读取有效的离线包 CONTENTS.json。', 'invalid_manifest'); }
  const baseEntries = entries(base.value, 'sim-advisor-offline-bundle/v1');
  const find = relative => baseEntries.find(entry => entry.path.toLowerCase() === relative.toLowerCase());
  const required = ['node/node.exe', 'initialize.mjs'];
  const programs = {};
  for (const relative of required) {
    const entry = find(relative);
    if (!entry) fail(`离线清单缺少 ${relative}。`, 'invalid_manifest');
    programs[relative] = verifyPrograms ? await verifyEntry(root, entry) : await containedFile(root, entry.path);
  }
  const model = find(PATHS.embedModelPath);
  let addon = null;
  const addonPath = path.join(root, 'addon-pageindex');
  try { await stat(addonPath); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return { base, baseEntries, programs, model, addon }; }
  const canonicalAddon = await realpath(addonPath);
  if (!inside(root, canonicalAddon) || !(await stat(canonicalAddon)).isDirectory()) fail('PageIndex 扩展目录越界或类型无效。', 'invalid_manifest');
  let addonManifest;
  try { addonManifest = await jsonFile(await containedFile(canonicalAddon, 'CONTENTS.json')); }
  catch (error) { if (error instanceof OfflineBundleSetupError) throw error; fail('PageIndex 扩展 CONTENTS.json 无效。', 'invalid_manifest'); }
  const addonEntries = entries(addonManifest.value, 'sim-advisor-offline-addon/v1');
  const basePython = find('manifests/python/manifest-minimal.json');
  if (!basePython || !SHA.test(addonManifest.value.requires_base_python_manifest_sha256)
      || addonManifest.value.requires_base_python_manifest_sha256 !== basePython.sha256) fail('PageIndex 扩展与基础 Python 清单不匹配。', 'invalid_manifest');
  if (verifyPrograms) await verifyEntry(root, basePython);
  addon = { root: canonicalAddon, manifest: addonManifest, entries: addonEntries };
  return { base, baseEntries, programs, model, addon };
}

function bundleSignature(bundle) {
  return { manifestSha256: bundle.base.sha256, addonManifestSha256: bundle.addon?.manifest.sha256 ?? null };
}

async function externalConfiguration(dataDir, provided) {
  if (!object(provided) || Object.keys(provided).some(key => !Object.hasOwn(EXTERNAL, key))) fail('外部资料路径只支持 comsolHome、docRoot、applications。', 'invalid_path');
  let previous = {};
  try { previous = (await jsonFile(await containedFile(dataDir, 'dependency-profile.json'), 1024 * 1024)).value.externalPaths ?? {}; } catch { /* Invalid old configuration is repairable. */ }
  const result = {}, warnings = [];
  for (const key of Object.keys(EXTERNAL)) {
    const explicit = Object.hasOwn(provided, key) && provided[key] !== undefined && provided[key] !== '';
    const value = explicit ? provided[key] : object(previous) ? previous[key] : undefined;
    if (value === undefined || value === '') continue;
    try {
      const resolved = await realpath(absolute(value, key));
      if (!(await stat(resolved)).isDirectory()) throw new Error('not directory');
      result[key] = resolved;
    } catch (error) {
      if (explicit) fail(`${key} 必须指向可访问的本机目录。`, 'invalid_path');
      // A stale external path must not prevent selecting a new offline bundle.
      warnings.push(`原 ${key} 路径已不可访问或无效，本次未沿用；请在插件设置中重新指定。`);
    }
  }
  return { paths: result, warnings };
}

async function verifiedConfiguration(root, dataDir, bundle, expectedExternal, receipt) {
  const profile = await jsonFile(await containedFile(dataDir, 'dependency-profile.json'), 1024 * 1024);
  const proof = await jsonFile(await containedFile(dataDir, 'offline-verification.json'), 1024 * 1024);
  const value = profile.value, verification = proof.value;
  const loaded = await loadDependencyProfile(dataDir);
  if (!object(value) || value.manifestSha256 !== bundle.base.sha256 || !samePath(await realpath(value.bundleRoot), root)
      || !object(verification) || verification.verified !== true || verification.singlePython !== true
      || !samePath(await realpath(verification.bundleRoot), root)
      || ![bundle.baseEntries.length, bundle.baseEntries.filter(entry => !entry.path.toLowerCase().endsWith('.gguf')).length].includes(verification.checkedFiles)
      || (verification.modelSha256 != null && verification.modelSha256 !== bundle.model?.sha256)
      || typeof value.configuredAt !== 'string' || !value.configuredAt
      || verification.configuredAt !== value.configuredAt || verification.pageindexAddon !== Boolean(bundle.addon)
      || !object(verification.core) || verification.core.version?.[0] !== 3 || verification.core.version?.[1] !== 12) throw new Error('verification mismatch');
  for (const [key, relative] of Object.entries(PATHS)) {
    if (key === 'embedModelPath') {
      if (bundle.model ? value.paths[key] !== relative || loaded.options[key] !== path.resolve(root, relative) : Object.hasOwn(value.paths, key)) throw new Error('optional embedding hint mismatch');
      continue;
    }
    if (value.paths[key] !== relative || !samePath(loaded.options[key], await realpath(path.join(root, relative)))) throw new Error('profile path mismatch');
  }
  if (bundle.addon ? loaded.options.pageindexPython !== loaded.options.pythonPath : Object.hasOwn(value.paths, 'pageindexPython')) throw new Error('optional addon mismatch');
  if (!samePath(await realpath(verification.core.prefix), await realpath(path.join(root, 'python')))) throw new Error('Python prefix mismatch');
  const actualExternal = value.externalPaths ?? {};
  if (Object.keys(actualExternal).length !== Object.keys(expectedExternal).length
      || Object.entries(expectedExternal).some(([key, directory]) => !samePath(actualExternal[key] ?? '', directory))) throw new Error('external configuration mismatch');
  // A copied data directory retains the exact signed-by-hash proof bytes.
  // Resolve its one operational report locator inside the selected directory;
  // never open the old data location, even when it still exists.
  let relative;
  if (typeof receipt?.qmdReportRelativePath === 'string') relative = receipt.qmdReportRelativePath;
  else {
    const prior = absolute(verification.qmdReport, 'QMD 验证记录');
    if (inside(dataDir, prior)) relative = path.relative(dataDir, prior);
    else if (typeof receipt?.dataDir === 'string' && path.isAbsolute(receipt.dataDir)
      && path.parse(receipt.dataDir).root !== path.resolve(receipt.dataDir) && inside(path.resolve(receipt.dataDir), prior)) relative = path.relative(receipt.dataDir, prior);
    else throw new Error('QMD proof outside data directory');
  }
  if (!relative || path.isAbsolute(relative)) throw new Error('Invalid QMD proof relative locator');
  const qmdPath = await containedFile(dataDir, relative);
  const qmd = await jsonFile(qmdPath, 4 * 1024 * 1024);
  if (!object(qmd.value) || qmd.value.ok !== true || !Array.isArray(qmd.value.network?.attempts) || qmd.value.network.attempts.length
      || (qmd.value.keyword_only === true ? !Array.isArray(qmd.value.lexical) || !qmd.value.lexical.length
        : qmd.value.cpu?.gpu !== false || !Number.isFinite(qmd.value.embedding_dimensions) || qmd.value.embedding_dimensions <= 0)) throw new Error('QMD proof invalid');
  return { profile, proof, qmd, loaded, verification, qmdReportRelativePath: path.relative(dataDir, qmdPath).split(path.sep).join('/') };
}

async function writeReceipt(filename, value) {
  const temporary = filename + '.' + randomUUID() + '.tmp';
  try { await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 }); await rename(temporary, filename); }
  finally { await rm(temporary, { force: true }); }
}

/** run(command, argv, {cwd,timeoutMs,maxOutput,shell}) resolves stdout on exit 0. */
export async function prepareOfflineBundle({ bundleRoot, dataDir, run, externalPaths = {}, onProgress,
  forceVerify = false, allowInitialize = true } = {}) {
  if (typeof run !== 'function') fail('离线配置缺少程序执行器。', 'invalid_runner');
  if (onProgress !== undefined && typeof onProgress !== 'function') fail('onProgress 必须为函数。', 'invalid_runner');
  if (typeof forceVerify !== 'boolean' || typeof allowInitialize !== 'boolean') fail('离线配置选项必须为布尔值。', 'invalid_options');
  const progress = (phase, detail) => { try { onProgress?.({ phase, detail }); } catch { /* An observer cannot invalidate verified configuration. */ } };
  let root, data;
  try {
    root = await realpath(absolute(bundleRoot, '离线包目录'));
    if (!(await stat(root)).isDirectory()) throw new Error('not directory');
    data = await canonicalMissing(absolute(dataDir, '数据目录'));
  } catch (error) { if (error instanceof OfflineBundleSetupError) throw error; fail('无法访问所选离线包或数据目录。', 'invalid_path'); }
  if (inside(root, data)) fail('数据目录必须位于离线包外。', 'invalid_path');
  const receiptPath = path.join(data, RECEIPT);
  try {
    progress('checking', '正在检查离线包清单与初始化程序。');
    const bundle = await inspectBundle(root);
    const external = await externalConfiguration(data, externalPaths);
    const desiredExternal = external.paths;
    for (const detail of external.warnings) progress('checking', detail);
    const signature = { schema: 'sim-advisor-plugin-offline-setup/v1', bundleRoot: root, dataDir: data,
      ...bundleSignature(bundle), externalPaths: desiredExternal };
    let receipt, existing;
    try { receipt = (await jsonFile(await containedFile(data, RECEIPT), 1024 * 1024)).value; } catch { /* No valid receipt: initialize. */ }
    try { existing = await verifiedConfiguration(root, data, bundle, desiredExternal, receipt); } catch { /* Old profile/proof may be repaired. */ }
    const expectedReceipt = existing && { ...signature, profileSha256: existing.profile.sha256,
      verificationSha256: existing.proof.sha256, qmdReportSha256: existing.qmd.sha256 };
    const reusable = object(receipt) && expectedReceipt && Object.keys(expectedReceipt).every(key => key === 'dataDir'
      ? typeof receipt.dataDir === 'string' && path.isAbsolute(receipt.dataDir)
      : JSON.stringify(receipt[key]) === JSON.stringify(expectedReceipt[key]));
    if (reusable) {
      let checked = existing;
      if (forceVerify) {
        progress('checking', '正在完整复核离线包文件；复用已通过的 CPU 检索验收。');
        const work = [...bundle.baseEntries.map(entry => [root, entry]), ...(bundle.addon?.entries.map(entry => [bundle.addon.root, entry]) ?? [])];
        for (let index = 0; index < work.length; index += 8) await Promise.all(work.slice(index, index + 8).map(([base, entry]) => verifyEntry(base, entry)));
        const current = await inspectBundle(root);
        checked = await verifiedConfiguration(root, data, current, desiredExternal, receipt);
        if (JSON.stringify(bundleSignature(current)) !== JSON.stringify(bundleSignature(bundle)) || checked.profile.sha256 !== existing.profile.sha256 || checked.proof.sha256 !== existing.proof.sha256 || checked.qmd.sha256 !== existing.qmd.sha256) fail('离线文件或配置在复核时发生变化，请重试。', 'configuration_changed');
      }
      if (!samePath(receipt.dataDir, data) || receipt.qmdReportRelativePath !== checked.qmdReportRelativePath) {
        await writeReceipt(receiptPath, { ...expectedReceipt, qmdReportRelativePath: checked.qmdReportRelativePath,
          preparedAt: receipt.preparedAt ?? new Date().toISOString() });
      }
      progress('ready', forceVerify ? '离线包完整校验通过，已有配置可用。' : '已读取已有离线依赖配置。');
      return { ready: true, reused: true, bundleRoot: root, dataDir: data, ...checked.loaded, verification: checked.verification, receiptPath, warnings: external.warnings };
    }
    if (!allowInitialize) fail('离线依赖配置缺失、已变化或验证记录不匹配；请在插件设置中配置所选离线包后再启动。', 'configuration_required');
    await mkdir(data, { recursive: true });
    await rm(receiptPath, { force: true });
    progress('initializing', '正在校验基础依赖并运行 QMD 关键词检索验收；embedding 保持关闭。');
    // A reusable receipt takes the full payload pass above, including these
    // launch files, exactly once. A fresh initializer must be hashed before
    // executing it; its own full verification and the postflight stay intact.
    for (const relative of ['node/node.exe', 'initialize.mjs']) {
      bundle.programs[relative] = await verifyEntry(root, bundle.baseEntries.find(entry => entry.path.toLowerCase() === relative));
    }
    let initializer, verifier;
    try {
      initializer = await realpath(fileURLToPath(new URL('./offline-initialize.mjs', import.meta.url)));
      verifier = await realpath(fileURLToPath(new URL('./runtime/scripts/verify_offline_qmd.mjs', import.meta.url)));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
      initializer = await realpath(fileURLToPath(new URL('../offline-dependencies/initialize.mjs', import.meta.url)));
      verifier = await realpath(fileURLToPath(new URL('../../scripts/verify_offline_qmd.mjs', import.meta.url)));
    }
    // Use this plugin's keyword-only initializer even for an old full bundle;
    // the legacy initializer unconditionally loaded and probed its GGUF.
    const argv = [initializer, '--bundle-root', root, '--verifier', verifier, '--data-dir', data];
    for (const [key, flag] of Object.entries(EXTERNAL)) if (desiredExternal[key]) argv.push(flag, desiredExternal[key]);
    try { await run(bundle.programs['node/node.exe'], argv, { cwd: root, timeoutMs: 600000, maxOutput: 4 * 1024 * 1024, shell: false }); }
    catch { fail('离线包初始化未成功完成，请检查包完整性、数据目录权限与本机运行依赖后重试。', 'initialization_failed'); }
    const current = await inspectBundle(root, { verifyPrograms: true });
    if (JSON.stringify(bundleSignature(current)) !== JSON.stringify(bundleSignature(bundle))) fail('离线包清单在初始化期间发生变化，请重试。', 'configuration_changed');
    let checked;
    try { checked = await verifiedConfiguration(root, data, current, desiredExternal); }
    catch { fail('初始化未产生与所选离线包匹配的单 Python 配置和离线验证记录。', 'invalid_result'); }
    if (existing && existing.profile.sha256 === checked.profile.sha256 && existing.proof.sha256 === checked.proof.sha256) fail('初始化未更新离线验证记录，未认定就绪。', 'invalid_result');
    await writeReceipt(receiptPath, { ...signature, qmdReportRelativePath: checked.qmdReportRelativePath, profileSha256: checked.profile.sha256, verificationSha256: checked.proof.sha256,
      qmdReportSha256: checked.qmd.sha256, preparedAt: new Date().toISOString() });
    progress('ready', '离线依赖与 CPU 检索验收通过。');
    return { ready: true, reused: false, bundleRoot: root, dataDir: data, ...checked.loaded, verification: checked.verification, receiptPath, warnings: external.warnings };
  } catch (error) {
    // Routine startup is read-only: a failed lightweight check must not erase
    // the last setup receipt and accidentally force embedding on a later save.
    if (allowInitialize || forceVerify) {
      try { await rm(receiptPath, { force: true }); } catch { /* An unvalidated receipt is never reused. */ }
    }
    if (error instanceof OfflineBundleSetupError) throw error;
    fail('离线包配置失败，请检查清单、路径与数据目录权限。', 'offline_setup_failed');
  }
}
