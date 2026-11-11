#!/usr/bin/env node
/** Offline verification and workbench-owned path configuration. No installers or network. */
import { createHash, randomUUID } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, realpath, rename, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const initializerRoot = path.dirname(fileURLToPath(import.meta.url));
const schema = 'sim-advisor-offline-dependencies/v1';
const embedUri = 'hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf';
const relativePaths = { pythonPath: 'python/python.exe', simPython: 'python/python.exe',
  mcpPythonPath: 'python/python.exe', nodePath: 'node/node.exe',
  qmdDir: 'qmd/node_modules/@tobilu/qmd' };
const inside = (base, candidate) => { const rel = path.relative(base, candidate); return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)); };
const argv = process.argv.slice(2), options = {};
const flags = new Map([['--data-dir', 'dataDir'], ['--comsol-home', 'comsolHome'], ['--doc-root', 'docRoot'], ['--applications', 'applications'], ['--bundle-root', 'bundleRoot'], ['--verifier', 'verifier']]);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--verify-only') { options.verifyOnly = true; continue; }
  if (argv[i] === '--verify-embedding') { options.verifyEmbedding = true; continue; }
  if (!flags.has(argv[i]) || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error('参数：--data-dir PATH --comsol-home PATH --doc-root PATH --applications PATH --verify-only');
  options[flags.get(argv[i])] = argv[++i];
}
const root = options.bundleRoot ? path.resolve(options.bundleRoot) : initializerRoot;
const verifier = options.verifier ? path.resolve(options.verifier) : path.join(root, 'scripts/verify_offline_qmd.mjs');
if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('此离线包仅支持 Windows x64。');
const dataDir = path.resolve(options.dataDir || path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData/Local'), 'sim-advisor-workbench'));
if (inside(root, dataDir)) throw new Error('DataDir 必须位于离线包外，不能把运行状态写入分发目录。');
const digest = async filename => {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(filename)) hash.update(chunk);
  return hash.digest('hex');
};
const atomicJSON = async (filename, value) => {
  await mkdir(path.dirname(filename), { recursive: true });
  const temporary = filename + '.' + randomUUID() + '.tmp';
  await writeFile(temporary, JSON.stringify(value, null, 2) + '\n', 'utf8');
  await rename(temporary, filename);
};
const manifestText = await readFile(path.join(root, 'CONTENTS.json'), 'utf8');
const manifest = JSON.parse(manifestText);
if (manifest.schema !== 'sim-advisor-offline-bundle/v1' || !Array.isArray(manifest.files)) throw new Error('离线包清单无效。');
const modelEntry = manifest.files.find(entry => entry.path === 'models/embeddinggemma-300M-Q8_0.gguf');
if (options.verifyEmbedding && !modelEntry) throw new Error('基础包不含权重，请在工作台检索控制页手动选择可选本地 GGUF。');
if (modelEntry) relativePaths.embedModelPath = modelEntry.path; // Optional hint; never an enable switch.
const checkedEntries = options.verifyEmbedding ? manifest.files : manifest.files.filter(entry => !entry.path.toLowerCase().endsWith('.gguf'));
process.stdout.write(`正在校验 ${checkedEntries.length} 个基础依赖文件（embedding 默认关闭）…\n`);
const canonicalRoot = await realpath(root);
const verifyEntry = async (entry, base = root, canonicalBase = canonicalRoot) => {
  const filename = path.resolve(base, entry.path);
  if (typeof entry.path !== 'string' || !inside(base, filename) || path.isAbsolute(entry.path)) throw new Error('清单包含无效路径。');
  const resolved = await realpath(filename);
  if (!inside(canonicalBase, resolved)) throw new Error('依赖链接离开了离线包。');
  const info = await stat(filename);
  if (!info.isFile() || info.size !== entry.bytes || await digest(filename) !== entry.sha256) throw new Error(`文件校验失败：${entry.path}`);
};
for (let index = 0; index < checkedEntries.length; index += 32) {
  await Promise.all(checkedEntries.slice(index, index + 32).map(entry => verifyEntry(entry)));
  if (index && index % 4992 === 0) process.stdout.write(`已校验 ${index} / ${checkedEntries.length} 个文件…\n`);
}
let pageindexAddon = false;
const addonRoot = path.join(root, 'addon-pageindex');
try {
  await stat(addonRoot);
  const addon = JSON.parse(await readFile(path.join(addonRoot, 'CONTENTS.json'), 'utf8'));
  if (addon.schema !== 'sim-advisor-offline-addon/v1' || !Array.isArray(addon.files)) throw new Error('PageIndex 扩展清单无效。');
  if (await digest(path.join(root, 'manifests/python/manifest-minimal.json')) !== addon.requires_base_python_manifest_sha256) throw new Error('PageIndex 扩展与基础依赖版本不匹配，请使用配套扩展。');
  const canonicalAddon = await realpath(addonRoot);
  if (!inside(canonicalRoot, canonicalAddon)) throw new Error('PageIndex 扩展必须位于基础包内。');
  process.stdout.write(`正在校验 PageIndex 可选扩展 ${addon.files.length} 个文件…\n`);
  for (let index = 0; index < addon.files.length; index += 32) await Promise.all(addon.files.slice(index, index + 32).map(entry => verifyEntry(entry, addonRoot, canonicalAddon)));
  pageindexAddon = true;
  relativePaths.pageindexPython = relativePaths.pythonPath;
} catch (error) {
  if (error.code !== 'ENOENT' || await stat(addonRoot).then(() => true, () => false)) throw error;
}
for (const [key, value] of Object.entries(relativePaths)) if (key !== 'embedModelPath') await stat(path.join(root, value));
const externalPaths = {};
for (const key of ['comsolHome', 'docRoot', 'applications']) {
  if (options[key]) {
    const value = await realpath(path.resolve(options[key]));
    if (!(await stat(value)).isDirectory()) throw new Error(`${key} 必须为目录。`);
    externalPaths[key] = value;
  }
}
const environment = { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8',
  LITELLM_LOCAL_MODEL_COST_MAP: 'True', QMD_FORCE_CPU: '1', QMD_LLAMA_GPU: 'false', NODE_LLAMA_CPP_SKIP_DOWNLOAD: 'true' };
