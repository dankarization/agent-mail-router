// Run using the serving host's existing TS preload, no installs.
// MAIL_TEST_HOST_ROOT selects read-only runtime sources; no live config is read.
import assert from 'node:assert/strict';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { mkdtempSync, rmSync, chmodSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { createMailAdapter } from '../mail-origin-plugin/index.mjs';

const host=process.env.MAIL_TEST_HOST_ROOT;
assert(host, 'MAIL_TEST_HOST_ROOT required');
const root=mkdtempSync(resolve(process.env.MAIL_TEST_SCRATCH ?? tmpdir(),'mail-origin-'));
process.env.HOME=root;
process.env.OPENCLAW_STATE_DIR=root;
process.env.OPENCLAW_CONFIG_PATH=resolve(root,'absent-test-config.json');
process.env.XDG_CONFIG_HOME=root;
const load=path => import(pathToFileURL(resolve(host,path)).href);
const {createTestPluginRegistry}=await load('src/plugins/registry-runtime.test-helpers.ts');
const {createPluginRecord}=await load('src/plugins/status.test-helpers.ts');
const {createPluginToolFactoryContext}=await load('src/plugins/tool-factory-context.ts');
const {bindPluginToolCallbacks}=await load('src/plugins/tool-factory-runtime.ts');
const {executeRegisteredPluginCommand}=await load('src/plugins/plugin-command-execution.ts');
const {markPluginRegistryRetired}=await load('src/plugins/registry-lifecycle.ts');
const {Socket}=await import('node:net');
const {syncBuiltinESMExports}=await import('node:module');
Socket.prototype.connect=function(){throw Error('network forbidden');};
syncBuiltinESMExports();
const dbPath=resolve(root,'mail.sqlite3');
const seed=new DatabaseSync(dbPath);seed.close();chmodSync(dbPath,0o600);
const config={dbPath,credentialsPath:resolve(root,'never-read.env'),pythonPath:'/usr/bin/python3'};
const fact={agentId:'main',sessionKey:'agent:main:main',sessionId:'synthetic-session-generation',
  requesterSenderId:'synthetic-owner',messageChannel:'telegram',senderIsOwner:true};
const p={operationId:'synthetic-operation-0001',to:['recipient@example.invalid'],subject:'Synthetic test',body:'Only synthetic data.'};
let calls=0, now=2000000000000, duringSend;
const adapter=createMailAdapter(config,{now:()=>now,send:async(_cfg,envelope)=>{
  calls++;
  const db=new DatabaseSync(dbPath);
  const row=db.prepare('SELECT * FROM mail_outbound_v1 WHERE mid=?').get(envelope.messageId);
  assert.equal(row.status,'inflight'); assert.equal(JSON.parse(row.origin).sessionKey,fact.sessionKey);
  db.close();
  await duringSend?.(envelope);
  return {status:'accepted',sentCopy:'failed'};
}});
const builder=createTestPluginRegistry();
const record=createPluginRecord({id:'agent-mail-origin',contracts:{tools:['mail_prepare']}});
builder.registry.plugins.push(record);
const api=builder.createApi(record,{config:{},registrationMode:'full'});
api.registerTool(adapter.factory,{name:'mail_prepare'});
api.registerCommand(adapter.command);
const entry=builder.registry.tools[0];
const registration=builder.registry.commands[0];
const command={...registration.command,pluginId:registration.pluginId};
assert.equal(entry.contextVersion,2);
assert(command,'registered command missing');
let current=true;
const assertion=()=>assert(current,'synthetic owner revoked');
const context=createPluginToolFactoryContext({entry,registry:builder.registry,context:fact,assertInvocationCurrent:assertion});
const raw=entry.factory(context);
const tool=bindPluginToolCallbacks(entry,builder.registry,raw,context.assertInvocationCurrent);
const confirm=(token,extra={})=>executeRegisteredPluginCommand(builder.registry,{
  command,args:token,commandBody:'/mailsend '+token,config:{},channel:'telegram',
  senderId:'synthetic-owner',isAuthorizedSender:true,senderIsOwner:true,
  assertOwnerCurrent:assertion,agentId:fact.agentId,sessionKey:fact.sessionKey,sessionId:fact.sessionId,...extra});
const details=r=>r.details;
try {
  const draft=details(await tool.execute('draft-1',p));
  assert.equal(calls,0);
  const token=draft.humanConfirmation.split(' ')[1];
  assert.equal(details(await tool.execute('draft-retry',p)).messageId,draft.messageId);
  await assert.rejects(()=>tool.execute('bad-session-arg',{...p,sessionKey:'agent:main:spoof'}),/mail_input_invalid/);
  assert(!String((await confirm(token,{sessionKey:'agent:main:unrelated'})).text).includes('"status":"accepted"'));
  assert(!String((await confirm(token,{sessionId:'reset-generation'})).text).includes('"status":"accepted"'));
  assert(!String((await confirm(token,{isAuthorizedSender:false})).text).includes('"status":"accepted"'));
  assert(!String((await confirm(token,{senderIsOwner:false})).text).includes('"status":"accepted"'));
  assert.equal(calls,0);
  duringSend=async envelope=>{
    // Very fast reply: Python reads the committed real adapter row DURING transport.
    // The fixture does not register an origin or call the ledger writer.
    const proof=spawnSync(config.pythonPath,['-B',fileURLToPath(new URL('./mail-origin-reply-proof.py',import.meta.url)),dbPath,envelope.messageId],
      {encoding:'utf8',env:{PATH:'/usr/bin:/bin'},maxBuffer:65536});
    assert.equal(proof.status,0,proof.stdout+proof.stderr);
    console.log(proof.stdout.trim());
    const duplicate=await confirm(token);
    assert.equal(JSON.parse(duplicate.text).status,'inflight');
  };
  const sent=JSON.parse((await confirm(token)).text);
  assert.equal(sent.status,'accepted');assert.equal(sent.sentCopy,'failed');assert.equal(calls,1);
  await confirm(token);assert.equal(calls,1);
  assert.equal(details(await tool.execute('retry-after-sent-fail',p)).status,'accepted');
  await assert.rejects(()=>tool.execute('changed',{...p,body:'changed'}),/operation_conflict/);
  duringSend=undefined;
  const expired=details(await tool.execute('expire',{...p,operationId:'synthetic-operation-expire'}));
  now+=600001;
  await confirm(expired.humanConfirmation.split(' ')[1]);assert.equal(calls,1);
  current=false;
  await assert.rejects(()=>tool.execute('revoked',{...p,operationId:'synthetic-operation-revoke'}));
  current=true;
  // Factory without runtime admission cannot mint its own guard.
  const absent=createPluginToolFactoryContext({entry,registry:builder.registry,context:fact});
  await assert.rejects(()=>entry.factory(absent).execute('unadmitted',p),/authority/);
  // Missing and non-owner runtime identity fail closed.
  for(const bad of [{...fact,sessionId:undefined},{...fact,senderIsOwner:false}]) {
    const c=createPluginToolFactoryContext({entry,registry:builder.registry,context:bad,assertInvocationCurrent:assertion});
    await assert.rejects(()=>entry.factory(c).execute('missing',p),/trusted_origin/);
  }
  const collision=createMailAdapter(config,{mintId:()=>draft.messageId});
  await assert.rejects(()=>collision.factory.create(context).execute('collision',{...p,operationId:'synthetic-id-collision'}));
  let uncertainCalls=0;
  const uncertain=createMailAdapter(config,{now:()=>now,send:async()=>{uncertainCalls++;throw Error('synthetic timeout');}});
  const ud=details(await uncertain.factory.create(context).execute('uncertain',{...p,operationId:'synthetic-operation-timeout'}));
  const uc={...command,handler:uncertain.command.handler};
  const retryUncertain=()=>executeRegisteredPluginCommand(builder.registry,{
    command:uc,args:ud.humanConfirmation.split(' ')[1],commandBody:'/mailsend',config:{},channel:'telegram',
    senderId:'synthetic-owner',isAuthorizedSender:true,senderIsOwner:true,assertOwnerCurrent:assertion,
    agentId:fact.agentId,sessionKey:fact.sessionKey,sessionId:fact.sessionId});
  assert.equal(JSON.parse((await retryUncertain()).text).status,'unknown');
  await retryUncertain();assert.equal(uncertainCalls,1);
  markPluginRegistryRetired(builder.registry);
  await assert.rejects(()=>tool.execute('retired',p));
  console.log('PASS real runtime registry -> v2 factory -> native authorized human command -> durable origin -> fast reply; no network');
} finally {rmSync(root,{recursive:true,force:true});}
