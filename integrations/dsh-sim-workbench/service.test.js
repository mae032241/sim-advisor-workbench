import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { spawn } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { syncBuiltinESMExports } from 'node:module';
import { mkdtemp, mkdir, readFile, writeFile, rm, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { WorkbenchService } from './service.js';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const FAKE_PYTHON = String.raw`
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
const argv=process.argv.slice(2), data=process.env.SIM_ADVISOR_DATA;
if(argv[0]==='-c'){
 console.log(JSON.stringify({version:JSON.parse(process.env.TEST_VERSION||'[3,12,8]'),executable:process.execPath,fts5:process.env.TEST_FTS5!=='false',server_import:process.env.TEST_IMPORT!=='false'}));
 process.exit(0);
}
if(path.basename(argv[0])==='run.py'){
 if(process.env.TEST_START_FAIL){console.error('PermissionError: PRIVATE SECRET FILE');process.exit(2);}
 const server=http.createServer((req,res)=>{
  fs.appendFileSync(path.join(data,'http-requests.jsonl'),JSON.stringify(req.url)+'\n');
  res.setHeader('Content-Type','application/json');
  if(req.url==='/api/health'){
   res.end(JSON.stringify({ready:process.env.TEST_HEALTH_READY!=='false',pid:process.env.TEST_WRONG_HEALTH_PID?process.ppid:process.pid,
    data_dir:process.env.TEST_HEALTH_DATA||data,service:process.env.TEST_HEALTH_SERVICE||'sim-advisor-workbench',protocol_version:1}));return;
  }
  setTimeout(()=>res.end(JSON.stringify({ready:false,services:{},marker:'fake-workbench',large:process.env.TEST_LARGE_STATUS?'x'.repeat(3*1024*1024):''})),Number(process.env.TEST_STATUS_DELAY||0));
 });
 server.listen(0,'127.0.0.1',()=>{
  const metadata={url:'http://127.0.0.1:'+server.address().port,pid:process.env.TEST_WRONG_PID?process.ppid:process.pid};
  setTimeout(()=>fs.writeFileSync(path.join(data,'server.json'),JSON.stringify(metadata)),Number(process.env.TEST_READY_DELAY||0));
  fs.writeFileSync(path.join(data,'server-launch.json'),JSON.stringify({argv,cwd:process.cwd(),env:{data,backend:process.env.SIM_ADVISOR_AGENT_BACKEND,embedded:process.env.SIM_ADVISOR_DSH_EMBED,cache:process.env.SIM_ADVISOR_SEARCH_CACHE,noBytecode:process.env.PYTHONDONTWRITEBYTECODE,doc:process.env.COMSOL_DOC_ROOT,applications:process.env.COMSOL_APPLICATIONS,simPython:process.env.SIM_ADVISOR_SIM_PYTHON,qmd:process.env.SIM_ADVISOR_QMD,qmdDir:process.env.SIM_ADVISOR_QMD_DIR,node:process.env.SIM_ADVISOR_NODE,embedding:process.env.SIM_ADVISOR_EMBED_MODEL,pageindex:process.env.SIM_ADVISOR_PAGEINDEX_PYTHON,comsol:process.env.SIM_ADVISOR_COMSOL_HOME}}));
  console.log('DO NOT FORWARD PRIVATE STDOUT');console.error('DO NOT FORWARD PRIVATE STDERR');
 });
}else{
 if(argv.includes('--fail')){console.error('PRIVATE CLI CREDENTIAL');process.exit(2);}
 if(argv.includes('--invalid-json')){console.log('PRIVATE RAW NONJSON');process.exit(0);}
 if(argv.includes('--hang'))setInterval(()=>{},1000);
 else if(argv.includes('--oversize'))process.stdout.write('x'.repeat(5*1024*1024));
 else console.log(JSON.stringify(argv.includes('list')?[{id:'library-a'}]:{argv:argv.slice(1),data,script:path.basename(argv[0])}));
}
`;

async function fixture(t, overrides={}){
 const parent=path.dirname(fileURLToPath(import.meta.url));
 const root=await mkdtemp(path.join(parent,'service-test-'));
 const packageRoot=path.join(root,'read only package with spaces'), dataDir=path.join(root,'persistent data');
 const runtime=path.join(packageRoot,'runtime');await mkdir(runtime,{recursive:true});
 for(const name of ['run.py','library_cli.py','agent_cli.py','tools_cli.py','agent_tools.py'])await writeFile(path.join(runtime,name),'# packaged source\n');
 const python=path.join(root,'explicit python.exe');await writeFile(python,'test executable');
 const fake=path.join(root,'fake-python.mjs');await writeFile(fake,FAKE_PYTHON);
 const launches=[],children=[];
 const service=new WorkbenchService({packageRoot,dataDir,pythonPath:python,startupTimeoutMs:1500,
  spawnProcess(command,args,options){launches.push({command,args,options});const child=spawn(process.execPath,[fake,...args],options);children.push(child);return child;},...overrides});
 t.after(async()=>{await service.stop();const resolved=path.resolve(root);assert.equal(path.dirname(resolved),parent);await rm(resolved,{recursive:true,force:true});});
 return {root,packageRoot,runtime,dataDir,python,fake,launches,children,service};
}

test('constructor/status stay lazy; start verifies owned PID and accepts unbuilt-corpus ready=false',async t=>{
 const f=await fixture(t);
 assert.equal(f.launches.length,0);assert.equal(f.service.status().state,'stopped');
 const first=await f.service.start(), again=await f.service.start();
 assert.equal(first.running,true);assert.equal(first.owned,true);assert.equal(first.pid,again.pid);
 assert.match(first.url,/^http:\/\/127\.0\.0\.1:\d+$/);
 assert.equal(f.launches.filter(c=>path.basename(c.args[0])==='run.py').length,1);
 const launch=JSON.parse(await readFile(path.join(f.dataDir,'server-launch.json'),'utf8'));
 assert.deepEqual(launch.argv.slice(-4),['--agent-backend','none','--port','0']);
 assert.equal(launch.cwd,f.dataDir);assert.equal(launch.env.backend,'none');assert.equal(launch.env.noBytecode,'1');
 assert.equal(launch.env.embedded,'1');
 assert.equal(launch.env.cache,path.join(f.dataDir,'cache','library-search'));
 assert.equal(first.python,f.python);assert.ok(!JSON.stringify(first).includes('PRIVATE'));
 assert.equal(f.launches.every(c=>c.options.shell===false&&c.options.windowsHide===true),true);
});

test('startup uses lightweight owned-process health and never requests the large slow corpus status',async t=>{
 const f=await fixture(t,{env:{...process.env,TEST_LARGE_STATUS:'1',TEST_STATUS_DELAY:'2000'}});
 const started=await f.service.start();assert.equal(started.running,true);
 const requests=(await readFile(path.join(f.dataDir,'http-requests.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
 assert.ok(requests.includes('/api/health'));assert.ok(!requests.includes('/api/status'));
});

test('health must affirm the owned PID, selected data root and protocol identity',async t=>{
 for(const fields of [{TEST_HEALTH_READY:'false'},{TEST_WRONG_HEALTH_PID:'1'},{TEST_HEALTH_DATA:'D:/different-data-root'},{TEST_HEALTH_SERVICE:'unrelated-server'}]){
  await t.test(Object.keys(fields)[0],async t=>{
   const f=await fixture(t,{env:{...process.env,...fields},startupTimeoutMs:300});
   await assert.rejects(f.service.start(),/健康检查/);assert.equal(f.service.status().running,false);
   const launch=JSON.parse(await readFile(path.join(f.dataDir,'server.json'),'utf8'));
   assert.throws(()=>process.kill(launch.pid,0));
  });
 }
});

test('offline profile is loaded at start and explicit plugin paths retain priority',async t=>{
 const f=await fixture(t);await mkdir(f.dataDir,{recursive:true});
 const bundle=path.join(f.root,'offline dependencies');await mkdir(bundle);
 await writeFile(path.join(bundle,'python.exe'),'fixture');await writeFile(path.join(bundle,'node.exe'),'fixture');
 await mkdir(path.join(bundle,'qmd'));
 await writeFile(path.join(bundle,'local embedding.gguf'),'fixture');
 await writeFile(path.join(f.dataDir,'dependency-profile.json'),JSON.stringify({schema:'sim-advisor-offline-dependencies/v1',bundleRoot:bundle,
  paths:{pythonPath:'python.exe',nodePath:'node.exe',qmdDir:'qmd',mcpPythonPath:'python.exe',embedModelPath:'local embedding.gguf'}}));
 const started=await f.service.start();
 assert.equal(started.python,f.python); // Existing explicit config outranks saved dependencies.
 assert.equal(started.mcp_python,path.join(bundle,'python.exe'));
 const launch=JSON.parse(await readFile(path.join(f.dataDir,'server-launch.json'),'utf8'));
 assert.equal(launch.env.node,path.join(bundle,'node.exe'));assert.equal(launch.env.qmdDir,path.join(bundle,'qmd'));
 assert.equal(launch.env.embedding,path.join(bundle,'local embedding.gguf'));
 assert.equal(started.embedding_model,launch.env.embedding);
});

test('invalid offline profile fails visibly without spawning a fallback runtime',async t=>{
 const f=await fixture(t);await mkdir(f.dataDir,{recursive:true});
 await writeFile(path.join(f.dataDir,'dependency-profile.json'),JSON.stringify({schema:'outdated'}));
 await assert.rejects(f.service.start(),/离线依赖配置/);assert.equal(f.launches.length,0);
});

test('path configuration and argv with spaces pass unchanged without touching package state',async t=>{
 const f=await fixture(t,{docRoot:'D:/docs with spaces',applications:'D:/apps',simPython:'D:/sim/python.exe',qmd:'D:/tools/qmd.cmd',qmdDir:'D:/qmd package',nodePath:'D:/node/node.exe',pageindexPython:'D:/page/python.exe',comsolHome:'D:/COMSOL'});
 await f.service.start();
 const launch=JSON.parse(await readFile(path.join(f.dataDir,'server-launch.json'),'utf8'));
 assert.equal(launch.env.doc,'D:/docs with spaces');assert.equal(launch.env.applications,'D:/apps');
 assert.equal(launch.env.simPython,'D:/sim/python.exe');assert.equal(launch.env.qmd,'D:/tools/qmd.cmd');
 assert.equal(launch.env.qmdDir,'D:/qmd package');assert.equal(launch.env.node,'D:/node/node.exe');
 assert.equal(launch.env.pageindex,'D:/page/python.exe');assert.equal(launch.env.comsol,'D:/COMSOL');
 const result=await f.service.runCli('library_cli.py',['add','user example','--path','D:/user models/part.mph']);
 assert.deepEqual(result.argv,['add','user example','--path','D:/user models/part.mph']);
 assert.equal(result.data,f.dataDir);
 await assert.rejects(stat(path.join(f.packageRoot,'data')));
 await assert.rejects(stat(path.join(f.runtime,'__pycache__')));
});

test('copied server metadata from another data root cannot adopt or kill the old PID',async t=>{
 const f=await fixture(t);await mkdir(f.dataDir,{recursive:true});
 await writeFile(path.join(f.dataDir,'server.json'),JSON.stringify({pid:process.pid,url:'http://127.0.0.1:49268',data_dir:path.join(f.root,'old workspace')}));
 const result=await f.service.start();assert.equal(result.running,true);assert.notEqual(result.pid,process.pid);
 await f.service.stop();assert.ok(process.pid>0);
});

test('legacy copied server metadata requires an explicit workbench location anchor',async t=>{
 const f=await fixture(t);await mkdir(f.dataDir,{recursive:true});
 await writeFile(path.join(f.dataDir,'server.json'),JSON.stringify({pid:process.pid,url:'http://127.0.0.1:49268'}));
 await writeFile(path.join(f.dataDir,'workspace-location.json'),JSON.stringify({schema:'sim-advisor-workspace-location/v1',current_root:path.join(f.root,'old workspace'),previous_roots:[]}));
 const result=await f.service.start();assert.equal(result.running,true);assert.notEqual(result.pid,process.pid);
 await f.service.stop();assert.ok(process.pid>0);
});

test('same data directory with a live foreign PID is never adopted or killed',async t=>{
 const f=await fixture(t);await mkdir(f.dataDir,{recursive:true});
 const metadata={pid:process.pid,url:'http://127.0.0.1:49268'};
 await writeFile(path.join(f.dataDir,'server.json'),JSON.stringify(metadata));
 await assert.rejects(f.service.start(),/不会接管/);
 assert.equal(f.launches.length,0);assert.equal(f.service.status().owned,false);
 await f.service.stop();assert.equal(process.pid,metadata.pid);
 assert.deepEqual(JSON.parse(await readFile(path.join(f.dataDir,'server.json'),'utf8')),metadata);
});

test('readiness rejects another PID even if it serves valid status; kills only owned child',async t=>{
 const f=await fixture(t,{env:{...process.env,TEST_WRONG_PID:'1'},startupTimeoutMs:200});
 await assert.rejects(f.service.start(),/自有进程的健康检查/);
 assert.equal(f.service.status().running,false);assert.equal(f.service.status().owned,false);
 assert.ok(process.pid>0);
});

test('simultaneous start shares one child and stop preserves data for a fresh start',async t=>{
 const f=await fixture(t);const [a,b]=await Promise.all([f.service.start(),f.service.start()]);
 assert.equal(a.pid,b.pid);await writeFile(path.join(f.dataDir,'retained.json'),'persistent');
 const stopped=await f.service.stop();assert.equal(stopped.stopped,true);assert.equal(stopped.running,false);
 assert.equal(await readFile(path.join(f.dataDir,'retained.json'),'utf8'),'persistent');
 const restarted=await f.service.start();assert.notEqual(restarted.pid,a.pid);
});

test('start requested while stop awaits child close does not deadlock',async t=>{
 const f=await fixture(t);const initial=await f.service.start();
 const stopping=f.service.stop();const starting=f.service.start();
 let timer;let result;
 try{result=await Promise.race([Promise.all([stopping,starting]),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('start/stop deadlock')),6000);})]);}
 finally{clearTimeout(timer);}
 assert.equal(result[0].state,'stopped');assert.equal(result[1].state,'running');assert.notEqual(result[1].pid,initial.pid);
});

