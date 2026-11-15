import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,openSync,readFileSync,closeSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {classify,diagnosticChannel} from '../openclaw-compiled-utility-r3.mts';
const secret='SECRET-MAIL-TOKEN-PROMPT-987';
const v={version:1,host:{buildRoot:'/pinned',expectedCommit:'a'.repeat(40),manifestPath:'/pinned/manifest',manifestSha256:'b'.repeat(64),configPath:'/tmp/qa-config',configSha256:'c'.repeat(64),policyPath:'/tmp/qa-policy',policySha256:'d'.repeat(64),agentId:'main',wallTimeoutMs:30000},payload:{body:secret,bodyComplete:true,catalogComplete:true,candidates:[]}};
process.env.OPENCLAW_CONFIG_PATH='/tmp/qa-config';
class Scope { constructor(_:unknown){} async track(fn:any){return fn()} async drain(){} beginClose(){} static async runWhenAllIdle(_:any,fn:any){return fn()} }
class IsolatedCompletionError extends Error {code:string; constructor(code:string){super(secret);this.name='IsolatedCompletionError';this.code=code;}}
function setup(bad:string,code='runtime-unavailable',cleanupFail=false) {
 let captured:any[]=[];
 const failure=()=>{const e=bad==='inference'?new IsolatedCompletionError(code):new TypeError(secret);e.stack=secret;(e as any).cause=secret;throw e};
 const m:any={config:{captureRuntimeConfigAsyncReader:()=>async()=>{if(bad==='config')failure();return {}}},utility:{prepareUtilityCompletionForAgent:async()=>{if(bad==='prepare')failure();return {useUtilityModel:true}},runUtilityCompletion:async(opts:any,fn:any)=>{if(bad==='prompt')assert.match(opts.systemPrompt,/verbatim substrings copied exactly from the email body/);return fn({})}},route:{resolveEffectiveAgentRuntime:()=>{if(bad==='route')failure();return bad==='agentsapi'?'agentsapi':'codex'}},isolated:{runIsolatedCompletion:async()=>{if(bad==='inference')failure();return {text:bad==='output'?'': '{}'}}},agent:{resolveAgentWorkspaceDir:()=>'/tmp'},scope:{AsyncWorkScope:Scope},lifecycle:{closePreparedModelRuntimeSnapshots:async()=>{if(cleanupFail||bad==='cleanup')failure()}},cleanup:{closeCliResources:async()=>{},waitForPendingCliDisposers:async()=>{}}};
 return {get events(){return captured}, deps:{capture:()=>({assertCurrent(){}}),load:async()=>m,workDeadline:Number(process.hrtime.bigint()/1000000n)+12000,diagnostics:(x:any[])=>{captured=x}}};
}
for(const phase of ['config','prepare','route','inference','output','cleanup']) test(`private metadata for ${phase}`,async()=>{const f=setup(phase);const result=await classify(v,f.deps as any);assert.equal(result.status,'deferred');assert.deepEqual(Object.keys(result).sort(),['errorCode','status','version']);assert(f.events.some(e=>e.phase===phase&&e.event==='error'));assert(!JSON.stringify(f.events).includes(secret));assert(Buffer.byteLength(JSON.stringify(f.events))<4096);});
test('isolated error codes are classified by whitelist only',async()=>{for(const code of ['runtime-unavailable','output-rejected',secret]){const f=setup('inference',code);await classify(v,f.deps as any);const e=f.events.find(e=>e.event==='error'&&e.phase==='inference');assert.equal(e.code,code===secret?undefined:code);assert(!JSON.stringify(f.events).includes(secret));}});
test('work error and cleanup error remain distinct',async()=>{const f=setup('inference','output-rejected',true);const r=await classify(v,f.deps as any);assert.equal(r.errorCode,'classifier_cleanup_failed');assert(f.events.some(e=>e.phase==='inference'&&e.code==='output-rejected'));assert(f.events.some(e=>e.phase==='cleanup'&&e.event==='error'));});
test('abort preserves existing timeout and response protocol',async()=>{const f=setup('none');const r=await classify(v,{...f.deps,initiallyAborted:true} as any);assert.equal(r.errorCode,'classifier_timeout');assert(!JSON.stringify(f.events).includes(secret));});
test('only host-owned private empty regular fd is accepted',()=>{const dir=mkdtempSync(join(tmpdir(),'utility-trace-'));try{const fd=openSync(join(dir,'trace'),'wx',0o600);try{const emit=diagnosticChannel(String(fd));assert(emit);emit!([{phase:'config',event:'start',elapsedMs:1}]);assert.deepEqual(JSON.parse(readFileSync(join(dir,'trace'),'utf8')).version,1);assert.equal(diagnosticChannel(String(fd)),undefined)}finally{closeSync(fd)}const fd2=openSync(join(dir,'public'),'wx',0o644);try{assert.equal(diagnosticChannel(String(fd2)),undefined)}finally{closeSync(fd2)}assert.equal(diagnosticChannel('2'),undefined);assert.equal(diagnosticChannel('3garbage'),undefined)}finally{rmSync(dir,{recursive:true,force:true})}});
test('diagnostic sink failure never changes classifier outcome',async()=>{const f=setup('none');const r=await classify(v,{...f.deps,diagnostics:()=>{throw Error(secret)}} as any);assert.deepEqual(r,{version:1,status:'ok',decisionRaw:'{}'});});

test('agentsapi is rejected before isolated inference',async()=>{const f=setup('agentsapi');const r=await classify(v,f.deps as any);assert.equal(r.status,'deferred');assert.equal(r.errorCode,'classifier_runtime_unsupported');assert(!f.events.some(e=>e.phase==='inference'));});

test('classifier requests verbatim evidence without relaxing the validator',async()=>{const f=setup('prompt');const r=await classify(v,f.deps as any);assert.equal(r.status,'ok');});
