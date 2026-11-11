import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import * as shim from '../bridge/index.js';
import * as canonical from '../../dsh-workbench-bridge/index.js';
import * as shimHTTP from '../bridge/http.js';
import * as canonicalHTTP from '../../dsh-workbench-bridge/http.js';

test('source bridge shims expose the exact canonical implementation and exception identity',()=>{
 assert.equal(shim.WorkbenchBridge,canonical.WorkbenchBridge);
 assert.equal(shim.apply,canonical.apply);
 assert.equal(shimHTTP.BridgeError,canonicalHTTP.BridgeError);
 assert.equal(shimHTTP.requestJSON,canonicalHTTP.requestJSON);
});

test('package builder takes standalone canonical bridge files instead of development shims',async()=>{
 const builder=await readFile(new URL('../../../scripts/build_dsh_workbench.py',import.meta.url),'utf8');
 assert.match(builder,/files\["bridge\/" \+ name\] = APP \/ "integrations" \/ "dsh-workbench-bridge" \/ name/);
 for(const file of ['index.js','http.js']){
  const packaged=await readFile(new URL('../../dsh-workbench-bridge/'+file,import.meta.url),'utf8');
  assert.doesNotMatch(packaged,/from\s+['"]\.\.\//,'Packaged bridge must not import outside its bundle');
 }
});
