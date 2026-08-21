#!/usr/bin/env node
// node-pty ships `spawn-helper` inside its prebuilds, and npm drops the
// executable bit when it extracts them.  Without it every pty.spawn() fails
// with a bare "posix_spawnp failed." and the terminal panel looks broken for
// reasons nothing in the app can explain.  Restore the bit after every install.
//
// Safe to run anywhere: on Windows there is no spawn-helper and this is a no-op.

const fs = require('node:fs');
const path = require('node:path');

const prebuilds = path.join(__dirname, '..', 'node_modules', 'node-pty', 'prebuilds');

function helpers(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .map((platform) => path.join(dir, platform, 'spawn-helper'))
    .filter((p) => fs.existsSync(p));
}

let fixed = 0;
for (const helper of helpers(prebuilds)) {
  const mode = fs.statSync(helper).mode;
  if (mode & 0o111) continue;
  fs.chmodSync(helper, mode | 0o755);
  fixed += 1;
  console.log(`[descant] made ${path.relative(process.cwd(), helper)} executable`);
}
if (fixed === 0) process.stdout.write('');