test('stop failure never claims success or loses its owned process, and a later stop can retry',async t=>{
 if(process.platform!=='win32'){t.skip('This regression simulates Windows taskkill failure');return;}
 const f=await fixture(t);const started=await f.service.start();
 const child=f.children.find(value=>value.pid===started.pid);
 const spawnOriginal=childProcess.spawn,killOriginal=child.kill;
 childProcess.spawn=(command,args)=>{
  assert.equal(command,'taskkill.exe');assert.deepEqual(args.slice(0,2),['/PID',String(started.pid)]);
  const failed=new EventEmitter();failed.kill=()=>false;queueMicrotask(()=>failed.emit('close',1));return failed;
 };
 child.kill=()=>false;
 syncBuiltinESMExports();
 try{
  await assert.rejects(f.service.stop(),/未能确认.*退出/);
  process.kill(started.pid,0); // This is only the fixture's owned Node child.
  const state=f.service.status();assert.equal(state.state,'error');assert.equal(state.running,true);assert.equal(state.owned,true);assert.equal(state.pid,started.pid);
  assert.ok(!Object.hasOwn(state,'stopped'));
 }finally{
  childProcess.spawn=spawnOriginal;child.kill=killOriginal;syncBuiltinESMExports();
 }
 const stopped=await f.service.stop();assert.equal(stopped.stopped,true);assert.equal(stopped.running,false);
 assert.throws(()=>process.kill(started.pid,0));
});

