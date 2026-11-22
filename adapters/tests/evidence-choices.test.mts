import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as source from '../openclaw-source-utility.mts';
import * as compiled from '../openclaw-compiled-utility-r3.mts';

const body='СИНТЕТИЧЕСКАЯ ПРОВЕРКА ПОЧТОВОЙ МАРШРУТИЗАЦИИ\nСИНТЕТИЧЕСКАЯ ПРОВЕРКА ПОЧТОВОЙ МАРШРУТИЗАЦИИ; тематика почтового монитора/e-mail маршрутизации; это тестовые данные, поручений и внешнего письма нет; действий не выполнять.';
for (const adapter of [source,compiled]) {
 test('exact source choices are UTF-8 bounded, cover the body and exclude paraphrases',()=>{
  const choices=adapter.evidenceChoices(body);
  assert(choices.length>1);assert.equal(choices.join(''),body);
  for(const s of choices){assert(body.includes(s));assert(Buffer.byteLength(s,'utf8')<=160);assert(s.length>0);}
  assert(!choices.includes('почтовой маршрутизации'));
  assert(choices.some(s=>s.includes('ПОЧТОВОЙ МАРШРУТИЗАЦИИ')));
  const schema=adapter.evidenceSchema(choices);
  assert.deepEqual(schema.properties.evidence.items.enum,[...new Set(choices)]);
  assert.equal(schema.properties.evidence.maxItems,4);
 });
 test('schema excludes Python-blank chunks including Unicode and separator whitespace',()=>{
  for (const blank of [' '.repeat(160),'\u0085'.repeat(80),'\u001c'.repeat(160)]) {
   const text=blank+'REAL source excerpt';
   const chunks=adapter.evidenceChoices(text);
   assert.equal(chunks.join(''),text);
   const offered=adapter.evidenceSchema(chunks).properties.evidence.items.enum;
   assert(!offered.includes(blank));
   assert(offered.every(x=>/[^\p{White_Space}\u001c-\u001f]/u.test(x)));
   assert(offered.some(x=>x.includes('REAL source excerpt')));
  }
 });
 test('long multibyte source is bounded and covers the final code point',()=>{
  const s='А'.repeat(6143)+'z';const list=adapter.evidenceChoices(s);
  assert(Buffer.byteLength(s)<=12288);
  assert(list.length<=80);assert.equal(list.join(''),s);
  assert(list.every(x=>s.includes(x)&&Buffer.byteLength(x)<=160&&!x.includes('\ufffd')));
  assert(list.at(-1)?.endsWith('z'));
 });
 test('real classify passes exact choices to model schema and prompt',async()=>{
  let passed:any;
  class Scope {constructor(_:unknown){} async track(fn:any){return fn()} async drain(){} beginClose(){} static async runWhenAllIdle(_:any,fn:any){return fn()}}
  const modules:any={config:{captureRuntimeConfigAsyncReader:()=>async()=>({})},utility:{prepareUtilityCompletionForAgent:async()=>({useUtilityModel:true}),runUtilityCompletion:async(opts:any)=>{passed=opts;return {text:'{\"candidateId\":null,\"candidateId\":null}'}}},agent:{resolveAgentWorkspaceDir:()=>'/qa'},scope:{AsyncWorkScope:Scope},lifecycle:{closePreparedModelRuntimeSnapshots:async()=>{}},cleanup:{closeCliResources:async()=>{},waitForPendingCliDisposers:async()=>{}}};
  const v={version:1,host:{buildRoot:'/pinned',expectedCommit:'a'.repeat(40),manifestPath:'/pinned/manifest',manifestSha256:'b'.repeat(64),configPath:'/tmp/qa-config',configSha256:'c'.repeat(64),policyPath:'/tmp/qa-policy',policySha256:'d'.repeat(64),agentId:'main',wallTimeoutMs:30000},payload:{body,bodyComplete:true,catalogComplete:true,candidates:[]}};
  process.env.OPENCLAW_CONFIG_PATH='/tmp/qa-config';
  const result=await adapter.classify(v,{capture:()=>({assertCurrent(){}}),load:async()=>modules,workDeadline:Number(process.hrtime.bigint()/1000000n)+12000} as any);
  assert.equal(result.status,'ok');assert.equal(result.decisionRaw,'{\"candidateId\":null,\"candidateId\":null}');
  const payload=JSON.parse(passed.prompt);
  assert.equal(payload.body,body);
  assert.deepEqual(payload.evidenceChoices,[...new Set(adapter.evidenceChoices(body))]);
  assert.deepEqual(passed.outputSchema.properties.evidence.items.enum,payload.evidenceChoices);
  assert.match(passed.systemPrompt,/only from evidenceChoices/);
 });
}
