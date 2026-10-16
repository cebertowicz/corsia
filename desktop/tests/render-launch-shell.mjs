import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const require = createRequire(path.join(repo, 'frontend/package.json'));
const { chromium } = require('playwright');
const args = new Map(process.argv.slice(2).reduce((pairs, value, index, all) => (
	value.startsWith('--') ? [...pairs, [value.slice(2), all[index + 1]]] : pairs
), []));
const output = args.get('output');
const approved = args.get('approved');

if (!output || !approved) {
	throw new Error('usage: node desktop/tests/render-launch-shell.mjs --output <dir> --approved <dir>');
}

function resolveWithin(root, requestPath) {
	const resolved = path.resolve(root, `.${requestPath}`);
	if (!resolved.startsWith(`${root}${path.sep}`)) throw new Error('invalid static path');
	return resolved;
}

const contentTypes = new Map([
	['.html', 'text/html; charset=utf-8'],
	['.css', 'text/css'],
	['.svg', 'image/svg+xml'],
	['.png', 'image/png'],
	['.js', 'text/javascript'],
]);

function startStaticServer() {
	const server = createServer(async (request, response) => {
		try {
			const requestUrl = new URL(request.url, 'http://127.0.0.1');
			const root = requestUrl.pathname.startsWith('/approved/')
				? approved
				: requestUrl.pathname.startsWith('/evidence/') ? output : repo;
			const suffix = requestUrl.pathname.startsWith('/approved/')
				? requestUrl.pathname.slice('/approved'.length)
				: requestUrl.pathname.startsWith('/evidence/')
					? requestUrl.pathname.slice('/evidence'.length)
					: requestUrl.pathname;
			const file = resolveWithin(root, suffix);
			if (!(await stat(file)).isFile()) throw new Error('not a file');
			response.writeHead(200, { 'Content-Type': contentTypes.get(path.extname(file)) ?? 'application/octet-stream' });
			response.end(await readFile(file));
		} catch {
			response.writeHead(404);
			response.end('not found');
		}
	});
	return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

function nativeShellStub() {
	window.__TAURI__ = {
		core: {
			invoke: async (command) => {
				if (command === 'get_service_log_path') return '/Users/you/Library/Logs/CORSIA/service.log';
				if (command === 'start_local_service') return { state: 'ready', url: '#test-navigation-held' };
				if (command === 'retry_quit') throw new Error('quit still refused');
				return undefined;
			},
		},
	};
	window.setTimeout = () => 0;
}

async function render(page, state) {
	if (state === 'startup-failure') await page.evaluate(() => window.__corsiaShowServiceFailure());
	if (state === 'background-off' || state === 'background-on') {
		await page.evaluate(() => window.__corsiaShowBackgroundApproval());
	}
	if (state === 'background-on') {
		await page.waitForFunction(() => document.getElementById('approval-check').dataset.state === 'on');
	}
	if (state === 'quit-failure') await page.evaluate(() => window.__corsiaShowQuitFailure());
}

async function assertImagesLoaded(page, state, theme) {
	const images = await page.locator('img').evaluateAll((nodes) => nodes.map((image) => ({
		src: image.currentSrc || image.src,
		naturalWidth: image.naturalWidth,
	})));
	const unloaded = images.filter((image) => image.naturalWidth === 0);
	if (unloaded.length) throw new Error(`${state} ${theme} has unloaded image(s): ${unloaded.map((image) => image.src).join(', ')}`);
	console.log(`img-check: ${state}/${theme}: ${images.length} image(s) loaded`);
	return images;
}

async function assertState(page, state) {
	const observed = await page.evaluate(() => ({
		status: document.getElementById('status').textContent,
		spinner: !document.getElementById('spinner').hidden,
		log: !document.getElementById('log').hidden,
		action: !document.getElementById('actions').hidden,
		secondary: !document.getElementById('failure-login-items').hidden,
		loginItemsClass: document.getElementById('failure-login-items').className,
		retryClass: document.getElementById('retry').className,
		approval: document.getElementById('approval-check').dataset.state,
		approvalText: document.getElementById('approval-state').textContent,
		noScroll: document.documentElement.scrollHeight <= window.innerHeight,
	}));
	const expected = {
		starting: ['Starting CORSIA…', true, false, false, false, 'secondary', 'primary', 'off', 'Off'],
		'startup-failure': ['CORSIA couldn’t start', false, true, true, false, 'secondary', 'primary', 'off', 'Off'],
		'background-off': ['Keep CORSIA running', false, false, false, false, 'secondary', 'primary', 'off', 'Off'],
		'background-on': ['Starting CORSIA…', false, false, false, false, 'secondary', 'primary', 'on', 'On'],
		'quit-failure': ['CORSIA couldn’t quit', false, true, true, true, 'primary', 'secondary', 'off', 'Off'],
	}[state];
	if (JSON.stringify([observed.status, observed.spinner, observed.log, observed.action, observed.secondary, observed.loginItemsClass, observed.retryClass, observed.approval, observed.approvalText]) !== JSON.stringify(expected)) {
		throw new Error(`${state} did not render through its native shell path: ${JSON.stringify(observed)}`);
	}
	return observed;
}

async function pixelDifference(page, left, right, ignoredRects = []) {
	return page.evaluate(async ({ left, right, ignoredRects }) => {
		const load = (src) => new Promise((resolve, reject) => {
			const image = new Image();
			image.onload = () => resolve(image);
			image.onerror = reject;
			image.src = src;
		});
		const [first, second] = await Promise.all([load(left), load(right)]);
		if (first.width !== second.width || first.height !== second.height) throw new Error('frame dimensions differ');
		const canvas = document.createElement('canvas');
		canvas.width = first.width;
		canvas.height = first.height;
		const context = canvas.getContext('2d', { willReadFrequently: true });
		context.drawImage(first, 0, 0);
		const firstPixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
		context.clearRect(0, 0, canvas.width, canvas.height);
		context.drawImage(second, 0, 0);
		const secondPixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
		let changed = 0;
		let minX = canvas.width;
		let minY = canvas.height;
		let maxX = -1;
		let maxY = -1;
		for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
			if (ignoredRects.some((rect) => x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height)) continue;
			const index = (y * canvas.width + x) * 4;
			if (Math.abs(firstPixels[index] - secondPixels[index]) + Math.abs(firstPixels[index + 1] - secondPixels[index + 1]) + Math.abs(firstPixels[index + 2] - secondPixels[index + 2]) > 12) {
				changed++;
				minX = Math.min(minX, x);
				minY = Math.min(minY, y);
				maxX = Math.max(maxX, x);
				maxY = Math.max(maxY, y);
			}
		}
		return { changed, bounds: changed ? { minX, minY, maxX, maxY } : null };
	}, { left, right, ignoredRects });
}