test('stop during startup cancels the old generation and leaves no service running',async t=>{
 const f=await fixture(t,{env:{...process.env,TEST_READY_DELAY:'1000'}});
 const starting=f.service.start();const rejected=assert.rejects(starting,/启动已取消|程序未成功/);
 await sleep(30);await f.service.stop();await rejected;
 assert.equal(f.service.status().running,false);assert.equal(f.service.status().state,'stopped');
});

test('explicit configuration reports incompatible Python, missing FTS5 and broken runtime import',async t=>{
 for(const [env,message] of [[{TEST_VERSION:'[3,11,9]'},/Python 3.12/],[{TEST_FTS5:'false'},/FTS5/],[{TEST_IMPORT:'false'},/runtime\/advisor.server/]]){
  const f=await fixture(t,{env:{...process.env,...env},offlineBundle:'D:/configuration fixture',prepareOfflineBundle:async()=>({ready:true})});
  await assert.rejects(f.service.configureOffline(),message);
  assert.equal(f.launches.some(c=>path.basename(c.args[0])==='run.py'),false);
 }
});

test('start, restart and a reloaded owner spawn only the service, never a Python probe',async t=>{
 const f=await fixture(t);
 await f.service.start();await f.service.stop();await f.service.start();await f.service.stop();
 const reloaded=new WorkbenchService({...f.service.options});t.after(()=>reloaded.stop());
 await reloaded.start();await reloaded.stop();
 assert.equal(f.launches.length,3);
 assert.ok(f.launches.every(call=>path.basename(call.args[0])==='run.py'));
});

