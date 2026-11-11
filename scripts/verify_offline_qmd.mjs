#!/usr/bin/env node
/** Offline keyword SDK smoke, with an explicit optional CPU embedding probe. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import {createRequire, syncBuiltinESMExports} from 'node:module';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import net from 'node:net';
import tls from 'node:tls';

const args = {};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i];
  if (key === '--help') {
    console.log('node verify_offline_qmd.mjs --qmd-dir SDK_DIR (--keyword-only | --model LOCAL_GGUF) --work-dir EMPTY_DIR --report REPORT_JSON');
    process.exit(0);
  }
  if (key === '--keyword-only') { args.keywordOnly = true; continue; }
  if (!['--qmd-dir', '--model', '--work-dir', '--report'].includes(key) || !process.argv[i + 1]) throw new Error(`Unknown/incomplete argument: ${key}`);
  args[key.slice(2)] = path.resolve(process.argv[++i]);
}
for (const key of ['qmd-dir', 'work-dir', 'report', ...(args.keywordOnly ? [] : ['model'])]) if (!args[key]) throw new Error(`Missing --${key}`);
const began = performance.now();
const result = {ok: false, started_at: new Date().toISOString(), fixture_kind: 'synthetic dependency smoke; not model/case validation', runtime: {node: process.version, modules: process.versions.modules, platform: process.platform, arch: process.arch}, qmd_dir: args['qmd-dir'], network: {policy: 'deny global fetch, http/https/http2 and net/tls socket connect', attempts: []}, stages: []};
const timer = setTimeout(() => { result.error = 'Native smoke exceeded 300 seconds'; finish(1); }, 300000);
function finish(code) {
  clearTimeout(timer);
  result.elapsed_ms = Math.round(performance.now() - began);
  fs.mkdirSync(path.dirname(args.report), {recursive: true});
  fs.writeFileSync(args.report, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify({ok: result.ok, report: args.report, elapsed_ms: result.elapsed_ms, error: result.error}));
  process.exit(code);
}
function deny(api) {
  return (...values) => { result.network.attempts.push({api, target: String(values[0]).slice(0, 300), stack: new Error().stack, at_ms: Math.round(performance.now() - began)}); throw new Error(`Network is disabled: ${api}`); };
}
globalThis.fetch = async (...args) => deny('fetch')(...args);
http.request = deny('http.request'); http.get = deny('http.get');
https.request = deny('https.request'); https.get = deny('https.get');
http2.connect = deny('http2.connect');
net.connect = deny('net.connect'); net.createConnection = deny('net.createConnection'); net.Socket.prototype.connect = deny('net.Socket.connect'); tls.connect = deny('tls.connect'); syncBuiltinESMExports();
let store;
async function stage(name, fn) {
  const t = performance.now(); console.error(`[offline-qmd] ${name}`);
  const value = await fn(); result.stages.push({name, elapsed_ms: Math.round(performance.now() - t)}); return value;
}
try {
  assert.equal(process.platform, 'win32'); assert.equal(process.arch, 'x64');
  const work = args['work-dir'];
  if (fs.existsSync(work) && fs.readdirSync(work).length) throw new Error('--work-dir must be empty (no original corpus/index is modified)');
  fs.mkdirSync(work, {recursive: true});
  for (const [key, value] of Object.entries({QMD_LLAMA_GPU: 'off', QMD_FORCE_CPU: '1', QMD_EMBED_PARALLELISM: '1', QMD_EMBED_MODEL: args.model, NODE_LLAMA_CPP_SKIP_DOWNLOAD: 'true', HF_HUB_OFFLINE: '1', XDG_CACHE_HOME: path.join(work, 'cache'), XDG_CONFIG_HOME: path.join(work, 'config'), XDG_DATA_HOME: path.join(work, 'data')})) process.env[key] = value;
  if (!args.model) delete process.env.QMD_EMBED_MODEL;
  result.keyword_only = Boolean(args.keywordOnly);
  if (!args.keywordOnly) {
  const modelStat = fs.statSync(args.model);
  result.model = await stage('verify local model', async () => {
    const hash = crypto.createHash('sha256');
    for await (const chunk of fs.createReadStream(args.model)) hash.update(chunk);
    const info = {bytes: modelStat.size, sha256: hash.digest('hex'), uri: pathToFileURL(args.model).href};
    assert.equal(info.bytes, 333590944, 'Expected the supplied EmbeddingGemma Q8_0 file');
    assert.equal(info.sha256, 'b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63');
    return info;
  });
  const requireQmd = createRequire(path.join(args['qmd-dir'], 'package.json'));
  const llamaEntry = requireQmd.resolve('node-llama-cpp');
  // Isolate the explicit binding probe so its disposal cannot influence QMD's
  // own native-addon and model lifecycle in the actual SDK test process.
  result.cpu = await stage('load packaged CPU binding (never build/download)', async () => {
    const code = `
      import assert from 'node:assert/strict';
      import http from 'node:http'; import https from 'node:https'; import http2 from 'node:http2'; import net from 'node:net'; import tls from 'node:tls';
      import {syncBuiltinESMExports} from 'node:module';
      const attempts=[];
      const deny=(api)=>(...args)=>{attempts.push(api);throw Error('Network disabled: '+api)};
      globalThis.fetch=async(...args)=>deny('fetch')(...args);
      http.request=deny('http.request');http.get=deny('http.get');
      https.request=deny('https.request');https.get=deny('https.get');http2.connect=deny('http2.connect');net.connect=deny('net.connect');net.createConnection=deny('net.createConnection');net.Socket.prototype.connect=deny('net.Socket.connect');tls.connect=deny('tls.connect');syncBuiltinESMExports();
      const {getLlama,LlamaGrammar}=await import(${JSON.stringify(pathToFileURL(llamaEntry).href)});
      const llama=await getLlama({gpu:false,build:'never',skipDownload:true,progressLogs:false});
      const grammar=await LlamaGrammar.getFor(llama,'json');
      const info={gpu:llama.gpu,supports_gpu_offloading:llama.supportsGpuOffloading,system_info:llama.systemInfo,cmake_options:llama.cmakeOptions,json_grammar_loaded:grammar.constructor.name,network_attempts:attempts};
      assert.equal(llama.gpu,false);assert.equal(attempts.length,0);
      await llama.dispose(); console.log('CPU_PROBE='+JSON.stringify(info)); process.exit(0);
    `;
    const stdout = execFileSync(process.execPath, ['--input-type=module', '-e', code], {env: process.env, encoding: 'utf8', timeout: 45000, maxBuffer: 2 * 1024 * 1024, windowsHide: true});
    const line = stdout.split(/\r?\n/).find(s => s.startsWith('CPU_PROBE='));
    if (!line) throw new Error('Native CPU probe returned no result');
    return JSON.parse(line.slice('CPU_PROBE='.length));
  });
  assert.equal(result.cpu.gpu, false, 'CPU prebuilt must load without GPU fallback');
  }
  result.ast = await stage('load and parse every packaged WASM grammar', async () => {
    const {getASTStatus, getASTBreakPoints} = await import(pathToFileURL(path.join(args['qmd-dir'], 'dist/ast.js')).href);
    const status = await getASTStatus();
    assert.ok(status.available && status.languages.every(language => language.available), JSON.stringify(status));
    const samples = {
      'smoke.ts': 'export function square(x: number): number { return x * x; }\n',
      'smoke.tsx': 'export function View() { return <div>Hello</div>; }\n',
      'smoke.js': 'export function square(x) { return x * x; }\n',
      'smoke.py': 'def square(x):\n    return x * x\n',
      'smoke.go': 'package main\nfunc square(x int) int { return x * x }\n',
      'smoke.rs': 'pub fn square(x: i32) -> i32 { x * x }\n',
    };
    const parses = [];
    for (const [file, code] of Object.entries(samples)) {
      const points = await getASTBreakPoints(code, file);
      assert.ok(points.length > 0, 'Expected actual AST break points: ' + file);
      parses.push({file, points});
    }
    return {status, parses};
  });
  const {createStore} = await import(pathToFileURL(path.join(args['qmd-dir'], 'dist/index.js')).href);
  const corpus = path.join(work, 'corpus'); fs.mkdirSync(corpus);
  const fixtures = {
    'thermal.md': '# Transient heat and thermal expansion\nHeat conduction predicts the temperature field following a brief energy pulse. The coefficient of thermal expansion converts temperature changes into strain. Constraints generate displacement and stress. Preserve the conduction path and validate the temperature peak and restrained deformation.\n',
    'fluid.md': '# Laminar internal flow\nAn incompressible Newtonian fluid obeys mass conservation and the stationary Navier Stokes equations. Hydraulic resistance relates pressure drop to volumetric rate between inlet and outlet boundaries. Evaluate velocity distribution, viscous dissipation and pressure difference in the lumen.\n',
  };
  for (const [file, text] of Object.entries(fixtures)) fs.writeFileSync(path.join(corpus, file), text);
  result.fixtures = Object.entries(fixtures).map(([file, text]) => ({file, text, sha256: crypto.createHash('sha256').update(text).digest('hex')}));
  // The upstream SDK accepts native absolute paths; a file: URI is only provenance.
  // node-llama-cpp 3.20.0 does not convert file: URIs before fs.pathExists().
  store = await createStore({dbPath: path.join(work, 'index.sqlite'), config: {collections: {smoke: {path: corpus, pattern: '*.md'}}, ...(args.keywordOnly ? {} : {models: {embed: args.model}})}});
  result.native_progress = [];
  for (const method of ['ensureLlama', 'resolveModel', 'ensureEmbedModel', 'ensureEmbedContexts']) {
    const original = store.internal.llm[method].bind(store.internal.llm);
    store.internal.llm[method] = async (...values) => {
      if (args.keywordOnly) throw new Error('Keyword-only verification must not initialize or resolve an embedding model: ' + method);
      const entry = {method, start_ms: Math.round(performance.now() - began)};
      result.native_progress.push(entry); console.error('[offline-qmd] native ' + method);
      try { const value = await original(...values); entry.end_ms = Math.round(performance.now() - began); return value; }
      catch (error) { entry.error = String(error); throw error; }
    };
  }
  result.update = await stage('createStore.update', () => store.update());
  result.lexical = await stage('searchLex', () => store.searchLex('hydraulic resistance', {collection: 'smoke', limit: 2}));
  assert.match(result.lexical[0]?.filepath || '', /fluid\.md$/);
  if (args.keywordOnly) {
    assert.equal(result.network.attempts.length, 0);
    assert.equal(result.native_progress.length, 0);
    result.embedding_enabled = false;
    result.status = await store.getStatus();
    await store.close(); store = null;
    result.ok = true;
    finish(0);
  }
  result.embedding = await stage('embed local GGUF on CPU', () => store.embed({model: args.model, maxDocsPerBatch: 2, maxBatchBytes: 65536, chunkStrategy: 'regex', onProgress: info => console.error('[offline-qmd] embedding ' + JSON.stringify(info))}));
  result.embedding_rows = store.internal.db.prepare('SELECT model,embed_fingerprint,count(*) AS chunks FROM content_vectors GROUP BY model,embed_fingerprint').all();
  const schema = store.internal.db.prepare("SELECT sql FROM sqlite_master WHERE name='vectors_vec'").get()?.sql || '';
  result.embedding_dimensions = Number(schema.match(/float\s*\[(\d+)\]/i)?.[1] || 0);
  assert.ok(result.embedding_dimensions > 0); assert.ok(result.embedding_rows.reduce((n, r) => n + r.chunks, 0) >= 2);
  const queries = [
    {query: 'Estimate the pumping power needed to push water along a confined passage', expected: 'fluid.md'},
    {query: 'Will a short heating event cause the constrained component to grow and develop mechanical forces?', expected: 'thermal.md'},
  ];
  result.vector = [];
  for (const test of queries) {
    const lexical = await store.searchLex(test.query, {collection: 'smoke', limit: 2});
    const hits = await stage('searchVector: ' + test.expected, () => store.searchVector(test.query, {collection: 'smoke', limit: 2}));
    assert.match(hits[0]?.filepath || '', new RegExp(test.expected.replace('.', '\\.') + '$'));
    assert.ok(hits.length > 0);
    result.vector.push({...test, lexical_hits_for_same_query: lexical.length, hits});
  }
  assert.equal(result.vector[0].lexical_hits_for_same_query, 0, 'The first semantic query must demonstrate a hit beyond lexical matches');
  assert.equal(result.network.attempts.length, 0, 'No network access may even be attempted');
  result.status = await store.getStatus();
  await store.close(); store = null;
  result.ok = true;
} catch (error) {
  result.error = error.stack || String(error);
  try { if (store) await store.close(); } catch (cleanup) { result.cleanup_error = String(cleanup); }
}
finish(result.ok ? 0 : 1);
