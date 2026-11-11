/** Read only the workbench-owned offline dependency profile, never DSH settings. */
import { readFile, realpath, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';

const PATHS = { pythonPath: 'file', simPython: 'file', pageindexPython: 'file', mcpPythonPath: 'file',
  nodePath: 'file', qmdDir: 'directory', embedModelPath: 'file' };
const EXTERNAL = ['comsolHome', 'docRoot', 'applications'];
const inside = (root, target) => { const relative = path.relative(root, target); return relative === '' || (!relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative)); };
const object = value => value && typeof value === 'object' && !Array.isArray(value);

export async function loadDependencyProfile(dataDir) {
  const filename = path.join(dataDir, 'dependency-profile.json');
  let raw;
  try { raw = await readFile(filename, 'utf8'); }
  catch (error) { if (error.code === 'ENOENT') return { options: {}, status: {} }; throw new Error('无法读取离线依赖配置。'); }
  let profile;
  try { profile = JSON.parse(raw); } catch { throw new Error('离线依赖配置不是有效 JSON，请重新运行 configure-offline.ps1。'); }
  if (!object(profile) || profile.schema !== 'sim-advisor-offline-dependencies/v1'
      || typeof profile.bundleRoot !== 'string' || !path.isAbsolute(profile.bundleRoot)
      || !object(profile.paths) || Object.keys(profile.paths).some(key => !Object.hasOwn(PATHS, key))
      || (profile.externalPaths !== undefined && (!object(profile.externalPaths) || Object.keys(profile.externalPaths).some(key => !EXTERNAL.includes(key))))) {
    throw new Error('离线依赖配置版本或字段无效，请重新运行 configure-offline.ps1。');
  }
  let bundleRoot;
  try { bundleRoot = await realpath(profile.bundleRoot); }
  catch { throw new Error('离线依赖包目录已移动或不可访问，请在新目录重新运行 configure-offline.ps1。'); }
  if (profile.manifestSha256) {
    if (!/^[a-f0-9]{64}$/.test(profile.manifestSha256)) throw new Error('离线依赖清单校验值无效。');
    let digest;
    try { digest = createHash('sha256').update(await readFile(path.join(bundleRoot, 'CONTENTS.json'))).digest('hex'); }
    catch { throw new Error('离线依赖清单缺失，请重新解压并配置。'); }
    if (digest !== profile.manifestSha256) throw new Error('离线依赖清单已变化，请重新运行 configure-offline.ps1 校验。');
  }
  const options = {};
  for (const [key, value] of Object.entries(profile.paths)) {
    if (typeof value !== 'string' || !value || path.isAbsolute(value) || /[\0\r\n]/.test(value)) throw new Error('离线依赖必须使用包内相对路径。');
    const target = path.resolve(bundleRoot, value);
    if (!inside(bundleRoot, target)) throw new Error('离线依赖路径不能离开解压目录。');
    // A legacy weight path is only a hint for explicit manual enablement.
    // Ordinary startup must not resolve/stat/hash a GGUF while disabled.
    if (key === 'embedModelPath') { options[key] = target; continue; }
    let actual, info;
    try { actual = await realpath(target); info = await stat(actual); }
    catch { throw new Error(`离线依赖 ${key} 缺失，请重新解压并配置。`); }
    if (!inside(bundleRoot, actual) || !(PATHS[key] === 'directory' ? info.isDirectory() : info.isFile())) throw new Error(`离线依赖 ${key} 路径无效。`);
    options[key] = actual;
  }
  for (const [key, value] of Object.entries(profile.externalPaths ?? {})) {
    if (typeof value !== 'string' || !path.isAbsolute(value) || /[\0\r\n]/.test(value)) throw new Error('COMSOL 与资料目录必须是明确的本机绝对路径。');
    options[key] = value;
  }
  return { options, status: { dependency_profile: filename, dependency_bundle: bundleRoot,
    mcp_python: options.mcpPythonPath, embedding_model: options.embedModelPath } };
}