test('Python is probed at explicit configuration only; ordinary startup and reload do not repeat it',async t=>{
 const f=await fixture(t,{offlineBundle:'D:/configuration fixture',prepareOfflineBundle:async()=>({ready:true,reused:true})});
 await f.service.configureOffline();
 assert.equal(f.launches.filter(call=>call.args[0]==='-c').length,1);
 await f.service.start();await f.service.stop();
 await f.service.configureOffline(undefined,{allowInitialize:false});await f.service.start();
 assert.equal(f.launches.filter(call=>call.args[0]==='-c').length,1);
 assert.equal(f.launches.filter(call=>path.basename(call.args[0])==='run.py').length,2);
});

test('explicit Python overrides env; unavailable explicit path does not silently fall back',async t=>{
 const f=await fixture(t,{env:{...process.env,SIM_ADVISOR_PYTHON:'D:/other/python.exe'}});
 await f.service.start();assert.equal(f.launches[0].command,f.python);
 const bad=await fixture(t,{pythonPath:'D:/definitely-missing/python.exe',env:{...process.env,SIM_ADVISOR_PYTHON:f.python}});
 await assert.rejects(bad.service.start(),/Python/);assert.equal(bad.launches.length,0);
});

test('official embedded Python is found before PATH when no explicit interpreter exists',async t=>{
 const f=await fixture(t);const primary=path.join(f.root,'official primary runtime');
 const embedded=path.join(primary,'dependencies','python',process.platform==='win32'?'python.exe':'bin/python3');
 await mkdir(path.dirname(embedded),{recursive:true});await writeFile(embedded,'official candidate');
 const service=new WorkbenchService({packageRoot:f.packageRoot,dataDir:f.dataDir,env:{PATH:'',DSH_BUNDLED_PRIMARY_RUNTIME:primary},
  spawnProcess(command,args,options){f.launches.push({command,args,options});return spawn(process.execPath,[f.fake,...args],options);}});
 try{await service.start();assert.equal(f.launches[0].command,embedded);}
 finally{await service.stop();}
});

