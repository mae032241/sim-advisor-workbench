#!/usr/bin/env node
/** Derive a CPU runtime payload from the verified closure; never alter its source. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = {source: path.join(appRoot, 'data/offline-build/qmd'), out: path.join(appRoot, 'data/offline-build/qmd-minimal')};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i];
  if (key === '--inspect') args.inspect = true;
  else if (key === '--help') { console.log('node minimize_offline_qmd.mjs [--source VERIFIED_QMD] [--out EMPTY_DIR] [--inspect]'); process.exit(0); }
  else if (['--source', '--out'].includes(key) && process.argv[i + 1]) args[key.slice(2)] = process.argv[++i];
  else throw new Error(`Unknown/incomplete argument: ${key}`);
}
const source = fs.realpathSync(path.resolve(args.source)), out = path.resolve(args.out);
function within(root, item) { const rel = path.relative(root, item); return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)); }
if (within(source, out) || within(out, source) || out === path.parse(out).root) throw new Error('Output must be separate from the verified source');
const sha = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const manifestPath = path.join(source, 'MANIFEST.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
const licenseInfo = JSON.parse(fs.readFileSync(path.join(source, 'LICENSES.json'), 'utf8'));
const licenses = new Set(licenseInfo.files.map(f => f.path));
const rules = [
  {id: 'llama-cpp-source-bundle', reason: 'llama.cpp build source; CPU prebuilt loads with build=never/skipDownload=true', match: p => p === 'node_modules/node-llama-cpp/llama/gitRelease.bundle'},
  {id: 'javascript-debug-maps', reason: 'Optional debugger source maps; emitted JavaScript and type declarations remain', match: p => p.endsWith('.map')},
  {id: 'sqlite-build-sources', reason: 'better-sqlite3/lib/binding.js loads retained prebuilds/win32-x64.node; no source compilation in runtime payload', match: p => /^node_modules\/better-sqlite3\/(?:src\/|deps\/|binding\.gyp$)/.test(p)},
  {id: 'tree-sitter-build-sources', reason: 'QMD dist/ast.js loads packaged .wasm through web-tree-sitter; retained WASM/queries/native bindings do not load parser C sources', match: p => /^node_modules\/tree-sitter-(?:go|javascript|python|rust)\/src\//.test(p) || /^node_modules\/tree-sitter-typescript\/(?:tsx|typescript)\/src\//.test(p)},
];
const removed = [], retained = [];
for (const file of manifest.files) {
  const abs = path.resolve(source, file.path);
  if (!within(source, abs) || fs.lstatSync(abs).isSymbolicLink() || fs.statSync(abs).size !== file.bytes || sha(abs) !== file.sha256) throw new Error(`Unverified source file: ${file.path}`);
  const protectedFile = licenses.has(file.path) || /(?:license|licence|notice|copying|copyright)/i.test(path.basename(file.path)) || /\.(?:node|dll|exe|so|dylib|wasm)$/i.test(file.path);
  const rule = protectedFile ? null : rules.find(rule => rule.match(file.path));
  if (rule) removed.push({...file, rule: rule.id, reason: rule.reason});
  else retained.push(file);
}
const ruleStats = rules.map(rule => ({id: rule.id, reason: rule.reason, files: removed.filter(f => f.rule === rule.id).length, bytes: removed.filter(f => f.rule === rule.id).reduce((n, f) => n + f.bytes, 0)}));
const report = {source_manifest_sha256: sha(manifestPath), source_bytes: manifest.total_bytes, retained_bytes: retained.reduce((n, f) => n + f.bytes, 0), removed_bytes: removed.reduce((n, f) => n + f.bytes, 0), rules: ruleStats, excluded_from_pruning: ['Every production dependency/package.json/JavaScript entry remains', 'All TypeScript peer files except debug maps remain', 'All native binaries and WASM remain', 'All original licenses/notices remain', 'llama/grammars and release metadata remain', 'Unreviewed test directories remain'], removed_files: removed};
if (args.inspect) { console.log(JSON.stringify(report, null, 2)); process.exit(0); }
if (fs.existsSync(out) && fs.readdirSync(out).length) throw new Error('Output must be empty; no existing bundle is deleted');
fs.mkdirSync(out, {recursive: true});
fs.writeFileSync(path.join(out, '.BUILDING'), JSON.stringify({pid: process.pid, at: new Date().toISOString()}));
for (const file of retained) {
  const dest = path.resolve(out, file.path);
  if (!within(out, dest)) throw new Error('Manifest path escapes output');
  fs.mkdirSync(path.dirname(dest), {recursive: true}); fs.copyFileSync(path.join(source, file.path), dest);
}
for (const native of manifest.native_files) if (!retained.some(file => file.path === native.path && file.sha256 === native.sha256)) throw new Error(`Native file changed: ${native.path}`);
for (const license of licenseInfo.files) if (!retained.some(file => file.path === license.path && file.sha256 === license.sha256)) throw new Error(`License changed: ${license.path}`);
for (const pkg of manifest.physical_packages) if (!fs.existsSync(path.join(out, pkg.path, 'package.json'))) throw new Error(`Runtime package disappeared: ${pkg.path}`);
fs.copyFileSync(path.join(source, 'LICENSES.json'), path.join(out, 'LICENSES.json'));
const minimalManifest = {...manifest, built_at: new Date().toISOString(), original_manifest_sha256: report.source_manifest_sha256, minimizer_sha256: sha(fileURLToPath(import.meta.url)), variant: 'windows-x64-cpu-runtime-minimal', removed_build_or_debug_files: removed, total_bytes: report.retained_bytes, files: retained};
fs.writeFileSync(path.join(out, 'MANIFEST.json'), JSON.stringify(minimalManifest, null, 2) + '\n');
fs.writeFileSync(path.join(out, 'PRUNING.json'), JSON.stringify(report, null, 2) + '\n');
fs.unlinkSync(path.join(out, '.BUILDING'));
console.log(JSON.stringify({out, bytes: report.retained_bytes, removed_bytes: report.removed_bytes, removed_files: removed.length, retained_files: retained.length, retained_packages: manifest.packages.length, retained_native_files: manifest.native_files.length, rules: ruleStats}));
