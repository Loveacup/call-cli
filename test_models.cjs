const test = require('node:test');
const assert = require('node:assert/strict');
const {discover} = require('./models.cjs');

test('model discovery uses ACP metadata, not CLI display IDs or a fixed catalog', async () => {
  const result = await discover('/work', {loadRuntime: async () => ({
    inspectAgentModels: async input => {
      assert.deepEqual(input.agentCommand, ['agy','acp']);
      assert.equal(input.cwd, '/work');
      return {currentModelId:'new-family',availableModelIds:['new-family','another-live-model']};
    }
  })});
  assert.deepEqual(result.availableModelIds, ['new-family','another-live-model']);
});

test('missing discovery capability fails without inventing candidates', async () => {
  await assert.rejects(discover('/work', {loadRuntime: async () => ({})}));
});
