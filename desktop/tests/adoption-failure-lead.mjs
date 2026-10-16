// Checks the first line of the adoption failure screen in the real launch shell:
// a failure raised before the old service was stopped says nothing changed; one
// raised after it says the old service is running again. The window reads the
// backend's `nothingChanged` flag, never the reason text.
//
// usage: node desktop/tests/adoption-failure-lead.mjs [--screenshots <dir>]
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const shell = path.join(repo, 'desktop/dist-shell');
const { chromium } = createRequire(path.join(process.env.PLAYWRIGHT_FROM ?? repo, 'frontend/package.json'))('playwright');
const flag = process.argv.indexOf('--screenshots');
const screenshots = flag > -1 ? process.argv[flag + 1] : null;
if (screenshots) await mkdir(screenshots, { recursive: true });

const types = { '.html': 'text/html', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = createServer(async (request, response) => {
	try {
		const name = new URL(request.url, 'http://127.0.0.1').pathname.replace(/^\/(desktop\/dist-shell\/)?/, '');
		const file = path.resolve(shell, name || 'index.html');
		if (!file.startsWith(`${shell}${path.sep}`)) throw new Error('outside the shell');
		response.writeHead(200, { 'content-type': types[path.extname(file)] ?? 'text/plain' });
		response.end(await readFile(file));
	} catch {
		response.writeHead(404);
		response.end();
	}
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch();

const keys = 'These provider keys aren’t stored in CORSIA yet: OPENAI_API_KEY. Add them in CORSIA › Settings › Keys, then choose Try again.';
const space = 'The backup needs 4.2 GB free and this disk has 1.1 GB. Free up space, then choose Try again.';
const cases = [
	// The same reason text, once with each flag: only the flag decides the lead.
	{ name: '3b-refused-keys', failure: { message: keys, nothingChanged: true }, lead: 'Nothing was changed. Your existing CORSIA is still running.' },
	{ name: '3c-refused-space', failure: { message: space, nothingChanged: true }, lead: 'Nothing was changed. Your existing CORSIA is still running.' },
	{ name: 'after-stop', failure: { message: 'the new service did not become ready; the old install was restored', nothingChanged: false }, lead: 'Your existing CORSIA is running again, as it was.' },
	{ name: 'same-text-after-stop', failure: { message: space, nothingChanged: false }, lead: 'Your existing CORSIA is running again, as it was.' },
	{ name: 'plain-string', failure: 'could not restart', lead: 'Your existing CORSIA is running again, as it was.' },
];

let failed = 0;
for (const theme of ['light', 'dark']) {
	for (const { name, failure, lead } of cases) {
		const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, colorScheme: theme });
		await page.addInitScript((thrown) => {
			window.__TAURI__ = { core: { invoke: async (command) => {
				if (command === 'get_service_log_path') return '/x/service.log';
				if (command === 'adoption_waiting_count') return 0;
				if (command === 'adopt_existing_install') throw thrown;
				return undefined;
			} } };
		}, failure);
		await page.goto(`http://127.0.0.1:${server.address().port}/index.html`, { waitUntil: 'networkidle' });
		await page.evaluate(() => window.__corsiaShowExistingInstall('/i', '~/work'));
		await page.click('#retry');
		await page.waitForFunction(() => document.getElementById('status').textContent.includes('couldn’t switch over'));
		const shown = await page.textContent('#line');
		const reason = typeof failure === 'string' ? failure : failure.message;
		const expected = `${lead} ${reason}`;
		if (shown !== expected) {
			failed += 1;
			console.error(`FAIL ${theme} ${name}\n  expected: ${expected}\n  shown:    ${shown}`);
		} else {
			console.log(`ok   ${theme} ${name}`);
		}
		if (screenshots && ['3b-refused-keys', '3c-refused-space'].includes(name)) await page.screenshot({ path: path.join(screenshots, `${name}-${theme}.png`) });
		await page.close();
	}
}
await browser.close();
server.close();
if (failed) process.exit(1);
