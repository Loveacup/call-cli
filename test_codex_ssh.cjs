'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {PassThrough, Readable} = require('node:stream');
const {spawn} = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const api = () => require('./codex-ssh.cjs');
function sink() { const s = new PassThrough(); const chunks=[]; s.on('data', b=>chunks.push(b)); s.bytes=()=>Buffer.concat(chunks); return s; }
async function fixture(t, overrides={}) {
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'cssh-test-'));
  t.after(()=>fs.rm(dir,{recursive:true,force:true}));
  const fake=path.join(dir,'fake.cjs');
  await fs.writeFile(fake, 'process.stdin.pipe(process.stdout);process.stdin.on("end",()=>{process.stderr.write(Buffer.from([0,255,13,10]));process.exitCode=7});');
  const calls=[]; let worker;
  const scheduler={desktop:async()=>true, register:async c=>{calls.push(['register',c.task]);}, start:async c=>{worker=api().worker(c,{spawnChild:()=>spawn(process.execPath,[fake],{stdio:['pipe','pipe','pipe']}),orphanCleanup:async()=>calls.push(['orphan',c.task])}); worker.catch(()=>{});},cleanup:async c=>calls.push(['cleanup',c.task]),...overrides};
  return {dir,calls,scheduler,worker:()=>worker};
}
test('validation refuses platform cwd timeout unsafe flags without scheduling', async()=>{
 let calls=0;const d={platform:'linux',scheduler:{register:()=>calls++}};
 await assert.rejects(api().parent({cwd:'C:\\work',args:['exec'],timeout:5},d),/Windows/);
 for(const o of [{cwd:'relative'},{timeout:0},{timeout:90000},{args:['exec','--yolo']},{args:['exec','-cfoo=1']},{args:['exec','--config','x=1']},{args:['app-server','--stdio','--sandbox=danger-full-access']},{args:[]}]) {
  assert.throws(()=>api().validate({cwd:'C:\\\\work',args:['exec'],timeout:5,...o}),/invalid|unsafe|unsupported/);
 }
 assert.equal(calls,0);
 assert.equal(api().parse(['--help']).help,true);
 assert.deepEqual(api().parse(['--cwd','C:\\\\work','--','app-server','--stdio']).args,['app-server','--stdio']);
});
test('frames bound size validate schema and preserve fragmented frames',async()=>{
 const collect=async chunks=>{const a=[];for await(const f of api().frames(Readable.from(chunks)))a.push(f);return a;};
 assert.deepEqual(await collect(['{"type":"eo','f"}\n']),[{type:'eof'}]);
 for(const data of ['x'.repeat(40000),'{}\n','{"type":"stdin","data":"!"}\n','{"type":"exit","code":-1}\n','{"type":"eof","extra":true}\n'])await assert.rejects(collect([data]));
});
test('unauthorized connection cannot terminate or launch legitimate session',async t=>{
 const f=await fixture(t); const start=f.scheduler.start;
 f.scheduler.start=async c=>{
  const net=require('node:net'); const bad=net.connect(c.pipe);bad.on('error',()=>{});
  await new Promise(r=>bad.once('connect',r));bad.end(JSON.stringify({type:'hello',token:'0'.repeat(64)})+'\n');
  await new Promise(r=>bad.once('close',r));await start(c);
 };
 assert.equal(await api().parent({cwd:'C:\\\\work',args:['exec','-'],timeout:5},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([]),stdout:sink(),stderr:sink()}),7);
 await f.worker();
});
test('no desktop fails before registration',async t=>{
 const f=await fixture(t,{desktop:async()=>false});
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([]),stdout:sink(),stderr:sink()}),/desktop/);
 assert.deepEqual(f.calls,[]);
});
test('registered start failure is not replayable and exact cleanup failure is surfaced',async t=>{
 const f=await fixture(t,{start:async()=>{throw Error('start failed');}});
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe')}),/DO_NOT_REPLAY/);
 assert.equal(f.calls[1][0],'cleanup');assert.equal(f.calls[0][1],f.calls[1][1]);
 f.scheduler.cleanup=async()=>{throw Error('cleanup failed');};
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe2')}),/DO_NOT_REPLAY.*cleanup/);
});
test('ready deadline fails closed and cleans exact task',async t=>{
 const f=await fixture(t);const start=f.scheduler.start;
 f.scheduler.start=async c=>{const timer=setTimeout(()=>start(c).catch(()=>{}),200);t.after(()=>clearTimeout(timer));};
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),readyMs:30,stdin:Readable.from([]),stdout:sink(),stderr:sink()}),/DO_NOT_REPLAY.*ready timeout/);
 assert.equal(f.calls[1][0],'cleanup');
});
test('worker parent death deletes own task before killing own child tree',async t=>{
 const net=require('node:net');const f=await fixture(t);const pipe=path.join(f.dir,'orphan');const events=[];let child;
 const server=net.createServer(s=>{s.once('data',()=>{s.write('{"type":"go"}\n');setTimeout(()=>s.destroy(),50);});});
 await new Promise(r=>server.listen(pipe,r));t.after(()=>new Promise(r=>server.close(r)));
 await assert.rejects(api().worker({pipe,token:'a'.repeat(64)}, {spawnChild:()=>{child=spawn(process.execPath,['-e','setTimeout(()=>{},500)']);return child;},orphanCleanup:async()=>events.push('delete'),killTree:async p=>{events.push('kill');assert.equal(p,child.pid);child.kill();}}),/DO_NOT_REPLAY/);
 assert.deepEqual(events,['delete','kill']);
});
test('total deadline bounds authenticated session and cleans',async t=>{
 const f=await fixture(t);const net=require('node:net');let peer;
 f.scheduler.start=async c=>{peer=net.connect(c.pipe);peer.on('error',()=>{});peer.on('data',()=>{});await new Promise(r=>peer.once('connect',r));peer.write(JSON.stringify({type:'hello',token:c.token})+'\n');setTimeout(()=>peer.end('{"type":"exit","code":0}\n'),150);};
 t.after(()=>peer?.destroy());
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),totalMs:30,stdin:Readable.from([]),stdout:sink(),stderr:sink()}),/DO_NOT_REPLAY.*total timeout/);
 assert.equal(f.calls[1][0],'cleanup');
});
test('Windows COM plan uses InteractiveToken lowest privilege exact ownership and encoded data',async()=>{
 const calls=[]; const scheduler=api().windowsScheduler(async script=>{calls.push(script);return JSON.stringify({sid:'S-1-test',session:1,codexBin:'C:\\\\bin\\\\codex.ps1'});});
 assert.equal(await scheduler.desktop({cwd:'C:\\\\work',codexBin:'C:\\\\bin\\\\codex.ps1'}),true);
 const c={task:'CodexSsh-11111111-1111-4111-8111-111111111111',timeout:900,node:'C:\\\\node.exe',entry:'C:\\\\bridge.cjs',config:'C:\\\\run\\\\config.json',cwd:'C:\\\\work'};
 await scheduler.register(c);await scheduler.start(c);await scheduler.cleanup(c);await scheduler.orphanCleanup(c);
 assert.match(calls[1],/LogonType\s*=\s*3/);assert.match(calls[1],/RunLevel\s*=\s*0/);assert.match(calls[1],/RegisterTaskDefinition/);
 assert.match(calls[1],/ExecutionTimeLimit/);assert.match(calls[1],/--desktop-worker/);assert.doesNotMatch(calls[1],/Triggers.Create|Password|HighestAvailable/);
 assert.match(calls[2],/RunEx/);assert.match(calls[3],/\.Stop\(0\)/);assert.match(calls[3],/DeleteTask/);assert.match(calls[3],/GetTask/);
 assert.doesNotMatch(calls[4],/\.Stop\(/);assert.match(calls[4],/DeleteTask/);
 assert.equal(c.codexBin,'C:\\\\bin\\\\codex.ps1');assert.ok(!calls.join('').includes(c.cwd));
});
test('actual CLI help is side effect free and non Windows refuses',async()=>{
 const run=async args=>{const p=spawn(process.execPath,[path.join(__dirname,'codex-ssh.cjs'),...args]);const out=sink(),err=sink();p.stdout.pipe(out);p.stderr.pipe(err);const [code]=await require('node:events').once(p,'close');return {code,out:out.bytes().toString(),err:err.bytes().toString()};};
 const help=await run(['--help']);assert.equal(help.code,0);assert.match(help.out,/--desktop-worker|--cwd/);
 if(process.platform!=='win32'){const r=await run(['--cwd','C:\\\\work','--','exec','-']);assert.notEqual(r.code,0);assert.equal(r.out,'');assert.match(r.err,/Windows/);}
});
test('config stays private to run directory and never stores prompt bodies',async t=>{
 for(const args of [['exec','secret prompt'],['exec','--profile','other'],['exec','--enable','feature'],['exec','--','secret']])assert.throws(()=>api().validate({cwd:'C:\\\\work',args,timeout:1}),/unsupported/);
 const f=await fixture(t);const start=f.scheduler.start;let configPath;
 f.scheduler.start=async c=>{configPath=c.config;assert.equal(path.dirname(c.config),c.run);assert.equal(path.isAbsolute(c.node),true);assert.equal(c.entry,path.join(__dirname,'codex-ssh.cjs'));const saved=JSON.parse(await fs.readFile(c.config,'utf8'));assert.equal(saved.token.length,64);assert.equal(saved.task,c.task);assert.ok(!JSON.stringify(saved).includes('secret prompt'));await start(c);};
 assert.equal(await api().parent({cwd:'C:\\\\work',args:['exec','-'],timeout:5},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from(['secret prompt']),stdout:sink(),stderr:sink()}),7);
 await assert.rejects(fs.stat(configPath),{code:'ENOENT'});await f.worker();
});
test('Windows child launch uses encoded PowerShell installed entry without shell interpolation',()=>{
 let got; const c={cwd:'C:\\\\work',codexBin:"C:\\\\bin\\\\codex.ps1",args:['exec','-'],config:'C:\\\\run\\\\config.json'};
 const marker={};assert.equal(api().spawnCodex(c,(...a)=>{got=a;return marker;}),marker);
 assert.equal(path.win32.isAbsolute(got[0]),true);assert.equal(got[2].shell,false);
 const script=Buffer.from(got[1].at(-1),'base64').toString('utf16le');assert.match(script,/& \$c.codexBin @argv/);assert.doesNotMatch(script,/dangerously|Bypass|danger-full-access/);assert.ok(!script.includes(c.codexBin));assert.deepEqual(got[2].stdio,['pipe','pipe','pipe']);
});
test('regression: real loopback transport forwards bytes and releases listener',async t=>{
 const f=await fixture(t);const original=f.scheduler.start;let endpoint;
 f.scheduler.start=async c=>{endpoint=c.pipe;assert.equal(endpoint.host,'127.0.0.1');assert.ok(endpoint.port>0);await original(c);};
 const payload=crypto.randomBytes(40000),out=sink();
 assert.equal(await api().parent({cwd:'C:\\\\work',args:['exec','-'],timeout:5},{platform:'win32',scheduler:f.scheduler,base:f.dir,stdin:Readable.from([payload]),stdout:out,stderr:sink()}),7);
 assert.deepEqual(out.bytes(),payload);await f.worker();
 await new Promise((resolve,reject)=>{const s=require('net').connect(endpoint);s.once('connect',()=>{s.destroy();reject(Error('listener leaked'))});s.once('error',e=>e.code==='ECONNREFUSED'?resolve():reject(e));});
});
test('default endpoint binds loopback only without opening a named pipe',async t=>{
 const net=require('node:net'),original=net.createServer;let observed;
 const f=await fixture(t);
 net.createServer=(...args)=>{const s=original(...args);s.listen=(options)=>{observed=options;queueMicrotask(()=>s.emit('error',Error('observed endpoint')));return s;};return s;};
 t.after(()=>net.createServer=original);
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec','-'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,stdin:Readable.from([]),stdout:sink(),stderr:sink()}),/observed endpoint/);
 assert.deepEqual(observed,{host:'127.0.0.1',port:0});
});
test('control output preserves split UTF8 and explicitly configures Windows encoding',async()=>{
 const {EventEmitter}=require('node:events');let script;
 const launch=(bin,args)=>{
  script=Buffer.from(args.at(-1),'base64').toString('utf16le');
  const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.kill=()=>{};
  setImmediate(()=>{const b=Buffer.from('路径中文','utf8');child.stdout.write(b.subarray(0,1));child.stdout.write(b.subarray(1,5));child.stdout.end(b.subarray(5));child.stderr.end();child.emit('close',0);});
  return child;
 };
 assert.equal(await api().runPS('Write-Output test',{launch}),'路径中文');
 assert.match(script,/Console\]::OutputEncoding\s*=\s*.*UTF8Encoding/);
});
test('PowerShell control leaf captures bounded output and fails on nonzero',async()=>{
 const launch=code=>(bin,args,opts)=>{assert.equal(opts.shell,false);assert.equal(args[3],'-EncodedCommand');return spawn(process.execPath,['-e',code],opts);};
 assert.equal(await api().runPS('test',{launch:launch('process.stdout.write("ok")')}),'ok');
 await assert.rejects(api().runPS('test',{launch:launch('process.exitCode=9')}),/control failed/);
 await assert.rejects(api().runPS('test',{launch:launch('process.stdout.write("x".repeat(70000))')}),/control output limit/);
});
test('public parent and desktop-worker entry compose with real fake child',async t=>{
 const f=await fixture(t);let worker;
 f.scheduler.start=async c=>{worker=api().main(['--desktop-worker',c.config],{platform:'win32',workerDeps:{spawnChild:()=>spawn(process.execPath,['-e','process.stdin.pipe(process.stdout)']),orphanCleanup:async()=>{},killTree:async pid=>process.kill(pid)}});worker.catch(()=>{});};
 const out=sink();const code=await api().main(['--cwd','C:\\\\work','--','exec','-'],{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([Buffer.from('hello')]),stdout:out,stderr:sink()});
 assert.equal(code,0);assert.equal(out.bytes().toString(),'hello');await worker;
});
test('orphan kill leaf targets worker PID tree only',async()=>{
 let got;
 await api().killOwnTree(undefined,(...args)=>{got=args;return spawn(process.execPath,['-e','']);});
 assert.deepEqual(got[1],['/PID',String(process.pid),'/T','/F']);assert.equal(got[2].shell,false);assert.match(got[0],/taskkill.exe$/);
});
test('SIGTERM interrupts authenticated session and releases stdin listeners',async t=>{
 const {EventEmitter}=require('node:events');const signals=new EventEmitter();const input=new PassThrough();
 const f=await fixture(t);const net=require('node:net');let peer;
 f.scheduler.start=async c=>{peer=net.connect(c.pipe);peer.on('error',()=>{});peer.on('data',()=>{});await new Promise(r=>peer.once('connect',r));peer.write(JSON.stringify({type:'hello',token:c.token})+'\n');setTimeout(()=>signals.emit('SIGTERM'),30);setTimeout(()=>peer.end('{"type":"exit","code":0}\n'),120);};
 t.after(()=>peer?.destroy());
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),signals,stdin:input,stdout:sink(),stderr:sink()}),/DO_NOT_REPLAY.*SIGTERM/);
 assert.equal(input.destroyed,true);assert.equal(signals.listenerCount('SIGTERM'),0);assert.equal(f.calls[1][0],'cleanup');
});
test('control subprocess timeout is bounded and nonzero',async()=>{
 const launch=()=>spawn(process.execPath,['-e','setTimeout(()=>{},300)'],{stdio:['ignore','pipe','pipe']});
 await assert.rejects(api().runPS('test',{launch,timeoutMs:20}),/control timeout/);
});
test('worker detects parent death while child stdin is backpressured',async t=>{
 const f=await fixture(t),net=require('node:net'),pipe=path.join(f.dir,'blocked');let child;
 const frame=JSON.stringify({type:'stdin',data:Buffer.alloc(16384).toString('base64')})+'\n';
 const server=net.createServer(s=>{s.on('error',()=>{});s.once('data',()=>{s.write('{"type":"go"}\n'+frame.repeat(128));setTimeout(()=>s.destroy(),40);});});
 await new Promise(r=>server.listen(pipe,r));t.after(()=>new Promise(r=>server.close(r)));
 const started=Date.now();
 await assert.rejects(api().worker({pipe,token:'b'.repeat(64)},{stallMs:80,spawnChild:()=>child=spawn(process.execPath,['-e','setTimeout(()=>{},700)']),orphanCleanup:async()=>{},killTree:async()=>child.kill()}),/DO_NOT_REPLAY/);
 assert.ok(Date.now()-started<400,'disconnect must not wait for child stdin drain');
});
test('expired registration never starts business command',async t=>{
 const f=await fixture(t);let starts=0;const register=f.scheduler.register;
 f.scheduler.register=async c=>{await register(c);await new Promise(r=>setTimeout(r,60));};f.scheduler.start=async()=>{starts++;};
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),totalMs:20,stdin:Readable.from([])}),/DO_NOT_REPLAY.*total timeout/);
 assert.equal(starts,0);assert.equal(f.calls[1][0],'cleanup');
});
test('regression: registration error cleans exact attempted task',async t=>{
 const f=await fixture(t);let task;
 f.scheduler.register=async c=>{task=c.task;throw Error('register failed');};
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([])}),/DO_NOT_REPLAY/);
 assert.deepEqual(f.calls,[['cleanup',task]]);
});
test('regression: authenticated transport loss fails closed',async t=>{
 const f=await fixture(t),net=require('node:net');
 f.scheduler.start=async c=>{const peer=net.connect(c.pipe);peer.on('error',()=>{});peer.once('connect',()=>peer.write(JSON.stringify({type:'hello',token:c.token})+'\n'));peer.once('data',()=>peer.destroy());};
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([])}),/DO_NOT_REPLAY/);
 assert.equal(f.calls[1][0],'cleanup');
});
test('regression: rejected worker handshake cannot launch child',async t=>{
 const f=await fixture(t),net=require('node:net'),pipe=path.join(f.dir,'reject');let launches=0,cleaned=0;
 const server=net.createServer(s=>s.once('data',()=>s.end('{"type":"eof"}\n')));await new Promise(r=>server.listen(pipe,r));t.after(()=>new Promise(r=>server.close(r)));
 await assert.rejects(api().worker({pipe,token:'c'.repeat(64)},{spawnChild:()=>launches++,orphanCleanup:async()=>cleaned++}),/DO_NOT_REPLAY/);
 assert.equal(launches,0);assert.equal(cleaned,1);
});
test('regression: cleanup failure overrides normal child exit',async t=>{
 const f=await fixture(t,{cleanup:async()=>{throw Error('delete failed');}});
 await assert.rejects(api().parent({cwd:'C:\\\\work',args:['exec'],timeout:1},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([]),stdout:sink(),stderr:sink()}),/DO_NOT_REPLAY.*cleanup/);await f.worker();
});
test('registration control command never includes capability even encoded',async()=>{
 let script;const scheduler=api().windowsScheduler(async s=>{script=s;return JSON.stringify({sid:'S-1',session:1,codexBin:'C:\\\\codex.ps1'});});await scheduler.desktop({cwd:'C:\\\\work'});
 await scheduler.register({task:'CodexSsh-'+crypto.randomUUID(),token:'d'.repeat(64),timeout:1});
 const blob=script.match(/FromBase64String\('([^']+)'\)/)[1];assert.ok(!Buffer.from(blob,'base64').toString().includes('d'.repeat(64)));
});
test('tracer: real socket child bytes EOF nonzero exit exact cleanup', async t=>{
 const f=await fixture(t), out=sink(), err=sink();
 const bytes=crypto.randomBytes(180000);
 const result=await api().parent({cwd:'C:\\work',args:['exec','-'],timeout:5},{platform:'win32',scheduler:f.scheduler,base:f.dir,pipe:path.join(f.dir,'pipe'),stdin:Readable.from([bytes]),stdout:out,stderr:err});
 assert.equal(result,7); assert.deepEqual(out.bytes(),bytes); assert.deepEqual(err.bytes(),Buffer.from([0,255,13,10]));
 assert.equal(f.calls.length,2); assert.equal(f.calls[0][0],'register'); assert.equal(f.calls[1][0],'cleanup'); assert.equal(f.calls[0][1],f.calls[1][1]);
 await f.worker();
});
