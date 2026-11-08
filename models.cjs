'use strict';
// Run on the execution device. Creates an owned metadata-only ACP session;
// no prompt, filesystem or terminal capability, and transport cleanup is awaited.
const {loadRuntime} = require('./shared-prompt.cjs');
const path = require('node:path');
async function discover(cwd, deps = {}) {
  if (!path.isAbsolute(cwd)) throw Error('ABSOLUTE_CWD_REQUIRED');
  const sdk = await (deps.loadRuntime ?? loadRuntime)();
  if (typeof sdk.inspectAgentModels !== 'function') throw Error('MODEL_DISCOVERY_UNAVAILABLE');
  const result = await sdk.inspectAgentModels({agentCommand: ['agy', 'acp'], cwd, timeoutMs: 30000});
  if (!result?.availableModelIds?.length) throw Error('NO_ADVERTISED_MODELS');
  return result;
}
module.exports = {discover};
if (require.main === module) discover(process.argv[2] ?? process.cwd())
  .then(result => console.log(JSON.stringify(result)))
  .catch(() => {console.error('AGY model discovery failed; do not guess or switch provider'); process.exitCode = 1;});
