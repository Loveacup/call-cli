"use strict";
// Sent over the existing execution transport; nothing is installed on the target.
const fs = require('fs'), path = require('path'), os = require('os'), crypto = require('crypto');
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const excluded = name => name.startsWith('.') || ['node_modules', '__pycache__', 'auth.json'].includes(name) || /(?:\.pem|\.key)$/i.test(name) || /^(?:id_|credentials)/i.test(name);
function directory(dir) {
  try { fs.mkdirSync(dir, {mode: 0o700}); }
  catch (error) { if (error.code !== 'EEXIST') throw error; }
  const stat = fs.lstatSync(dir);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw Error('Unsafe skill directory');
}
function rootFor(task) {
  if (!/^[a-f0-9]{16}$/.test(task)) throw Error('Invalid task identity');
  const root = path.join(os.tmpdir(), 'call-cli-skills');
  directory(root);
  const dest = path.join(root, task); directory(dest); return dest;
}
function collect(source) {
  if (path.basename(source) === 'SKILL.md') source = path.dirname(source);
  if (!path.isAbsolute(source) || fs.lstatSync(source).isSymbolicLink()) throw Error('Invalid target skill source');
  const files = [], skipped = []; let total = 0;
  function walk(dir, prefix) {
    for (const name of fs.readdirSync(dir).sort()) {
      const rel = prefix + name, full = path.join(dir, name), stat = fs.lstatSync(full);
      if (excluded(name) || stat.isSymbolicLink()) { skipped.push(rel); continue; }
      if (stat.isDirectory()) walk(full, rel + '/');
      else if (stat.isFile()) {
        const bytes = fs.readFileSync(full); total += bytes.length;
        if (bytes.length > 4*1024*1024 || total > 16*1024*1024 || files.length >= 1000) throw Error('Skill bundle too large');
        files.push({path: rel, sha256: hash(bytes), size: bytes.length, data: bytes.toString('base64')});
      }
    }
  }
  walk(source, ''); files.sort((a,b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  if (!files.some(f => f.path === 'SKILL.md')) throw Error('SKILL.md missing');
  const manifest = files.map(({path,sha256,size}) => ({path,sha256,size}));
  return {files, skipped, sha256: hash(JSON.stringify(manifest))};
}
function run(request) {
  const root = rootFor(request.task);
  if (request.op === 'cleanup') { fs.rmSync(root, {recursive: true, force: true}); return {}; }
  let bundle = request.bundle;
  if (!/^skill-[1-9][0-9]*$/.test(bundle.id)) throw Error('Invalid skill identity');
  const targetSource = bundle.location === 'target';
  if (targetSource && !bundle.sha256) bundle = {...bundle, ...collect(bundle.path)};
  if (!/^[a-f0-9]{64}$/.test(bundle.sha256)) throw Error('Invalid skill digest');
  const parent = path.join(root, bundle.id); directory(parent);
  const dest = path.join(parent, bundle.sha256); directory(dest);
  if (targetSource && !bundle.files) throw Error('Target snapshot manifest missing');
  for (const file of bundle.files) {
    if (!file.path || file.path.includes('\\') || file.path.includes(':') || file.path.split('/').some(p => !p || p === '.' || p === '..')) throw Error('Unsafe resource path');
    const parts = file.path.split('/'); let dir = dest;
    for (const part of parts.slice(0,-1)) { dir = path.join(dir, part); directory(dir); }
    const full = path.join(dest, ...parts);
    try {
      if (fs.lstatSync(full).isSymbolicLink()) throw Error('Unsafe resource symlink');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (file.data !== undefined) {
      const bytes = Buffer.from(file.data, 'base64');
      if (hash(bytes) !== file.sha256 || bytes.length !== file.size) throw Error('Resource checksum mismatch');
      fs.writeFileSync(full, bytes, {mode: 0o600}); fs.chmodSync(full, 0o600);
    } else if (hash(fs.readFileSync(full)) !== file.sha256) throw Error('Pinned target snapshot changed');
  }
  const text = fs.readFileSync(path.join(dest, 'SKILL.md'), 'utf8');
  const name = /^name:\s*["']?([^\n"']+)/m.exec(text);
  return {target_path: dest, sha256: bundle.sha256, name: name ? name[1].trim() : bundle.id,
    files: bundle.files.map(({path,sha256,size}) => ({path,sha256,size})), skipped: bundle.skipped || []};
}
module.exports = {run};