test('CLI returns arrays/objects and hides failure logs, malformed output and oversized output',async t=>{
 const f=await fixture(t);assert.deepEqual(await f.service.runCli('library_cli.py',['list']),[{id:'library-a'}]);
 for(const flag of ['--fail','--invalid-json','--oversize']){
  await assert.rejects(f.service.runCli('library_cli.py',[flag]),error=>!error.message.includes('PRIVATE'));
  assert.equal(f.service.status().running,true);
 }
 await assert.rejects(f.service.runCli('../run.py',[]),/不合法/);
 await assert.rejects(f.service.runCli('library_cli.py',['--data-dir','D:/foreign']),/不合法/);
});

test('CLI timeout does not terminate the owned HTTP server',async t=>{
 const f=await fixture(t);const started=await f.service.start();
 await assert.rejects(f.service.runCli('library_cli.py',['--hang'],{timeoutMs:100}),/超时/);
 assert.equal(f.service.status().pid,started.pid);assert.equal(f.service.status().running,true);
});

test('stop invalidates a pending CLI before it can spawn after the owned-child snapshot',async t=>{
 const f=await fixture(t);await f.service.start();
 const cli=f.service.runCli('library_cli.py',['--hang'],{timeoutMs:1000});
 const rejected=assert.rejects(cli,/取消|停止/);
 await f.service.stop();await rejected;
 assert.equal(f.service.status().state,'stopped');
 assert.equal(f.launches.filter(item=>path.basename(item.args[0])==='library_cli.py').length,0);
 assert.deepEqual(await f.service.runCli('library_cli.py',['list']),[{id:'library-a'}]);
});

