// Emits desktop/dist-shell/living-logo.js from the app's living-mark module, so the launch shell page plays the same mark
// as the app. The shell page is a classic script (it opens from disk and loads no ES modules), so each `export` is removed
// and the names are put on window.livingLogo. Run before packaging: node desktop/scripts/gen-shell-logo.mjs
// With --check it writes nothing and exits non-zero if the committed copy is stale.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(here, '../../frontend/src/lib/livingLogo.js');
const target = path.resolve(here, '../dist-shell/living-logo.js');

export function classicCopy(text) {
	const names = [...text.matchAll(/^export (?:async )?(?:const|function|class) (\w+)/gm)].map((match) => match[1]);
	if (names.length === 0) throw new Error('no exports found in livingLogo.js');
	const body = text.replace(/^export /gm, '');
	return `// Generated from frontend/src/lib/livingLogo.js by desktop/scripts/gen-shell-logo.mjs. Do not edit.\n(function () {\n${body}\nwindow.livingLogo = { ${names.join(', ')} };\n})();\n`;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	const next = classicCopy(fs.readFileSync(source, 'utf8'));
	if (process.argv.includes('--check')) {
		if (!fs.existsSync(target) || fs.readFileSync(target, 'utf8') !== next) {
			console.error('desktop/dist-shell/living-logo.js is stale: run node desktop/scripts/gen-shell-logo.mjs');
			process.exit(1);
		}
	} else {
		fs.writeFileSync(target, next);
		console.log(`wrote ${path.relative(process.cwd(), target)}`);
	}
}
