import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classify, validateEnvelope, captureSource, discardWrite, assertConfigSupported } from '../openclaw-source-utility.mts';
import { mkdtempSync, mkdirSync, writeFileSync, lstatSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname } from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';
const qaRoot=process.env.OPENCLAW_SOURCE_QA_ROOT;
const qaScope=qaRoot ? await import(pathToFileURL(resolve(qaRoot,'src/shared/async-work-scope.ts')).href) : undefined;
const {AsyncWorkScope,trackAsyncWork}=qaScope??{};

const value=()=>({version:1,host:{buildRoot:'/pinned',expectedCommit:'a'.repeat(40),manifestPath:'/pinned/operator-artifact-manifest.json',manifestSha256:'b'.repeat(64),configPath:'/tmp/qa-config',configSha256:'c'.repeat(64),policyPath:'/tmp/qa-policy',policySha256:'d'.repeat(64),agentId:'main',wallTimeoutMs:30000},payload:{body:'example',bodyComplete:true,catalogComplete:true,candidates:[{candidateId:'c_'+'e'.repeat(32),agentId:'main',title:'topic',purpose:'test',aliases:[]}]}});
process.env.OPENCLAW_CONFIG_PATH='/tmp/qa-config';
process.env.OPENCLAW_MAIL_WORK_DEADLINE_MONOTONIC_MS=String(Number(process.hrtime.bigint()/1000000n)+60000);

