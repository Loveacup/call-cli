#!/usr/bin/env node
// One-shot SSH client. No daemon, credential storage, or remote-job ownership.
'use strict';
const {spawn}=require('node:child_process');
const argv=process.argv.slice(2), split=argv.indexOf('--');
if(argv.includes('--help')) {
  console.log('node remote-client.cjs --host SSH_ALIAS --entry /path/collab.py --db /path/tasks.sqlite3 [--python python3] -- ACTION [ARGS]');
  process.exit(0);
}
const opts={};
for(let i=0;i<(split<0?argv.length:split);i+=2) opts[argv[i]]=argv[i+1];
const args=split<0?[]:argv.slice(split+1);
const fail=reason=>{console.log(JSON.stringify({error:reason,do_not_replay:true}));process.exit(1)};
if(!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(opts['--host']||'') ||
   !opts['--entry']?.startsWith('/') || !opts['--db']?.startsWith('/') || !args.length)
  fail('host_alias_absolute_entry_db_and_action_required');
const limit=Number(opts['--timeout-ms']||45000);
if(!Number.isFinite(limit)||limit<=0||limit>120000)fail('invalid_transport_timeout');
const quote=s=>"'"+s.replaceAll("'","'\\''")+"'";
const remote='export PATH=/opt/homebrew/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH; exec '+
  [opts['--python']||'python3',opts['--entry'],'--db',opts['--db'],...args].map(quote).join(' ');
const child=spawn(opts['--ssh-bin']||'ssh',['-n','-T','-o','BatchMode=yes','-o','ConnectTimeout=8',opts['--host'],remote],
                  {stdio:['ignore','pipe','pipe']});
let output='',done=false;
function finish(value,code) {
  if(done)return;
  done=true;
  clearTimeout(timer);
  console.log(JSON.stringify(value));
  // A complete CLI JSON reply has been received. Closing this owned transport
  // does NOT cancel, restart, or infer death of a remote collaboration worker.
  child.kill();
  process.exit(code);
}
const unknown=()=>finish({error:'response_unknown',do_not_replay:true},1);
const timer=setTimeout(unknown,limit);
child.stdout.setEncoding('utf8');
child.stdout.on('data',chunk=>{
  output+=chunk.toString('utf8');
  if(output.length>4*1024*1024){unknown();return}
  let reply;
  try{reply=JSON.parse(output)}catch{return}
  finish(reply,reply&&reply.error?1:0);
});
child.stderr.resume(); // Never echo arbitrary SSH/provider diagnostics.
child.on('error',unknown);
child.on('exit',()=>{if(!done)unknown()});
