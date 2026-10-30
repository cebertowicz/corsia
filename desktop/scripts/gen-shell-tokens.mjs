// Emits desktop/dist-shell/tokens.css from the app's design tokens, so the launch shell page never carries
// hand-copied visual values. Run before packaging: node desktop/scripts/gen-shell-tokens.mjs
// With --check it writes nothing and exits non-zero if the committed copy is stale.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(here, '../../frontend/src/app-tokens.css');
const target = path.resolve(here, '../dist-shell/tokens.css');
const css = fs.readFileSync(source, 'utf8');
const blocks = [];
let depth = 0;
let start = -1;

for (let index = 0; index < css.length; index++) {
	if (css[index] === '{') {
		if (depth === 0) start = css.lastIndexOf('\n', index) + 1;
		depth++;
	} else if (css[index] === '}') {
		depth--;
		if (depth === 0) {
			const block = css.slice(start, index + 1);
			if (/^(:root|\[data-theme="light"\])\s*\{/.test(block)) blocks.push(block);
		}
	}
}

if (blocks.length === 0) throw new Error('no token blocks found in app-tokens.css');
const header = '/* Generated from frontend/src/app-tokens.css by desktop/scripts/gen-shell-tokens.mjs. Do not edit. */';
const next = `${header}\n\n${blocks.join('\n\n')}\n`;
if (process.argv.includes('--check')) {
	if (!fs.existsSync(target) || fs.readFileSync(target, 'utf8') !== next) {
		console.error('desktop/dist-shell/tokens.css is stale: run node desktop/scripts/gen-shell-tokens.mjs');
		process.exit(1);
	}
} else {
	fs.writeFileSync(target, next);
	console.log(`wrote ${path.relative(process.cwd(), target)} (${blocks.length} blocks)`);
}