test('a CLI waiting for start remains cancelled even after stop has fully completed',async t=>{
 const f=await fixture(t);await f.service.start();
 const start=f.service.start.bind(f.service);let entered,release;
 const waiting=new Promise(resolve=>entered=resolve),gate=new Promise(resolve=>release=resolve);
 f.service.start=async()=>{const state=await start();entered();await gate;return state;};
 const cli=f.service.runCli('library_cli.py',['list']);const rejected=assert.rejects(cli,/取消/);
 await waiting;await f.service.stop();release();await rejected;
 assert.equal(f.service.status().state,'stopped');
 assert.equal(f.launches.filter(item=>path.basename(item.args[0])==='library_cli.py').length,0);
 f.service.start=start;
 assert.deepEqual(await f.service.runCli('library_cli.py',['list']),[{id:'library-a'}]);
});

test('a new CLI request during stopping rejects rather than queueing an implicit restart',async t=>{
 const f=await fixture(t);await f.service.start();
 const stopping=f.service.stop();
 await assert.rejects(f.service.runCli('library_cli.py',['list']),/取消|停止/);
 await stopping;
 assert.equal(f.launches.filter(item=>path.basename(item.args[0])==='library_cli.py').length,0);
});

test('data paths inside the package are rejected before writes and startup failures are safe',async t=>{
 const f=await fixture(t);
 assert.throws(()=>new WorkbenchService({packageRoot:f.packageRoot,dataDir:path.join(f.packageRoot,'data')}),/包外/);
 const failed=await fixture(t,{env:{...process.env,TEST_START_FAIL:'1'}});
 await assert.rejects(failed.service.start(),error=>/访问被拒绝/.test(error.message)&&!error.message.includes('PRIVATE'));
 assert.equal(failed.service.status().state,'error');
});