function fake(opts:{agentsapi?:boolean,releaseFails?:boolean,delayMs?:number,sourceChanged?:boolean,cleanupFails?:boolean,snapshotThrows?:boolean,cliThrows?:boolean}={}) {
  let release=false, invoked=false, calls=0; const events:string[]=[];
  const modules:any={
    config:{captureRuntimeConfigAsyncReader:()=>async()=>({})},
    utility:{prepareUtilityCompletionForAgent:async()=>({useUtilityModel:true,provider:'qa',model:'fake',agentDir:'/qa'}),runUtilityCompletion:async(p:any,run:any)=>{assert.equal(p.outputTextPolicy,'strict-visible');assert.equal(p.thinkLevel,'off');assert.equal(p.answerTokenBudget,undefined);return await run(p);}},
    route:{resolveIsolatedCompletionProvider:()=>({provider:'qa'}),resolveIsolatedCompletionRoute:async()=>({selection:{selectedHarnessId:opts.agentsapi?'agentsapi':'codex'}})},
    isolated:{runIsolatedCompletion:async()=>{invoked=true; void trackAsyncWork(async()=>{await new Promise(r=>setTimeout(r,opts.delayMs??25));release=true;if(opts.releaseFails) throw Error('fake');}).catch(()=>{});return {text:'{"candidateId":null}'};}},
    agent:{resolveAgentWorkspaceDir:()=>'/qa'},scope:{AsyncWorkScope},
    lifecycle:{closePreparedModelRuntimeSnapshots:async()=>{events.push('snapshot');if(opts.snapshotThrows) throw Error('snapshot');}},
    cleanup:{closeCliResources:async()=>{events.push('cli');if(opts.cleanupFails) console.error('fake disposer failure');if(opts.cliThrows) throw Error('cli');},waitForPendingCliDisposers:async()=>{events.push('wait');}}
  };
  return {deps:{capture:()=>({assertCurrent:()=>{calls++;if(opts.sourceChanged && calls>1) throw Error('changed');}}),load:async()=>modules},events,get release(){return release;},get invoked(){return invoked;}};
}
test('strict envelope and bounded candidates',()=>{assert.doesNotThrow(()=>validateEnvelope(value())); const v=value();v.payload.bodyComplete=false;assert.throws(()=>validateEnvelope(v));});
test('discarded diagnostic writes settle both callback forms',async()=>{await new Promise<void>(r=>discardWrite('private',r));await new Promise<void>(r=>discardWrite('private','utf8',r));});
test('strict config accepts escapes but rejects nested escaped include keys and JSON5',()=>{assert.doesNotThrow(()=>assertConfigSupported('{"path":"a\\\\b"}'));assert.throws(()=>assertConfigSupported('{"nested":[{"\\u0024include":"child"}]}'));assert.throws(()=>assertConfigSupported('{foo:1}'));});
test('manifest pins critical files and detects changes',()=>{
 const root=mkdtempSync(resolve(tmpdir(),'bridge-pin-')); const sha=(s:string)=>createHash('sha256').update(s).digest('hex');
 try {
  const names=['package.json','dist/build-info.json','scripts/tsx.mjs','src/config/config.ts','src/agents/utility-completion.ts','src/agents/isolated-completion.ts','src/agents/isolated-completion-route.ts','src/agents/agent-scope.ts','src/shared/async-work-scope.ts','src/agents/prepared-model-runtime.lifecycle.ts','src/cli/runtime-cleanup.ts'];
  const files=names.map(path=>{const full=resolve(root,path), content=path==='package.json'?'{"name":"openclaw"}':path==='dist/build-info.json'?JSON.stringify({commit:'a'.repeat(40)}):'test'; mkdirSync(dirname(full),{recursive:true});writeFileSync(full,content);return {path,bytes:Buffer.byteLength(content),mode:lstatSync(full).mode&0o7777,sha256:sha(content)};});
  const config=resolve(root,'config.private'), policy=resolve(root,'policy.private');writeFileSync(config,'{}');writeFileSync(policy,'{}');
  for(const path of ['config.private','policy.private']) files.push({path,bytes:2,mode:lstatSync(resolve(root,path)).mode&0o7777,sha256:sha('{}')});
  const manifest=JSON.stringify({commit:'a'.repeat(40),files,symlinks:[]}), mp=resolve(root,'operator-artifact-manifest.json');writeFileSync(mp,manifest);
  const host={buildRoot:root,expectedCommit:'a'.repeat(40),manifestPath:mp,manifestSha256:sha(manifest),configPath:config,configSha256:sha('{}'),policyPath:policy,policySha256:sha('{}')};
  const s=captureSource(host,()=>{}); s.assertCurrent();
  writeFileSync(resolve(root,'src/agents/utility-completion.ts'),'changed');assert.throws(()=>s.assertCurrent());
 } finally {rmSync(root,{recursive:true,force:true});}
});
const sourceTest=(name:string,fn:()=>Promise<void>)=>test(name,{skip:!qaRoot},fn);
sourceTest('success waits for late owned cleanup',async()=>{const f=fake();const r=await classify(value(),f.deps as any);assert.equal(r.status,'ok');assert.equal(f.release,true);});
sourceTest('failed late cleanup prevents success',async()=>{const f=fake({releaseFails:true});const r=await classify(value(),f.deps as any);assert.equal(r.status,'deferred');assert.equal(r.errorCode,'classifier_cleanup_failed');});
sourceTest('agentsapi never invokes inference',async()=>{const f=fake({agentsapi:true});const r=await classify(value(),f.deps as any);assert.equal(r.errorCode,'classifier_runtime_unsupported');assert.equal(f.invoked,false);});
sourceTest('reported disposer failure prevents success',async()=>{const f=fake({cleanupFails:true});const r=await classify(value(),f.deps as any);assert.equal(r.errorCode,'classifier_cleanup_failed');});
for (const opts of [{snapshotThrows:true},{cliThrows:true}]) sourceTest('all remaining finalizers run after rejection',async()=>{const f=fake(opts);const r=await classify(value(),f.deps as any);assert.equal(r.errorCode,'classifier_cleanup_failed');assert.deepEqual(f.events,['snapshot','cli','wait']);});
sourceTest('source mutation prevents success',async()=>{const f=fake({sourceChanged:true});const r=await classify(value(),f.deps as any);assert.equal(r.status,'deferred');});
sourceTest('deadline during cleanup cannot return success',async()=>{const f=fake({delayMs:1100});const v=value();const r=await classify(v,{...f.deps,workDeadline:Number(process.hrtime.bigint()/1000000n)+1000} as any);assert.equal(r.status,'deferred');assert.equal(r.errorCode,'classifier_timeout');assert.equal(f.release,true);});