delete environment.PYTHONPATH; delete environment.PYTHONHOME; delete environment.PYTHONUSERBASE;
environment.PYTHONNOUSERSITE = '1';
function run(command, args, timeout = 120000) {
  const output = spawnSync(command, args, { cwd: root, env: environment, windowsHide: true, shell: false, encoding: 'utf8', timeout, maxBuffer: 4 * 1024 * 1024 });
  if (output.status !== 0 || output.error) throw new Error(`依赖检查未通过：${path.basename(command)}，请查看包内验证说明；未运行网络安装。`);
  return output.stdout.trim();
}
process.stdout.write('正在检查单一 Python 最小依赖环境…\n');
const pythonProbe = String.raw`import sys,json,sqlite3,importlib,importlib.util,importlib.metadata as m,contextlib
def deny(event,args):
 if event in ('socket.connect','socket.getaddrinfo'):raise RuntimeError('offline verification blocks network')
sys.addaudithook(deny)
names=json.loads(sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
 for name in names:importlib.import_module(name)
for name in json.loads(sys.argv[2]):
 assert importlib.util.find_spec(name) is None, 'unexpected optional package: '+name
db=sqlite3.connect(':memory:');db.execute('CREATE VIRTUAL TABLE probe USING fts5(body)');db.close()
print(json.dumps({'version':list(sys.version_info[:3]),'modules':names,'prefix':sys.prefix}))`;
const modules = ['sim', 'simparse', 'gmsh', 'pypdf', 'mcp.server.lowlevel', 'httpx', 'anyio'];
if (pageindexAddon) modules.push('pageindex', 'agents', 'litellm', 'pypdfium2');
const absent = pageindexAddon ? ['pytest', 'torch', 'pandas', 'numpy'] : ['pageindex', 'agents', 'litellm', 'botocore', 'boto3', 'pytest', 'torch', 'pandas', 'numpy'];
const core = JSON.parse(run(path.join(root, relativePaths.pythonPath), ['-I', '-B', '-c', pythonProbe, JSON.stringify(modules), JSON.stringify(absent)]));
if (core.version[0] !== 3 || core.version[1] !== 12) throw new Error('Python 版本与离线包不符。');
process.stdout.write(options.verifyEmbedding ? '正在显式验证 CPU embedding/向量检索（禁用网络）…\n' : '正在验证 QMD 关键词检索（禁用网络；不加载 embedding）…\n');
const checkRoot = path.join(dataDir, 'offline-checks', randomUUID());
await mkdir(path.dirname(checkRoot), { recursive: true });
const qmdReport = checkRoot + '.json';
try {
  run(process.execPath, [verifier, '--qmd-dir', path.join(root, relativePaths.qmdDir),
    ...(options.verifyEmbedding ? ['--model', path.join(root, relativePaths.embedModelPath)] : ['--keyword-only']),
    '--work-dir', checkRoot, '--report', qmdReport], 300000);
} catch {
  throw new Error(`QMD 离线检查未通过，记录：${qmdReport}。若提示缺少 MSVCP/VCRUNTIME，请交互运行 prerequisites/vc_redist.x64.exe 后重试；配置脚本不会自动安装系统组件。`);
}
const qmd = JSON.parse(await readFile(qmdReport, 'utf8'));
if (!qmd.ok || qmd.network.attempts.length || (options.verifyEmbedding
  ? qmd.cpu?.gpu !== false || qmd.embedding_dimensions <= 0
  : qmd.keyword_only !== true || !Array.isArray(qmd.lexical) || !qmd.lexical.length)) throw new Error(`QMD 离线检查未通过，记录：${qmdReport}`);