test('selected offline bundle is configured without a shell before Python starts',async t=>{
 let setups=0;
 const f=await fixture(t,{offlineBundle:'D:/offline bundle with spaces',docRoot:'D:/manuals',
  prepareOfflineBundle:async options=>{
   setups++;
   assert.equal(options.bundleRoot,'D:/offline bundle with spaces');assert.equal(options.dataDir,f.dataDir);
   assert.equal(options.externalPaths.docRoot,'D:/manuals');
   options.onProgress('Checking selected offline bundle');
   await writeFile(path.join(f.dataDir,'dependency-profile.json'),JSON.stringify({schema:'sim-advisor-offline-dependencies/v1',
    bundleRoot:f.root,paths:{pythonPath:path.basename(f.python),mcpPythonPath:path.basename(f.python)}}));
   return {ready:true,reused:setups>1};
  }});
 const configured=await f.service.configureOffline();
 assert.equal(configured.running,false);assert.equal(configured.dependency_setup.state,'ready');
 assert.equal(f.launches.filter(call=>call.args[0]==='-c').length,1);
 assert.ok(!f.launches.some(call=>path.basename(call.args[0])==='run.py'));
 const running=await f.service.start();assert.equal(running.running,true);
 assert.equal(running.mcp_python,f.python);assert.equal(running.dependency_setup.reused,false);
 assert.equal(setups,1,'Starting after successful configuration must not repeat full verification');
 assert.equal(f.launches.every(call=>call.options.shell===false&&call.options.windowsHide===true),true);
});

test('same-instance start avoids duplicate setup and stop or another owner uses lightweight persisted configuration',async t=>{
 let setups=0;const modes=[];
 const f=await fixture(t,{offlineBundle:'D:/offline fixture',prepareOfflineBundle:async options=>{
  modes.push([options.forceVerify,options.allowInitialize]);setups++;return{ready:true,reused:setups>1};
 }});
 await f.service.configureOffline();assert.equal(setups,1);
 const first=await f.service.start();assert.equal(setups,1);
 const launches=f.launches.length;
 assert.equal((await f.service.start()).pid,first.pid);
 assert.equal(setups,1);assert.equal(f.launches.length,launches,'A warm start does not run a Python or bundle probe');
 await f.service.stop();await f.service.start();assert.equal(setups,2,'A restart reloads persisted configuration without full verification');
 assert.deepEqual(modes.at(-1),[false,false]);
 await f.service.stop();await f.service.configureOffline();assert.equal(setups,3);
 assert.deepEqual(modes.at(-1),[false,true]);
 await f.service.configureOffline(undefined,{forceVerify:true});assert.equal(setups,4,'Only an explicit full verification requests payload hashes');
 assert.deepEqual(modes.at(-1),[true,true]);
 await f.service.start();assert.equal(setups,4);
 await f.service.stop();
 const another=new WorkbenchService({...f.service.options});
 t.after(()=>another.stop());
 await another.configureOffline(undefined,{allowInitialize:false});assert.equal(setups,5,'Another owner loads the persisted proof without implicit initialization');
 assert.deepEqual(modes.at(-1),[false,false]);
});

test('changing bundle or external paths before first start invalidates the successful selection',async t=>{
 const checked=[];
 const f=await fixture(t,{offlineBundle:'D:/original bundle',prepareOfflineBundle:async options=>{
  checked.push({bundle:options.bundleRoot,external:options.externalPaths});return{ready:true};
 }});
 await f.service.configureOffline();
 f.service.options.offlineBundle='D:/replacement bundle';
 await f.service.start();
 assert.deepEqual(checked.map(value=>value.bundle),['D:/original bundle','D:/replacement bundle']);
 await f.service.stop();await f.service.configureOffline();
 f.service.options.docRoot='D:/different manuals';
 await f.service.start();assert.equal(checked.length,4);
 assert.equal(checked.at(-1).external.docRoot,'D:/different manuals');
});

test('failed configuration cannot populate the same-instance verification cache',async t=>{
 let checks=0;
 const f=await fixture(t,{offlineBundle:'D:/offline fixture',prepareOfflineBundle:async()=>{
  if(++checks===1)throw new Error('Fixture checksum mismatch');return{ready:true};
 }});
 await assert.rejects(f.service.configureOffline(),/checksum/);
 await f.service.start();assert.equal(checks,2);assert.equal(f.service.status().dependency_setup.state,'ready');
});

