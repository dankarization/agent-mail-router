// A single operator-scoped Gateway RPC, with request body only on stdin.
// No tokens or mail content in argv, env, diagnostic output, or temporary files.
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
const output=process.stdout.write.bind(process.stdout);
const discard=(_value, encoding, callback)=>{const done=typeof encoding==='function'?encoding:callback;if(typeof done==='function')queueMicrotask(done);return true};
process.stdout.write=discard;process.stderr.write=discard;
let reply={status:'error',error:'gateway_unavailable'};
try {
  const root=process.argv[2];
  if(!root || process.argv.length!==3) throw Error('input');
  let size=0;const chunks=[];
  for await(const chunk of process.stdin){size+=chunk.length;if(size>32768)throw Error('input');chunks.push(chunk)}
  const input=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!input || !['sessions.list','chat.inject'].includes(input.method) || !input.params || typeof input.params!=='object' || Array.isArray(input.params))throw Error('input');
  const sdk=await import(pathToFileURL(resolve(root,'dist/plugin-sdk/gateway-runtime.js')).href);
  if(typeof sdk.callGatewayFromCli!=='function')throw Error('runtime');
  const value=await sdk.callGatewayFromCli(input.method,{timeout:'20000'},input.params,{defaultTimeoutMs:20000,sharedStateMode:'read-only'});
  reply={status:'ok',value};
  if(Buffer.byteLength(JSON.stringify(reply))>8*1024*1024)throw Error('response');
} catch { reply={status:'error',error:'gateway_unavailable'}; }
output(JSON.stringify(reply)+'\n');