async function nonBackgroundPixels(page, src, rect) {
	return page.evaluate(async ({ src, rect }) => {
		const image = await new Promise((resolve, reject) => {
			const candidate = new Image();
			candidate.onload = () => resolve(candidate);
			candidate.onerror = reject;
			candidate.src = src;
		});
		const canvas = document.createElement('canvas');
		canvas.width = image.width;
		canvas.height = image.height;
		const context = canvas.getContext('2d', { willReadFrequently: true });
		context.drawImage(image, 0, 0);
		const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
		const sampleX = Math.max(0, rect.x - 1);
		const sampleY = Math.max(0, rect.y - 1);
		const sampleOffset = (sampleY * canvas.width + sampleX) * 4;
		const background = [pixels[sampleOffset], pixels[sampleOffset + 1], pixels[sampleOffset + 2]];
		let nonBackground = 0;
		for (let y = rect.y; y < rect.y + rect.height; y++) for (let x = rect.x; x < rect.x + rect.width; x++) {
			const offset = (y * canvas.width + x) * 4;
			if (Math.abs(pixels[offset] - background[0]) + Math.abs(pixels[offset + 1] - background[1]) + Math.abs(pixels[offset + 2] - background[2]) > 12) nonBackground++;
		}
		return nonBackground;
	}, { src, rect });
}

async function renderFrames(browser, baseUrl) {
	const frames = [];
	for (const theme of ['light', 'dark']) for (const state of ['starting', 'startup-failure', 'background-off', 'background-on', 'quit-failure']) {
		const context = await browser.newContext({ colorScheme: theme, viewport: { width: 1280, height: 900 } });
		await context.addInitScript(nativeShellStub);
		const page = await context.newPage();
		await page.goto(`${baseUrl}/desktop/dist-shell/index.html`, { waitUntil: 'networkidle' });
		await render(page, state);
		const observed = await assertState(page, state);
		const images = await assertImagesLoaded(page, state, theme);
		const file = `${state}-${theme}-1280x900.png`;
		await page.screenshot({ path: path.join(output, file) });
		const mark = await page.locator('.mark').boundingBox();
		const primary = state === 'startup-failure' ? await page.locator('.primary:visible').boundingBox() : null;
		frames.push({ file, state, theme, size: '1280x900', observed, images, mark, primary });
		await context.close();
	}
	return frames;
}

async function renderSmallFrame(browser, baseUrl) {
	const frames = [];
	for (const theme of ['light', 'dark']) {
		const context = await browser.newContext({ colorScheme: theme, viewport: { width: 800, height: 600 } });
		await context.addInitScript(nativeShellStub);
		const page = await context.newPage();
		await page.goto(`${baseUrl}/desktop/dist-shell/index.html`, { waitUntil: 'networkidle' });
		await render(page, 'background-off');
		const observed = await assertState(page, 'background-off');
		const images = await assertImagesLoaded(page, 'background-off', theme);
		if (!observed.noScroll) throw new Error(`background-off ${theme} scrolls at 800x600`);
		const file = `background-off-${theme}-800x600.png`;
		await page.screenshot({ path: path.join(output, file) });
		frames.push({ file, state: 'background-off', theme, size: '800x600', observed, images });
		await context.close();
	}
	return frames;
}

