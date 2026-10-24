// Emits desktop/dist-shell/update-screen.js and update-screen.css from the app's own update screen
// (frontend/src/lib/update/shellEntry.ts), so the shell's update page draws the same component with the same tokens.
// The shell page is a classic script that opens from the app bundle, so the entry is built as one IIFE with the app's
// installed Vite and Svelte and the app's svelte.config.js. Run before packaging: node desktop/scripts/gen-shell-update.mjs
// With --check it writes nothing and exits non-zero if the committed copy is stale.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, '../../frontend');
const shell = path.resolve(here, '../dist-shell');
const require = createRequire(path.join(frontend, 'package.json'));
const load = async (name) => import(pathToFileURL(require.resolve(name)).href);
const header = (source) => `Generated from frontend/src/lib/update/shellEntry.ts by desktop/scripts/gen-shell-update.mjs. Do not edit.${source}`;

export async function buildShellUpdate() {
	const { build } = await load('vite');
	const { svelte } = await load('@sveltejs/vite-plugin-svelte');
	const result = await build({
		root: frontend,
		configFile: false,
		logLevel: 'silent',
		mode: 'production',
		plugins: [svelte({ configFile: path.join(frontend, 'svelte.config.js') })],
		resolve: { alias: { $lib: path.join(frontend, 'src/lib') }, conditions: ['browser'] },
		define: { 'process.env.NODE_ENV': '"production"' },
		build: {
			write: false,
			cssCodeSplit: false,
			lib: { entry: path.join(frontend, 'src/lib/update/shellEntry.ts'), formats: ['iife'], name: 'corsiaUpdateScreen', fileName: () => 'update-screen.js' },
		},
	});
	const output = (Array.isArray(result) ? result : [result]).flatMap((entry) => entry.output);
	const script = output.find((file) => file.type === 'chunk');
	const style = output.find((file) => file.type === 'asset' && file.fileName.endsWith('.css'));
	if (!script || !style) throw new Error('the shell update screen build produced no script or no stylesheet');
	return {
		'update-screen.js': `// ${header('')}\n${script.code}`,
		'update-screen.css': `/* ${header('')} */\n${typeof style.source === 'string' ? style.source : Buffer.from(style.source).toString('utf8')}`,
	};
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
	const files = await buildShellUpdate();
	if (process.argv.includes('--check')) {
		const stale = Object.entries(files).filter(([name, text]) => !fs.existsSync(path.join(shell, name)) || fs.readFileSync(path.join(shell, name), 'utf8') !== text);
		if (stale.length) {
			console.error(`desktop/dist-shell/${stale.map(([name]) => name).join(', ')} stale: run node desktop/scripts/gen-shell-update.mjs`);
			process.exit(1);
		}
	} else {
		for (const [name, text] of Object.entries(files)) {
			fs.writeFileSync(path.join(shell, name), text);
			console.log(`wrote ${path.relative(process.cwd(), path.join(shell, name))}`);
		}
	}
}
