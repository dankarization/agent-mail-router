import assert from 'node:assert/strict';
import fs from 'node:fs';
const adapter=fs.readFileSync(new URL('./mail-trigger-result.js',import.meta.url),'utf8');
const parse=new Function('result',adapter+'\nreturn JSON.parse(output);');
const event={fire:false,state:{cursor:{last_uid:114,uidvalidity:'77'}}};
const ok={status:'completed',exitCode:0,aggregated:JSON.stringify(event)};
for (const r of [ok,{details:ok},{result:{details:ok}},{result:ok}]) assert.deepEqual(parse(r),event);
for (const [value,expected] of [
 [{...ok,exitCode:7},/exit_7/],
 [{status:'failed',exitCode:null,timedOut:true,failureKind:'overall-timeout'},/exec_timeout/],
 [{status:'failed',exitCode:null,noOutputTimedOut:true},/exec_timeout/],
 [{status:'failed',exitCode:null,failureKind:'signal'},/exec_failed/],
 [{status:'running',sessionId:'test'},/exec_incomplete/],
 [{status:'completed',aggregated:ok.aggregated},/exit_missing/],
 [{...ok,exitCode:null},/exit_missing/],
 [{...ok,exitCode:'0'},/exit_missing/],
 [{...ok,isError:true},/exec_failed/],
 [{...ok,exitSignal:'SIGTERM'},/exec_failed/],
 [{...ok,aggregated:null},/output_missing/],
 [null,/contract_missing/],
]) assert.throws(()=>parse(value),expected);
console.log('16 adapter cases PASS (4 success envelopes, 12 fail-closed cases)');
