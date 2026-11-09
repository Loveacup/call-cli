"use strict";
const {test} = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path'), crypto = require('node:crypto');
const {run} = require('./skill-stage.cjs');
const hash = data => crypto.createHash('sha256').update(data).digest('hex');
function taskId() { return crypto.randomBytes(8).toString('hex'); }
function bundle(files) {
  return {id:'skill-1', location:'origin', sha256:'a'.repeat(64), files:Object.entries(files).map(([name, value]) => {
    const bytes = Buffer.from(value);return {path:name,sha256:hash(bytes),size:bytes.length,data:bytes.toString('base64')};
  })};
}
test('origin materialization keeps binary resources and rejects traversal without outside writes', () => {
  const task = taskId();
  try {
    const result = run({task,bundle:bundle({'SKILL.md':'# Guide','assets/data.bin':'\u0000\u00ff'})});
    assert.deepEqual(fs.readFileSync(path.join(result.target_path,'assets/data.bin')), Buffer.from('\u0000\u00ff'));
    assert.throws(() => run({task,bundle:bundle({'../escape':'forbidden'})}), /Unsafe resource path/);
    assert.equal(fs.existsSync(path.join(path.dirname(result.target_path),'escape')),false);
    assert.throws(() => run({task,bundle:bundle({'C:/escape':'forbidden'})}), /Unsafe resource path/);
    assert.throws(() => run({task,bundle:bundle({'..\\escape':'forbidden'})}), /Unsafe resource path/);
  } finally { run({task,op:'cleanup'}); }
});
test('target snapshot survives source changes but refuses modified pinned bytes', () => {
  const source = fs.mkdtempSync(path.join(os.tmpdir(),'callcli-target-source-')), task = taskId();
  try {
    fs.writeFileSync(path.join(source,'SKILL.md'),'# Original');
    fs.writeFileSync(path.join(source,'private.key'),'NEVER TRANSFER');
    const first = run({task,bundle:{id:'skill-1',location:'target',path:source}});
    fs.writeFileSync(path.join(source,'SKILL.md'),'# Changed');
    const second = run({task,bundle:{id:'skill-1',location:'target',path:source,...first}});
    assert.equal(fs.readFileSync(path.join(second.target_path,'SKILL.md'),'utf8'),'# Original');
    assert.equal(fs.existsSync(path.join(second.target_path,'private.key')),false);
    fs.writeFileSync(path.join(second.target_path,'SKILL.md'),'CORRUPTED');
    assert.throws(() => run({task,bundle:{id:'skill-1',location:'target',path:source,...first}}), /Pinned target snapshot changed/);
    run({task,op:'cleanup'});assert.equal(fs.existsSync(first.target_path),false);
  } finally {run({task,op:'cleanup'});fs.rmSync(source,{recursive:true,force:true});}
});
test('resource symlink cannot redirect bundle writes', () => {
  const task = taskId(), outside = fs.mkdtempSync(path.join(os.tmpdir(),'callcli-symlink-'));
  try {
    const spec = bundle({'SKILL.md':'# Guide'}), result = run({task,bundle:spec});
    fs.unlinkSync(path.join(result.target_path,'SKILL.md'));
    fs.symlinkSync(path.join(outside,'untouched'),path.join(result.target_path,'SKILL.md'));
    assert.throws(() => run({task,bundle:spec}), /Unsafe resource symlink/);
    assert.equal(fs.existsSync(path.join(outside,'untouched')),false);
  } finally {run({task,op:'cleanup'});fs.rmSync(outside,{recursive:true,force:true});}
});
