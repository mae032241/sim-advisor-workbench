#!/usr/bin/env node
/** Build a relocatable Windows x64 CPU QMD dependency closure, without npm/install. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const defaults = {
  source: path.join(appRoot, '.tools/qmd'),
  out: path.join(appRoot, 'data/offline-build/qmd'),
  'node-home': 'D:/software/dsh/resources/runtime/primary-runtime/dependencies/node',
};
const args = {...defaults};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i];
  if (key === '--inspect') args.inspect = true;
  else if (key === '--help') {
    console.log('node build_offline_qmd.mjs [--source QMD_INSTALL] [--out EMPTY_DIR] [--node-home NODE_DISTRIBUTION] [--inspect]');
    process.exit(0);
  } else if (['--source', '--out', '--node-home'].includes(key) && process.argv[i + 1]) args[key.slice(2)] = process.argv[++i];
  else throw new Error(`Unknown/incomplete argument: ${key}`);
}
const source = fs.realpathSync(path.resolve(args.source));
const sourceModules = fs.realpathSync(path.join(source, 'node_modules'));
const out = path.resolve(args.out);
function within(root, item) { const rel = path.relative(root, item); return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)); }
if (within(source, out) || within(out, source) || out === path.parse(out).root) throw new Error('Output must be isolated from the source installation.');
const slash = p => p.split(path.sep).join('/');
const json = p => JSON.parse(fs.readFileSync(p, 'utf8'));
const sha = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
function findDependency(owner, name) {
  const req = createRequire(path.join(owner, 'package.json'));
  for (const modules of req.resolve.paths(name) || []) {
    const candidate = path.join(modules, name, 'package.json');
    if (fs.existsSync(candidate)) {
      const root = fs.realpathSync(path.dirname(candidate));
      if (!within(sourceModules, root)) throw new Error(`Dependency ${name} escapes the allowed installation: ${root}`);
      if (json(path.join(root, 'package.json')).name !== name) throw new Error(`Package identity mismatch: ${name}`);
      return root;
    }
  }
  return null;
}
function exclusion(name, pkg = {}) {
  if (name.startsWith('@node-llama-cpp/') && name !== '@node-llama-cpp/win-x64') return 'CPU-only Windows x64: excluded optional GPU/other-platform llama prebuilt';
  if (name.startsWith('sqlite-vec-') && name !== 'sqlite-vec-windows-x64') return 'Windows x64: excluded optional other-platform SQLite extension';
  for (const [key, target] of [['os', 'win32'], ['cpu', 'x64']]) {
    const supported = pkg[key];
    if (Array.isArray(supported) && (supported.includes('!' + target) || (supported.some(v => !v.startsWith('!')) && !supported.includes(target)))) return `Package ${key} excludes ${target}`;
  }
  return null;
}
const nodes = new Map(), queue = [], excluded = [], missing = [];
function add(root) {
  if (!nodes.has(root)) {
    const pkg = json(path.join(root, 'package.json'));
    const node = {root, pkg, id: `p${nodes.size + 1}`, edges: []};
    nodes.set(root, node); queue.push(node);
  }
  return nodes.get(root);
}
const qmdRoot = findDependency(source, '@tobilu/qmd');
if (!qmdRoot) throw new Error('Installed @tobilu/qmd package not found');
const entry = add(qmdRoot);
for (let cursor = 0; cursor < queue.length; cursor++) {
  const owner = queue[cursor], p = owner.pkg;
  const names = [...new Set([...Object.keys(p.dependencies || {}), ...Object.keys(p.optionalDependencies || {}), ...Object.keys(p.peerDependencies || {})])].sort();
  for (const name of names) {
    const kind = name in (p.optionalDependencies || {}) ? 'optional' : name in (p.dependencies || {}) ? 'dependency' : 'peer';
    const optional = kind === 'optional' || (kind === 'peer' && p.peerDependenciesMeta?.[name]?.optional === true);
    let reason = exclusion(name);
    if (reason) {
      if (!optional) throw new Error(`Cannot omit required ${name}: ${reason}`);
      excluded.push({from: owner.id, name, reason}); continue;
    }
    const root = findDependency(owner.root, name);
    if (!root) {
      if (!optional) throw new Error(`Missing required ${kind} ${name} for ${p.name}@${p.version}`);
      missing.push({from: owner.id, name, kind, reason: 'Not installed; optional dependency/peer'}); continue;
    }
    const depPkg = json(path.join(root, 'package.json'));
    reason = exclusion(name, depPkg);
    if (reason) {
      if (!optional) throw new Error(`Cannot omit required ${name}: ${reason}`);
      excluded.push({from: owner.id, name, reason}); continue;
    }
    const dep = add(root);
    owner.edges.push({name, to: dep.id, kind});
  }
}
const byId = new Map(queue.map(n => [n.id, n]));
const report = {format: 1, target: {platform: 'win32', arch: 'x64', gpu: false}, entry: {name: entry.pkg.name, version: entry.pkg.version}, packages: queue.map(n => ({id: n.id, name: n.pkg.name, version: n.pkg.version, license: n.pkg.license ?? null, dependencies: n.edges})), excluded_optional_packages: excluded, unavailable_optional_packages: missing};
if (args.inspect) { console.log(JSON.stringify(report, null, 2)); process.exit(0); }
if (fs.existsSync(out) && fs.readdirSync(out).length) throw new Error(`Output is not empty; use a new directory (no automatic deletion): ${out}`);
fs.mkdirSync(out, {recursive: true});
fs.writeFileSync(path.join(out, '.BUILDING'), JSON.stringify({pid: process.pid, at: new Date().toISOString()}));
const omittedFiles = [], dereferenced = [], instances = new Map(), pending = [];
function copyTree(src, dest, pkg, rel = '', ancestors = new Set()) {
  const real = fs.realpathSync(src);
  if (!within(sourceModules, real)) throw new Error(`Source file link escapes package installation: ${src}`);
  const stat = fs.statSync(src);
  if (fs.lstatSync(src).isSymbolicLink()) dereferenced.push({package: pkg.id, path: slash(rel)});
  if (stat.isDirectory()) {
    if (ancestors.has(real)) throw new Error(`Directory link cycle: ${src}`);
    const seen = new Set(ancestors); seen.add(real);
    fs.mkdirSync(dest, {recursive: true});
    for (const name of fs.readdirSync(src).sort()) {
      if (name === 'node_modules') continue;
      copyTree(path.join(src, name), path.join(dest, name), pkg, path.join(rel, name), seen);
    }
  } else if (stat.isFile()) {
    // Some packages publish all platforms together. Only native binaries under
    // an explicitly named other prebuild platform are omitted; no generic deps pruned.
    const parts = slash(rel).split('/');
    const platformPart = parts[0] === 'prebuilds' ? parts[1] : null;
    if (platformPart && /^(?:darwin|linux|linuxmusl|win32)-(?:arm64|x64|arm|ia32)(?:[./-]|$)/.test(platformPart) && !/^win32-x64(?:[./-]|$)/.test(platformPart) && /\.(?:node|dll|so|dylib)$/i.test(rel)) {
      omittedFiles.push({package: pkg.id, path: slash(rel), reason: 'Explicit other-platform native prebuilt binary'}); return;
    }
    fs.copyFileSync(src, dest);
  } else throw new Error(`Unsupported special file: ${src}`);
}
function install(node, dest) {
  if (instances.has(dest)) {
    if (instances.get(dest) !== node.id) throw new Error(`Physical package conflict at ${dest}`);
    return;
  }
  if (dest.length > 230) throw new Error(`Dependency nesting is too deep for a safely portable Windows path: ${dest}`);
  copyTree(node.root, dest, node);
  instances.set(dest, node.id); pending.push({node, dest});
}
// A normal flat Node tree, adding nested copies only for actual version/peer conflicts.
for (const node of queue) {
  const dest = path.join(out, 'node_modules', node.pkg.name);
  if (!instances.has(dest)) install(node, dest);
}
for (let cursor = 0; cursor < pending.length; cursor++) {
  const {node, dest} = pending[cursor];
  for (const edge of node.edges) {
    const lookup = createRequire(path.join(dest, 'package.json')).resolve.paths(edge.name) || [];
    const nearest = lookup.map(p => path.join(p, edge.name)).find(p => instances.has(p));
    if (nearest && instances.get(nearest) === edge.to) continue;
    install(byId.get(edge.to), path.join(dest, 'node_modules', edge.name));
  }
}
// Recheck every dependency from every physical package instance, without using source paths.
for (const {node, dest} of pending) for (const edge of node.edges) {
  const lookup = createRequire(path.join(dest, 'package.json')).resolve.paths(edge.name) || [];
  const actual = lookup.map(p => path.join(p, edge.name)).find(p => fs.existsSync(path.join(p, 'package.json')));
  if (!actual || instances.get(actual) !== edge.to) throw new Error(`Unresolved relocated edge: ${node.pkg.name} -> ${edge.name}`);
}
const nodeHome = path.resolve(args['node-home']);
const nodeExe = path.join(nodeHome, 'bin/node.exe'), nodeLicense = path.join(nodeHome, 'LICENSE');
if (!fs.existsSync(nodeExe) || !fs.existsSync(nodeLicense)) throw new Error('Node distribution must contain bin/node.exe and LICENSE');
const nodeInfo = JSON.parse(execFileSync(nodeExe, ['-p', 'JSON.stringify({node:process.version,modules:process.versions.modules,napi:process.versions.napi,platform:process.platform,arch:process.arch})'], {encoding: 'utf8', timeout: 10000, windowsHide: true}));
if (nodeInfo.platform !== 'win32' || nodeInfo.arch !== 'x64' || !nodeInfo.node.startsWith('v24.')) throw new Error('Expected the supplied independent Windows x64 Node 24 runtime');
fs.mkdirSync(path.join(out, 'node'), {recursive: true});
fs.copyFileSync(nodeExe, path.join(out, 'node/node.exe'));
fs.copyFileSync(nodeLicense, path.join(out, 'node/LICENSE'));
fs.writeFileSync(path.join(out, 'package.json'), JSON.stringify({name: 'sim-advisor-offline-qmd', private: true, type: 'module', dependencies: {'@tobilu/qmd': entry.pkg.version}}, null, 2) + '\n');
const files = [], licenses = [], natives = [];
function inventory(dir) {
  for (const name of fs.readdirSync(dir).sort()) {
    const abs = path.join(dir, name), stat = fs.lstatSync(abs), rel = slash(path.relative(out, abs));
    if (stat.isSymbolicLink()) throw new Error(`Output must not contain links: ${rel}`);
    if (stat.isDirectory()) inventory(abs);
    else if (stat.isFile() && name !== '.BUILDING') {
      const file = {path: rel, bytes: stat.size, sha256: sha(abs)}; files.push(file);
      if (/(?:license|licence|notice|copying|copyright)/i.test(name)) licenses.push(file);
      if (/\.(?:node|dll|exe|so|dylib)$/i.test(name)) natives.push(file);
    }
  }
}
inventory(out);
const manifest = {...report, built_at: new Date().toISOString(), builder_sha256: sha(fileURLToPath(import.meta.url)), node: nodeInfo, physical_packages: [...instances].map(([dest, id]) => ({id, path: slash(path.relative(out, dest))})), dereferenced_links: dereferenced, omitted_files: omittedFiles, native_files: natives, total_bytes: files.reduce((n, f) => n + f.bytes, 0), files};
fs.writeFileSync(path.join(out, 'LICENSES.json'), JSON.stringify({packages: report.packages.map(({id, name, version, license}) => ({id, name, version, license})), files: licenses, note: 'Original package contents and license/notice files are retained. SPDX metadata is inventory, not a replacement for full license terms.'}, null, 2) + '\n');
fs.writeFileSync(path.join(out, 'MANIFEST.json'), JSON.stringify(manifest, null, 2) + '\n');
fs.unlinkSync(path.join(out, '.BUILDING'));
console.log(JSON.stringify({out, packages: queue.length, physical_packages: instances.size, files: files.length, bytes: manifest.total_bytes, native_files: natives.length, excluded_optional_packages: excluded.length, node: nodeInfo}));