test('ordinary start refuses missing offline setup without initializing or spawning Python',async t=>{
 const f=await fixture(t,{offlineBundle:'D:/offline fixture',prepareOfflineBundle:async options=>{
  assert.equal(options.allowInitialize,false);assert.equal(options.forceVerify,false);
  throw new Error('请在插件设置中配置所选离线包后再启动。');
 }});
 await assert.rejects(f.service.start(),/插件设置/);
 assert.equal(f.launches.length,0);assert.equal(f.service.status().state,'error');
});

test('the following start still loads and validates the actual dependency profile',async t=>{
 let checks=0;
 const f=await fixture(t,{offlineBundle:'D:/offline fixture',prepareOfflineBundle:async()=>{
  checks++;await writeFile(path.join(f.dataDir,'dependency-profile.json'),JSON.stringify({schema:'sim-advisor-offline-dependencies/v1',
   bundleRoot:f.root,paths:{pythonPath:path.basename(f.python)}}));return{ready:true};
 }});
 await f.service.configureOffline();
 await writeFile(path.join(f.dataDir,'dependency-profile.json'),JSON.stringify({schema:'invalid profile'}));
 await assert.rejects(f.service.start(),/离线依赖配置/);
 assert.equal(checks,1);assert.equal(f.launches.filter(call=>path.basename(call.args[0])==='run.py').length,0);
 await f.service.configureOffline();await rm(path.join(f.dataDir,'dependency-profile.json'));
 await assert.rejects(f.service.start(),/配置缺失/);assert.equal(f.launches.filter(call=>path.basename(call.args[0])==='run.py').length,0);
 await f.service.configureOffline();await f.service.start();assert.equal(checks,3);
});

test('stop cancels a dependency initializer owned by the plugin before any HTTP service starts',async t=>{
 let entered;const executing=new Promise(resolve=>{entered=resolve;});
 const f=await fixture(t,{offlineBundle:'D:/offline bundle',prepareOfflineBundle:async({run})=>{
  const pending=run(f.python,['initialize.mjs','--hang'],{timeoutMs:600000,maxOutput:4096});entered();
  await pending;throw new Error('initializer unexpectedly finished');
 }});
 const configuring=f.service.configureOffline();const rejected=assert.rejects(configuring,/取消|未成功/);
 await executing;await f.service.stop();await rejected;
 assert.equal(f.service.status().state,'stopped');assert.equal(f.service.status().running,false);
 assert.equal(f.launches.some(call=>path.basename(call.args[0])==='run.py'),false);
});

test('automatic configuration does not alter a data directory with a live foreign service',async t=>{
 let setups=0;
 const f=await fixture(t,{offlineBundle:'D:/offline bundle',prepareOfflineBundle:async()=>{setups++;return{ready:true};}});
 await mkdir(f.dataDir,{recursive:true});await writeFile(path.join(f.dataDir,'server.json'),JSON.stringify({pid:process.pid,url:'http://127.0.0.1:49268'}));
 await assert.rejects(f.service.configureOffline(),/不会接管/);
 assert.equal(setups,0);assert.equal(f.launches.length,0);
});

test('choosing a data root inside the offline bundle fails before any directory is created',async t=>{
 const f=await fixture(t);const offline=path.join(f.root,'offline bundle');await mkdir(offline);
 const dataDir=path.join(offline,'not-created');let setups=0;
 const selected=new WorkbenchService({packageRoot:f.packageRoot,dataDir,offlineBundle:offline,
  prepareOfflineBundle:async()=>{setups++;return{ready:true};}});
 try{
  await assert.rejects(selected.configureOffline(),/离线依赖包外/);
  await assert.rejects(stat(dataDir),{code:'ENOENT'});assert.equal(setups,0);
 }finally{await selected.stop();}
});
