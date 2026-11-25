import { DatabaseSync } from 'node:sqlite';
import { randomUUID, randomBytes, createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isAbsolute, dirname, resolve } from 'node:path';
import { statSync, realpathSync } from 'node:fs';

// Origin is captured only from host factory/command contexts, never tool JSON.
// The operator/installed code/OS account remain trusted, as for the existing DB.
const text = value => ({content:[{type:'text',text:JSON.stringify(value)}], details:value});
const fail = code => { throw new Error(code); };
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const identity = (ctx, command = false) => {
  const guard = command ? ctx.assertOwnerCurrent : ctx.assertInvocationCurrent;
  if (ctx.senderIsOwner !== true || typeof guard !== 'function' ||
      (command && ctx.isAuthorizedSender !== true) ||
      ![ctx.sessionKey,ctx.sessionId,ctx.agentId,command ? ctx.senderId : ctx.requesterSenderId,
        command ? ctx.channel : ctx.messageChannel].every(v => typeof v === 'string' && v.length && v.length < 512)) fail('trusted_origin_unavailable');
  guard();
  return {sessionKey:ctx.sessionKey,sessionId:ctx.sessionId,agentId:ctx.agentId,
    senderId:command ? ctx.senderId : ctx.requesterSenderId,
    channel:command ? ctx.channel : ctx.messageChannel,
    accountId:command ? (ctx.accountId ?? '') : (ctx.agentAccountId ?? '')};
};
function payload(args) {
  if (!args || Object.keys(args).sort().join(',') !== 'body,operationId,subject,to' ||
      typeof args.operationId !== 'string' || !/^[a-zA-Z0-9_-]{16,80}$/.test(args.operationId) ||
      !Array.isArray(args.to) || !args.to.length || args.to.length > 20 ||
      args.to.some(a => typeof a !== 'string' || a.length > 254 || !/^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9.-]+$/.test(a)) ||
      new Set(args.to).size !== args.to.length ||
      typeof args.subject !== 'string' || !args.subject.length || args.subject.length > 200 || /[\r\n\x00-\x1f\x7f]/.test(args.subject) ||
      typeof args.body !== 'string' || !args.body.length || Buffer.byteLength(args.body) > 12000 || args.body.includes('\0')) fail('mail_input_invalid');
  return {to:[...args.to],subject:args.subject,body:args.body};
}
export function openLedger(path) {
  if (!isAbsolute(path)) fail('absolute_db_path_required');
  // Reuse the existing private router DB; never create a world-readable draft store.
  const file=statSync(path), parent=statSync(dirname(path));
  if(realpathSync(path)!==resolve(path) || !file.isFile() || (file.mode & 0o077) ||
     file.uid!==process.getuid() || (parent.mode & 0o022) || parent.uid!==process.getuid()) fail('private_db_required');
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA busy_timeout=1000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS mail_outbound_v1(
      op TEXT PRIMARY KEY,mid TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,origin TEXT NOT NULL,
      approval TEXT NOT NULL UNIQUE,created INTEGER NOT NULL,status TEXT NOT NULL,result TEXT);`);
  return db;
}
export function transport(config, envelope) {
  return new Promise(resolve => {
    const worker = fileURLToPath(new URL('../../tools/agent_mail_send_transport.py', import.meta.url));
    let settled = false, output = '';
    const finish = result => { if (!settled) { settled=true; clearTimeout(timer); resolve(result); } };
    const child = spawn(config.pythonPath, ['-I',worker,config.credentialsPath],
      {stdio:['pipe','pipe','ignore'],env:{PATH:'/usr/bin:/bin',PYTHONDONTWRITEBYTECODE:'1'}});
    const timer = setTimeout(() => { child.kill('SIGKILL'); finish({status:'unknown',sentCopy:'unknown'}); }, 45000);
    child.on('error', () => finish({status:'unknown',sentCopy:'unknown'}));
    child.stdin.on('error', () => {});
    child.stdout.on('data', chunk => { output+=chunk; if (output.length > 4096) { child.kill('SIGKILL'); finish({status:'unknown',sentCopy:'unknown'}); } });
    child.on('close', code => {
      try {
        const r=JSON.parse(output);
        if (code !== 0 || !['accepted','partial','refused','unknown','not_sent'].includes(r.status) ||
            !['ok','failed','not_attempted','unknown'].includes(r.sentCopy)) throw Error();
        finish(r);
      } catch { finish({status:'unknown',sentCopy:'unknown'}); }
    });
    child.stdin.end(JSON.stringify(envelope));
  });
}
export function createMailAdapter(config, {send = transport, now = Date.now, mintId = () => '<'+randomUUID()+'@mail-origin.invalid>'} = {}) {
  for (const k of ['dbPath','credentialsPath','pythonPath']) if (typeof config[k] !== 'string' || !isAbsolute(config[k])) fail('operator_config_required');
  const transaction = (db, guard, action) => {
    db.exec('BEGIN IMMEDIATE');
    try { guard(); const result=action(); db.exec('COMMIT'); return result; }
    catch(e) { db.exec('ROLLBACK'); throw e; }
  };
  const create = ctx => ({
    name:'mail_prepare',label:'Prepare mail',description:'Prepare immutable mail, not send it. The human must review the full returned draft and personally confirm /mailsend TOKEN in this same session. Never run that command on their behalf. No attachments or CC.',
    parameters:{type:'object',additionalProperties:false,required:['operationId','to','subject','body'],
      properties:{operationId:{type:'string'},to:{type:'array',items:{type:'string'}},subject:{type:'string'},body:{type:'string'}}},
    async execute(_id,args) {
      const origin=identity(ctx), p=payload(args), db=openLedger(config.dbPath);
      try {
        const row=transaction(db,()=>ctx.assertInvocationCurrent(),()=>{
          const old=db.prepare('SELECT * FROM mail_outbound_v1 WHERE op=?').get(args.operationId);
          if (old) {
            if(old.origin!==JSON.stringify(origin)||old.payload!==JSON.stringify(p)) fail('operation_conflict');
            return old;
          }
          const mid=mintId();
          if(!/^<[^<>\s]{1,998}>$/.test(mid)) fail('message_id_invalid');
          for (const table of ['anchors','mail']) {
            const exists=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(table);
            if(exists && db.prepare('SELECT 1 FROM '+table+' WHERE '+(table==='anchors'?'id':'message_id')+'=?').get(mid)) fail('message_id_conflict');
          }
          const approval=randomBytes(24).toString('hex');
          db.prepare('INSERT INTO mail_outbound_v1 VALUES(?,?,?,?,?,?,?,NULL)').run(args.operationId,mid,JSON.stringify(p),JSON.stringify(origin),approval,now(),'draft');
          return db.prepare('SELECT * FROM mail_outbound_v1 WHERE op=?').get(args.operationId);
        });
        return text({status:row.status,messageId:row.mid,payload:p,payloadSha256:hash(p),
          ...(row.status==='draft'?{humanConfirmation:'/mailsend '+row.approval,expiresAt:row.created+600000}:{result:row.result ? JSON.parse(row.result) : null}),
          authority:'draft is not authorization; confirmation must be human and same runtime session'});
      } finally { db.close(); }
    }
  });
  const command = {name:'mailsend',description:'Confirm the exact prepared mail in this session (human only).',
    acceptsArgs:true,requireAuth:true,requiredScopes:['operator.write'],
    async handler(ctx) {
      const origin=identity(ctx,true);
      if(!/^[a-f0-9]{48}$/.test(ctx.args ?? '')) fail('confirmation_invalid');
      const db=openLedger(config.dbPath);
      try {
        const row=transaction(db,()=>ctx.assertOwnerCurrent(),()=>{
          const row=db.prepare('SELECT * FROM mail_outbound_v1 WHERE approval=?').get(ctx.args);
          if(!row || row.origin!==JSON.stringify(origin)) fail('confirmation_origin_mismatch');
          if(row.status!=='draft') return {...row,replay:true};
          if(now() < row.created || now()-row.created > 600000) fail('confirmation_expired');
          // The immutable origin and Message-ID are durable BEFORE entering any SMTP code.
          // This is a one-shot intent: crash, timeout and Sent failure NEVER permit SMTP replay.
          db.prepare("UPDATE mail_outbound_v1 SET status='inflight' WHERE op=? AND status='draft'").run(row.op);
          return row;
        });
        if(row.replay) return {text:JSON.stringify({status:row.status,messageId:row.mid,replay:true})};
        ctx.assertOwnerCurrent();
        let result;
        try { result=await send(config,{messageId:row.mid,...JSON.parse(row.payload)}); }
        catch { result={status:'unknown',sentCopy:'unknown'}; }
        if(!result || !['accepted','partial','refused','unknown','not_sent'].includes(result.status)) result={status:'unknown',sentCopy:'unknown'};
        // Settlement of accepted work is bookkeeping, not a new effect. It may outlive authority.
        transaction(db,()=>{},()=>db.prepare("UPDATE mail_outbound_v1 SET status=?,result=? WHERE op=? AND status='inflight'").run(result.status,JSON.stringify(result),row.op));
        return {text:JSON.stringify({messageId:row.mid,...result,resend:'forbidden; reconcile unknown outcomes manually'})};
      } finally { db.close(); }
    }};
  return {factory:{contextVersion:2,create},command};
}
export default {
  id:'agent-mail-origin',
  register(api) {
    const adapter=createMailAdapter(api.pluginConfig ?? {});
    api.registerTool(adapter.factory,{name:'mail_prepare',optional:true,workspaceAccess:false});
    api.registerCommand(adapter.command);
  }
};
