"use strict";
// External SDK stand-in: no sockets, provider, shared owner or model calls.
const fs = require('node:fs');
const path = require('node:path');
const mode = process.env.CALLCLI_DIAGNOSTIC_MODE;
const secret = process.env.CALLCLI_DIAGNOSTIC_CANARY;
const trace = value => fs.appendFileSync(path.join(process.env.CALLCLI_DIAGNOSTIC_ROOT, 'runtime.jsonl'), JSON.stringify(value) + '\n');
const fail = () => {throw Error(secret);};
if (mode === 'abrupt') {console.error(secret); process.exit(1);}
if (mode === 'load') fail();
exports.createSharedAcpRuntime = () => {
  if (mode === 'runtime') fail();
  return {
    startTurn({requestId}) {
      trace('prompt');
      if (mode === 'start') fail();
      const accepted = !['failed-unaccepted', 'result'].includes(mode);
      return {
        promptStarted: accepted ? Promise.resolve() : Promise.reject(Error(secret)),
        events: (async function* () {
          if (mode === 'malformed-diagnostic') {
            console.log(JSON.stringify({type:'callcli.shared.result', requestId, status:'failed', stopReason:null,
              errorCategory:'auth_required', errorSource:'turn_result',
              ...JSON.parse(process.env.CALLCLI_DIAGNOSTIC_FIELDS)}));
            return;
          }
          if (mode === 'invalid-frame') {
            await new Promise(resolve => setImmediate(resolve));
            console.log('null');
          }
          if (mode === 'events') fail();
          if (!mode.startsWith('failed') && mode !== 'result') {
            yield {type:'text_delta', stream:'output', tag:'agent_message_chunk', text:'SYNTHETIC_FINAL_ANSWER\n'};
          }
        })(),
        result: mode === 'result' ? Promise.reject(Error(secret)) : Promise.resolve(
          mode.startsWith('failed') ? {status:'failed', error:{message:secret}} : {status:'completed', stopReason:'end_turn'})
      };
    },
    async shutdown() {
      trace('shutdown');
      if (mode === 'shutdown-slow') await new Promise(resolve => setTimeout(resolve, 800));
      if (mode === 'shutdown-race') await new Promise(resolve => setTimeout(resolve, 1500));
      if (mode === 'shutdown') fail();
      if (mode === 'shutdown-pending') await new Promise(resolve => setTimeout(resolve, 30000));
    }
  };
};
