// Invoke the ordinary CLI bootstrap; request JSON remains on stdin and in
// process memory, never in the operating system argument vector or a file.
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
try {
  const root=process.argv[2];
  if(!root || process.argv.length!==3) throw Error('input');
  let size=0;const chunks=[];
  for await(const chunk of process.stdin){size+=chunk.length;if(size>96*1024)throw Error('input');chunks.push(chunk)}
  const input=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!input || !['sessions.list','chat.inject'].includes(input.method) || !input.params || typeof input.params!=='object' || Array.isArray(input.params))throw Error('input');
  const entry=resolve(root,'dist/index.js');
  process.argv=[process.execPath,entry,'gateway','call',input.method,'--params',JSON.stringify(input.params),'--timeout','20000','--json'];
  await import(pathToFileURL(entry).href);
} catch { process.exitCode=1; }
