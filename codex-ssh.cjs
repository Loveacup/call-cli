'use strict';
// Desktop execution-context bridge, not an OS security boundary.
const net=require('node:net');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const {once}=require('node:events');
const {spawn}=require('node:child_process');
async function send(s, value) { if(!s.write(JSON.stringify(value)+'\n')) await once(s,'drain'); }
function checkFrame(f){
 if(!f||typeof f!=='object'||Array.isArray(f))throw Error('invalid frame');
 const fields={hello:['type','token'],go:['type'],eof:['type'],stdin:['type','data'],stdout:['type','data'],stderr:['type','data'],exit:['type','code']}[f.type];
 if(!fields||Object.keys(f).length!==fields.length||!fields.every(k=>Object.hasOwn(f,k)))throw Error('invalid frame schema');
 if(f.type==='hello'&&!/^[a-f0-9]{64}$/.test(f.token))throw Error('invalid capability');
 if('data' in f&&(typeof f.data!=='string'||f.data.length>21848||Buffer.from(f.data,'base64').toString('base64')!==f.data))throw Error('invalid frame bytes');
 if(f.type==='exit'&&(!Number.isInteger(f.code)||f.code<0||f.code>4294967295))throw Error('invalid exit');
 return f;
}
async function* frames(s) {
 let pending='';
 for await(const b of s) {
  const text=Buffer.isBuffer(b)?b.toString('utf8'):b;
  for(const part of text.split(/(?<=\n)/)) {
   pending+=part;if(Buffer.byteLength(pending)>32768)throw Error('frame too large');
   if(pending.endsWith('\n')) {yield checkFrame(JSON.parse(pending));pending='';}
  }
 }
 if(pending)throw Error('partial frame');
}
async function bytes(s,type,src){for await(const chunk of src){const b=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);for(let i=0;i<b.length;i+=16384)await send(s,{type,data:b.subarray(i,i+16384).toString('base64')});}}
async function write(s,b){if(!s.write(b))await once(s,'drain');}
async function bounded(p,ms,label){let timer;try{return await Promise.race([p,new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error(label)),ms);})]);}finally{clearTimeout(timer);}}
async function worker(c,d={}) {
 const s=net.connect(c.pipe);s.on('error',()=>{});
 let child,output,completed=false,eof=false;
 try {
  await once(s,'connect');await send(s,{type:'hello',token:c.token});
  for await(const f of frames(s)) {
   if(!child&&f.type==='go'){
    child=d.spawnChild(c);child.stdin.on('error',()=>{});
    output=Promise.all([bytes(s,'stdout',child.stdout),bytes(s,'stderr',child.stderr),once(child,'close')]).then(async r=>{
     await send(s,{type:'exit',code:r[2][0]??1});completed=true;s.end();
    });output.catch(()=>s.destroy());
   } else if(child&&f.type==='stdin'&&!eof)await bounded(write(child.stdin,Buffer.from(f.data,'base64')),d.stallMs??15000,'stdin stalled');
   else if(child&&f.type==='eof'&&!eof){eof=true;child.stdin.end();}
   else throw Error('invalid worker protocol');
  }
  if(!completed)throw Error('transport closed');
  if(output)await output;
 } catch {
  let cleanupFailed=false;
  try{await d.orphanCleanup(c);}catch{cleanupFailed=true;}
  finally {if(child)await d.killTree(child.pid);}
  throw Error('DO_NOT_REPLAY: worker transport failure'+(cleanupFailed?'; cleanup failed':''));
 } finally {s.destroy();}
}
function validate(o) {
 if(typeof o.cwd!=='string'||! /^[A-Za-z]:[\\/]/.test(o.cwd)||/[\x00-\x1f]/.test(o.cwd))throw Error('invalid cwd');
 if(!Number.isInteger(o.timeout)||o.timeout<1||o.timeout>3600)throw Error('invalid timeout');
 if(!Array.isArray(o.args)||!['exec','app-server'].includes(o.args[0]))throw Error('unsupported command');
 for(const a of o.args)if(typeof a!=='string'||/\x00/.test(a)||/^(-c|--config(?:=|$)|--yolo(?:=|$)|--dangerously-bypass-approvals-and-sandbox(?:=|$))/.test(a)||a.includes('danger-full-access'))throw Error('unsafe option');
 if(o.args[0]==='app-server'&&(o.args.length!==2||o.args[1]!=='--stdio'))throw Error('unsupported app-server options');
 if(o.args[0]==='exec'&&o.args.slice(1).some(a=>!['-','--json','--ephemeral','--skip-git-repo-check'].includes(a)))throw Error('unsupported exec option: use stdin for prompts');
 if(o.codexBin&&(!path.win32.isAbsolute(o.codexBin)||! /\.(ps1|cmd|exe)$/i.test(o.codexBin)))throw Error('invalid codex-bin');
 return o;
}
function parse(argv){
 if(argv.length===1&&argv[0]==='--help')return {help:true};
 const o={timeout:900};let i=0;
 while(i<argv.length&&argv[i]!=='--') {const k=argv[i++],v=argv[i++]; if(v===undefined)throw Error('invalid option');if(k==='--cwd')o.cwd=v;else if(k==='--timeout-seconds')o.timeout=Number(v);else if(k==='--codex-bin')o.codexBin=v;else throw Error('unsupported option');}
 if(argv[i++]!=='--')throw Error('invalid command separator');o.args=argv.slice(i);return validate(o);
}
async function parent(o,d={}) {
 if((d.platform||process.platform)!=='win32')throw Error('Windows required');
 validate(o);
 const scheduler=d.scheduler||windowsScheduler(runPS);
 if(!await scheduler.desktop(o))throw Error('no logged-in desktop');
 const task='CodexSsh-'+crypto.randomUUID(); const run=await fs.mkdtemp(path.join(d.base||os.tmpdir(),'codex-ssh-'));
 const c={...o,task,token:crypto.randomBytes(32).toString('hex'),pipe:d.pipe||{host:'127.0.0.1',port:0},run,config:path.join(run,'config.json'),node:process.execPath,entry:__filename};
 const server=net.createServer(); let socket;
 const peers=new Set();
 const done=new Promise((resolve,reject)=>server.on('connection',s=>{
 peers.add(s);s.once('close',()=>peers.delete(s));s.on('error',()=>{});
 let authenticated=false; const authTimer=setTimeout(()=>s.destroy(),1000);
 (async()=>{for await(const f of frames(s)){
 if(!authenticated){
  if(f.type!=='hello'||socket||!crypto.timingSafeEqual(Buffer.from(f.token),Buffer.from(c.token)))throw Error('authentication');
  authenticated=true;socket=s;clearTimeout(authTimer);await send(s,{type:'go'});
  (async()=>{await bytes(s,'stdin',d.stdin||process.stdin);await send(s,{type:'eof'});})().catch(reject);
 } else if(f.type==='stdout'||f.type==='stderr')await write(d[f.type]||process[f.type],Buffer.from(f.data,'base64'));
 else if(f.type==='exit'){resolve(f.code);return;}
 else throw Error('invalid protocol direction');
 }if(authenticated)reject(Error('transport closed'));})().catch(e=>{if(authenticated)reject(e);s.destroy();}).finally(()=>clearTimeout(authTimer));}));
 done.catch(()=>{});
 const input=d.stdin||process.stdin,signals=d.signals||process;
 let cancelled;
 let interrupt;const interrupted=new Promise((_,reject)=>{interrupt=e=>{cancelled=e;reject(e);};});interrupted.catch(()=>{});
 const onInt=()=>interrupt(Error('SIGINT')),onTerm=()=>interrupt(Error('SIGTERM'));
 signals.on('SIGINT',onInt);signals.on('SIGTERM',onTerm);
 let attempted=false,readyTimer,totalTimer;
 const readyDeadline=new Promise((_,reject)=>{readyTimer=setTimeout(()=>{if(!socket)reject(cancelled=Error('ready timeout'));},d.readyMs??15000);});
 const totalDeadline=new Promise((_,reject)=>{totalTimer=setTimeout(()=>reject(cancelled=Error('total timeout')),d.totalMs??o.timeout*1000);});
 readyDeadline.catch(()=>{});totalDeadline.catch(()=>{});
 try {
  await new Promise((res,rej)=>{server.once('error',rej);server.listen(c.pipe,res);});
  if(typeof c.pipe==='object'){
   const address=server.address();
   if(!address||address.address!=='127.0.0.1'||!Number.isInteger(address.port)||address.port<1)throw Error('invalid local endpoint');
   c.pipe={host:'127.0.0.1',port:address.port};
  }
  if(cancelled)throw cancelled;
  attempted=true;await scheduler.register(c);
  if(cancelled)throw cancelled;
  await fs.writeFile(c.config,JSON.stringify(c),{flag:'wx',mode:0o600});
  if(cancelled)throw cancelled;
  await scheduler.start(c);return await Promise.race([done,readyDeadline,totalDeadline,interrupted]);
 }
 catch(e){throw Error((attempted?'DO_NOT_REPLAY: ':'')+e.message);}
 finally {
  signals.off('SIGINT',onInt);signals.off('SIGTERM',onTerm);input.destroy();
  clearTimeout(totalTimer);clearTimeout(readyTimer);for(const s of peers)s.destroy();await new Promise(r=>server.close(r));
  try{if(attempted)await scheduler.cleanup(c);}catch{throw Error('DO_NOT_REPLAY: cleanup failed');}
  finally{await fs.rm(run,{recursive:true,force:true});}
 }
}
function psData(value){return "$c = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+Buffer.from(JSON.stringify(value)).toString('base64')+"')) | ConvertFrom-Json\n";}
const COM = "$ErrorActionPreference='Stop'\n$s=New-Object -ComObject 'Schedule.Service'; $s.Connect(); $f=$s.GetFolder('\\')\n";
function windowsScheduler(run){
 let desktop;
 const cleanup=async(c,stop)=>{
  if(!/^CodexSsh-[a-f0-9-]{36}$/.test(c.task))throw Error('invalid owned task');
  return run(COM+psData({task:c.task})+`
function Find-Owned { try { return $f.GetTask($c.task) } catch { if ($_.Exception.HResult -eq -2147024894) { return $null }; throw } }
$t=Find-Owned
$stopFailed=$false
if ($null -ne $t) {
 ${stop?"try { $t.Stop(0) } catch { $stopFailed=$true }":""}
 try { $f.DeleteTask($c.task,0) } catch { if ($_.Exception.HResult -ne -2147024894) { throw } }
}
if ($null -ne (Find-Owned)) { throw 'owned task still present' }
if ($stopFailed) { throw 'owned task Stop failed' }
`);
 };
 return {
  async desktop(o){
   desktop=JSON.parse(await run("$ErrorActionPreference='Stop'\n"+psData(o)+`
if (-not (Test-Path -LiteralPath $c.cwd -PathType Container)) { throw 'invalid cwd' }
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$sid=$identity.User.Value
$sessions=@(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | ForEach-Object {
 $owner=Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid
 if ($owner.Sid -eq $sid -and $_.SessionId -gt 0) { [int]$_.SessionId }
} | Sort-Object -Unique)
if ($sessions.Count -ne 1) { throw 'no unambiguous logged-in desktop' }
$bin=$c.codexBin
if (-not $bin) { $bin=(Get-Command codex -CommandType ExternalScript,Application -ErrorAction Stop | Select-Object -First 1).Source }
if (-not [IO.Path]::IsPathRooted($bin) -or -not (Test-Path -LiteralPath $bin -PathType Leaf)) { throw 'invalid codex entry' }
@{sid=$sid;session=$sessions[0];codexBin=$bin} | ConvertTo-Json -Compress
`));
   return Boolean(desktop?.sid&&Number.isInteger(desktop.session)&&desktop.session>0&&desktop.codexBin);
  },
  async register(c){
   if(!desktop)throw Error('desktop preflight required');
   Object.assign(c,desktop);
   const {task,timeout,node,entry,config,cwd,sid}=c;
   return run(COM+psData({task,timeout,node,entry,config,cwd,sid})+`
$d=$s.NewTask(0)
$d.Principal.UserId=$c.sid
$d.Principal.LogonType=3
$d.Principal.RunLevel=0
$d.Settings.Enabled=$true
$d.Settings.AllowDemandStart=$true
$d.Settings.DisallowStartIfOnBatteries=$false
$d.Settings.StopIfGoingOnBatteries=$false
$d.Settings.ExecutionTimeLimit='PT'+[string]($c.timeout+30)+'S'
$a=$d.Actions.Create(0)
$a.Path=$c.node
$a.Arguments='"'+$c.entry+'" --desktop-worker "'+$c.config+'"'
$a.WorkingDirectory=$c.cwd
$null=$f.RegisterTaskDefinition($c.task,$d,2,$c.sid,$null,3,$null)
`);
  },
  async start(c){return run(COM+psData({task:c.task,session:c.session})+"$null=$f.GetTask($c.task).RunEx($null,4,$c.session,$null)");},
  cleanup:c=>cleanup(c,true),orphanCleanup:c=>cleanup(c,false)
 };
}
function powershell(){return path.win32.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');}
function psArgs(script){const utf8="$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);\n";return ['-NoLogo','-NoProfile','-NonInteractive','-EncodedCommand',Buffer.from(utf8+script,'utf16le').toString('base64')];}
function spawnCodex(c,launch=spawn){
 const script="$ErrorActionPreference='Stop'\n"+psData({codexBin:c.codexBin,args:c.args})+"$argv=@($c.args | ForEach-Object { [string]$_ }); & $c.codexBin @argv; exit $LASTEXITCODE";
 return launch(powershell(),psArgs(script),{cwd:c.cwd,stdio:['pipe','pipe','pipe'],shell:false,windowsHide:false});
}
async function runPS(script,d={}) {
 const child=(d.launch||spawn)(powershell(),psArgs(script),{stdio:['ignore','pipe','pipe'],shell:false,windowsHide:true});
 const result=[];let size=0,overflow=false,timedOut=false;
 const timer=setTimeout(()=>{timedOut=true;child.kill();},d.timeoutMs??10000);
 child.stdout.on('data',b=>{if(size+b.length>65536){overflow=true;child.kill();}else {size+=b.length;result.push(Buffer.from(b));}});
 child.stderr.resume();
 let code;try{[code]=await once(child,'close');}finally{clearTimeout(timer);}
 if(timedOut)throw Error('control timeout');
 if(overflow)throw Error('control output limit');
 if(code!==0)throw Error('PowerShell control failed');
 return Buffer.concat(result).toString('utf8').trim();
}
async function killOwnTree(_childPid,launch=spawn){
 // Delete the registration BEFORE this call: killing this worker prevents later cleanup.
 const killer=launch(path.win32.join(process.env.SystemRoot||'C:\\Windows','System32','taskkill.exe'),['/PID',String(process.pid),'/T','/F'],{stdio:'ignore',shell:false,windowsHide:true});
 const [code]=await once(killer,'close');if(code!==0)throw Error('DO_NOT_REPLAY: own tree cleanup failed');
}
async function main(argv,d={}){
 if(argv[0]==='--desktop-worker'){
  if((d.platform||process.platform)!=='win32')throw Error('Windows required');
  if(argv.length!==2||!path.isAbsolute(argv[1])||path.basename(argv[1])!=='config.json'||!path.basename(path.dirname(argv[1])).startsWith('codex-ssh-'))throw Error('invalid worker config path');
  const stat=await fs.lstat(argv[1]);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>32768)throw Error('invalid worker config');
  const c=JSON.parse(await fs.readFile(argv[1],'utf8'));validate(c);
  if(c.config!==argv[1]||c.run!==path.dirname(argv[1])||!/^CodexSsh-[a-f0-9-]{36}$/.test(c.task)||! /^[a-f0-9]{64}$/.test(c.token))throw Error('invalid worker identity');
  await worker(c,d.workerDeps||{spawnChild:spawnCodex,orphanCleanup:async()=>{await windowsScheduler(runPS).orphanCleanup(c);await fs.rm(c.run,{recursive:true,force:true});},killTree:killOwnTree});return 0;
 }
 const o=parse(argv);
 if(o.help){process.stdout.write('node codex-ssh.cjs --cwd ABS_WINDOWS_DIR [--timeout-seconds 1..3600] [--codex-bin ABS_ENTRY] -- app-server --stdio | exec -\nDefault timeout: 900 seconds. No TUI. Prompts must arrive on stdin. No automatic replay.\n');return 0;}
 return parent(o,d);
}
if(require.main===module)main(process.argv.slice(2)).then(code=>{process.exitCode=code;}).catch(e=>{process.stderr.write('codex-ssh: '+e.message+'\n');process.exitCode=1;});
module.exports={parent,worker,frames,validate,parse,windowsScheduler,main,spawnCodex,runPS,killOwnTree};
