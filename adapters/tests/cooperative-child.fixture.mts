import { classify } from '../openclaw-source-utility.mts';
const v={version:1,host:{buildRoot:'/pinned',expectedCommit:'a'.repeat(40),manifestPath:'/pinned/operator-artifact-manifest.json',manifestSha256:'b'.repeat(64),configPath:'/tmp/qa-config',configSha256:'c'.repeat(64),policyPath:'/tmp/qa-policy',policySha256:'d'.repeat(64),agentId:'main',wallTimeoutMs:30000},payload:{body:'x',bodyComplete:true,catalogComplete:true,candidates:[{candidateId:'c_'+'e'.repeat(32),agentId:'main',title:'topic',purpose:'test',aliases:[]}]}};
process.env.OPENCLAW_CONFIG_PATH='/tmp/qa-config';
let cleaned=false;
class Scope {track(f:any){return f();} beginClose(){} async drain(){await new Promise(r=>setTimeout(r,40));cleaned=true;} static async runWhenAllIdle(_s:any,f:any){await f();}}
const m:any={scope:{AsyncWorkScope:Scope},config:{captureRuntimeConfigAsyncReader:()=>async()=>({})},utility:{prepareUtilityCompletionForAgent:async()=>({useUtilityModel:true}),runUtilityCompletion:async(p:any)=>{process.stdout.write('ready\n');await new Promise<void>(r=>p.abortSignal.addEventListener('abort',()=>r(),{once:true}));return {text:'{}'};}},agent:{resolveAgentWorkspaceDir:()=>'/qa'},lifecycle:{closePreparedModelRuntimeSnapshots:async()=>{}},cleanup:{closeCliResources:async()=>{},waitForPendingCliDisposers:async()=>{}}};
const result=await classify(v,{capture:()=>({assertCurrent:()=>{}}),load:async()=>m,workDeadline:Number(process.hrtime.bigint()/1000000n)+5000} as any);
process.stdout.write(JSON.stringify({result,cleaned})+'\n');