async function writeEvidence(baseUrl, frames, diffPage) {
	const pairDifferences = [];
	for (let index = 0; index < frames.length; index++) for (let other = index + 1; other < frames.length; other++) {
		if (frames[index].size !== frames[other].size) continue;
		const difference = await pixelDifference(diffPage, `${baseUrl}/evidence/${frames[index].file}`, `${baseUrl}/evidence/${frames[other].file}`);
		if (difference.changed <= 50) throw new Error(`frames ${frames[index].file} and ${frames[other].file} differ by only ${difference.changed} pixels`);
		pairDifferences.push({ left: frames[index].file, right: frames[other].file, changedPixels: difference.changed });
	}
	const parity = [];
	for (const theme of ['light', 'dark']) {
		const frame = frames.find((candidate) => candidate.state === 'startup-failure' && candidate.theme === theme);
		const approvedFile = `splash-after-didnt-start-1280${theme === 'dark' ? '-dark' : ''}-r3.png`;
		const raw = await pixelDifference(diffPage, `${baseUrl}/evidence/${frame.file}`, `${baseUrl}/approved/${approvedFile}`);
		const mark = { x: Math.floor(frame.mark.x) - 2, y: Math.floor(frame.mark.y) - 2, width: Math.ceil(frame.mark.width) + 4, height: Math.ceil(frame.mark.height) + 4 };
		const primary = { x: Math.floor(frame.primary.x) - 12, y: Math.floor(frame.primary.y) - 12, width: Math.ceil(frame.primary.width) + 24, height: Math.ceil(frame.primary.height) + 24 };
		const outsideLogoAndPrimary = await pixelDifference(diffPage, `${baseUrl}/evidence/${frame.file}`, `${baseUrl}/approved/${approvedFile}`, [mark, primary]);
		if (outsideLogoAndPrimary.changed !== 0) throw new Error(`startup-failure ${theme} differs from the approved r3 frame outside Logo B and the tokenized primary action by ${outsideLogoAndPrimary.changed} pixels at ${JSON.stringify(outsideLogoAndPrimary.bounds)}`);
		const logoDelta = await pixelDifference(diffPage, `${baseUrl}/evidence/${frame.file}`, `${baseUrl}/approved/${approvedFile}`, [primary]);
		if (logoDelta.changed === 0) throw new Error(`startup-failure ${theme} Logo B does not differ from the approved Logo A frame`);
		const nonBackgroundLogoPixels = await nonBackgroundPixels(diffPage, `${baseUrl}/evidence/${frame.file}`, mark);
		if (nonBackgroundLogoPixels === 0) throw new Error(`startup-failure ${theme} Logo B is blank`);
		parity.push({ theme, approvedFile, rawChangedPixels: raw.changed, outsideLogoAndPrimaryChangedPixels: outsideLogoAndPrimary.changed, logoDeltaChangedPixels: logoDelta.changed, nonBackgroundLogoPixels });
	}
	const elements = {
		starting: 'spinner',
		'startup-failure': 'service log and Try again',
		'background-off': 'Off ring and Open Login Items',
		'background-on': 'On tick',
		'quit-failure': 'Open Login Items pill and Try again text action',
	};
	const lines = frames.map((frame) => `- ${frame.file}: ${elements[frame.state]}`).join('\n');
	const report = `# Launch restyle frame evidence\n\n${lines}\n\nImage check: every page image loaded in all ${frames.length} frames.\n\nMinimum pair difference: ${Math.min(...pairDifferences.map((entry) => entry.changedPixels))} changed pixels (pass mark: more than 50).\n\nStartup failure parity (Logo A swapped for B):\nPrimary exclusion: parity excludes the Logo B and primary-action rectangles because the primary uses the design-system token RGB 91,63,208 while the approved r3 mockup used older blue RGB 0,118,158.\n${parity.map((entry) => `- ${entry.theme}: ${entry.rawChangedPixels} raw changed pixels; ${entry.outsideLogoAndPrimaryChangedPixels} outside the Logo B and primary rectangles; Logo B delta ${entry.logoDeltaChangedPixels}; ${entry.nonBackgroundLogoPixels} non-background Logo B pixels.`).join('\n')}\n`;
	await writeFile(path.join(output, 'FRAME-EVIDENCE.md'), report);
	await writeFile(path.join(output, 'frame-metrics.json'), `${JSON.stringify({ frames, minimumPairDifference: Math.min(...pairDifferences.map((entry) => entry.changedPixels)), pairDifferences, parity }, null, 2)}\n`);
	return { minimumPairDifference: Math.min(...pairDifferences.map((entry) => entry.changedPixels)), parity };
}

await mkdir(output, { recursive: true });
const server = await startStaticServer();
const address = server.address();
const baseUrl = `http://127.0.0.1:${address.port}`;
const browser = await chromium.launch();
try {
	const frames = [...await renderFrames(browser, baseUrl), ...await renderSmallFrame(browser, baseUrl)];
	const diffPage = await browser.newPage();
	await diffPage.goto(`${baseUrl}/desktop/dist-shell/index.html`, { waitUntil: 'networkidle' });
	const evidence = await writeEvidence(baseUrl, frames, diffPage);
	console.log(JSON.stringify(evidence));
} finally {
	await browser.close();
	await new Promise((resolve) => server.close(resolve));
}