if (options.verifyOnly) {
  console.log(JSON.stringify({ verified: true, configured: false, platform: 'win32-x64', core, pageindexAddon, qmdReport }, null, 2));
} else {
  const modelHash = options.verifyEmbedding ? modelEntry.sha256 : null;
  if (options.verifyEmbedding) {
  const cache = path.join(dataDir, 'cache/library-search');
  const model = path.join(root, relativePaths.embedModelPath);
  const modelInfo = await stat(model);
  // Python will refresh the nanosecond timestamp/hash on first read; no JS rounding of mtime_ns.
  const receiptName = 'embedding-' + createHash('sha256').update(embedUri).digest('hex').slice(0, 16) + '.json';
  await atomicJSON(path.join(cache, receiptName), { uri: embedUri, path: model, sha256: modelHash, size: modelInfo.size,
    mtime_ns: 0, available: true, runtime: 'CPU / node-llama-cpp' });
  }
  const profile = { schema, bundleRoot: await realpath(root), paths: relativePaths, externalPaths,
    manifestSha256: createHash('sha256').update(manifestText).digest('hex'), configuredAt: new Date().toISOString() };
  await atomicJSON(path.join(dataDir, 'dependency-profile.json'), profile);
  await atomicJSON(path.join(dataDir, 'offline-verification.json'), { verified: true, bundleRoot: profile.bundleRoot,
    checkedFiles: checkedEntries.length, embeddingVerified: Boolean(options.verifyEmbedding), modelSha256: modelHash,
    core, singlePython: true, pageindexAddon, qmdReport, configuredAt: profile.configuredAt });
  process.stdout.write(`配置完成：${path.join(dataDir, 'dependency-profile.json')}\n`);
  process.stdout.write('在 DSH 安装工作台插件后调用 start_workbench。embedding 默认关闭，只能在检索控制页手动启用。已有服务请先停止再启动。\n');
  process.stdout.write('COMSOL、手册和案例目录可通过参数指定；云端规划模型仍需要联网。\n');
}
