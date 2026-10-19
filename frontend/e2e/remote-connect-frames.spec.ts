import { expect, test, type Page } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Evidence frames for corsia#638 PR 2 (Connect to CORSIA on another computer, Settings › System › Devices).
 * Every frame is the real screen on `/_design/remote-connect`, reached by using it (click, type), with only the desktop
 * shell and the server answers faked. A frame passes only if its state-specific element is on screen; the run fails if
 * any two frames are pixel-identical. Output: REMOTE_FRAMES_OUT (default e2e-results/remote-connect-frames).
 */
const OUT = process.env.REMOTE_FRAMES_OUT ?? join(process.cwd(), 'e2e-results', 'remote-connect-frames');

type Size = 'w1440' | 'w393';
type Frame = {
	id: string; url: string; size: Size; element: string; line: string;
	act?: (page: Page) => Promise<void>;
};
const SIZES = { w1440: { width: 1440, height: 1000 }, w393: { width: 393, height: 852 } };

const pickStudio = async (page: Page) => { await page.getByRole('button', { name: /Alex’s Mac mini/ }).click(); };
const toCode = async (page: Page) => { await pickStudio(page); await page.getByRole('button', { name: 'Continue' }).click(); await expect(page.getByRole('heading', { name: 'Enter the code' })).toBeVisible(); };
const typeCode = async (page: Page) => { await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label')?.startsWith('Code, character 1')); await page.keyboard.type('K7QX', { delay: 40 }); };
const setup = (query: string) => `/_design/remote-connect?kind=setup&${query}`;
const devices = (state: string, extra = '') => `/_design/remote-connect?kind=devices&state=${state}${extra}`;

const frames: Frame[] = [
	{ id: 'r1-looking', url: setup('discover=empty'), size: 'w1440', element: 'text=Looking…', line: 'The list says Looking… and nothing is selectable yet.' },
	{ id: 'r1-found', url: setup('discover=found'), size: 'w1440', element: 'ul[aria-label="CORSIA on this network"] >> text=Living room mini', line: 'Two computers listed as rows by name; Continue is disabled.' },
	{ id: 'r1-picked', url: setup('discover=found'), size: 'w1440', element: 'ul[aria-label="CORSIA on this network"] .row.current', line: 'The picked row is marked current and Continue is enabled.', act: async (page) => { await pickStudio(page); } },
	{ id: 'r1-typed', url: setup('discover=found'), size: 'w1440', element: '#remote-address', line: 'A typed address replaces the pick; the list has no current row.', act: async (page) => { await page.getByRole('button', { name: /Living room mini/ }).click(); await page.locator('#remote-address').fill('192.168.1.20:47902'); } },
	{ id: 'r1-none-found', url: setup('discover=empty'), size: 'w1440', element: 'text=No other computer running CORSIA found yet.', line: 'After five seconds with nothing found the screen says what to check.', act: async (page) => { await expect(page.getByText('No other computer running CORSIA found yet.')).toBeVisible({ timeout: 8000 }); } },
	{ id: 'r6-denied', url: setup('discover=denied'), size: 'w1440', element: 'button:has-text("Open System Settings")', line: 'macOS blocked the local network; one action opens the setting.' },
	{ id: 'r4-network', url: setup('discover=found&pair=network'), size: 'w1440', element: '.field .err', line: 'The computer did not answer: the network line sits under the address field.', act: async (page) => { await toCode(page); await typeCode(page); await expect(page.getByRole('heading', { name: 'Connect to CORSIA on another computer' })).toBeVisible(); } },
	{ id: 'r4-incompatible', url: setup('discover=found&pair=incompatible'), size: 'w1440', element: 'text=This computer has CORSIA 1.4 and Alex’s Mac mini has 1.2', line: 'Both versions are named and the older one is told to update.', act: async (page) => { await toCode(page); await typeCode(page); await expect(page.getByRole('heading', { name: 'Connect to CORSIA on another computer' })).toBeVisible(); } },
	{ id: 'r7-code', url: setup('discover=found'), size: 'w1440', element: '[data-testid="host-steps"]', line: 'Three numbered crops of the other computer and four empty boxes with the cursor in the first.', act: toCode },
	{ id: 'r7-wrong-code', url: setup('discover=found&pair=wrong_code'), size: 'w1440', element: 'p.err >> text=That code doesn’t match.', line: 'A wrong code: boxes cleared, cursor back in the first, the refusal line below.', act: async (page) => { await toCode(page); await typeCode(page); await expect(page.locator('p.err')).toBeVisible(); } },
	{ id: 'r7-expired', url: setup('discover=found&pair=expired'), size: 'w1440', element: 'p.err >> text=That code expired.', line: 'An expired code names Open CORSIA › Settings › System and asks for a new one.', act: async (page) => { await toCode(page); await typeCode(page); await expect(page.locator('p.err')).toBeVisible(); } },
	{ id: 'r7-paused', url: setup('discover=found&pair=paused'), size: 'w1440', element: 'p.err >> text=Pairing is paused', line: 'Pairing paused on the other computer: press New code there.', act: async (page) => { await toCode(page); await typeCode(page); await expect(page.locator('p.err')).toBeVisible(); } },
	{ id: 'r1-found-393', url: setup('discover=found'), size: 'w393', element: 'ul[aria-label="CORSIA on this network"] >> text=Living room mini', line: 'The choose screen at phone width.' },
	{ id: 'r7-code-393', url: setup('discover=found'), size: 'w393', element: '[data-testid="host-steps"]', line: 'The code screen at phone width: the pictures stack above the boxes.', act: toCode },
	{ id: 'r1-found-dark', url: setup('discover=found&theme=dark'), size: 'w1440', element: 'ul[aria-label="CORSIA on this network"] >> text=Living room mini', line: 'The choose screen, dark.' },
	{ id: 'r7-code-dark', url: setup('discover=found&theme=dark'), size: 'w1440', element: '[data-testid="host-steps"]', line: 'The code screen, dark.', act: toCode },

	{ id: 'dev-loading', url: devices('loading'), size: 'w1440', element: 'text=Loading…', line: 'The only state with a dimmed switch; the description reads Loading….' },
	{ id: 'dev-off', url: devices('off'), size: 'w1440', element: 'button[aria-label="Let other devices connect"][aria-pressed="false"]', line: 'Off: the switch row and nothing beneath it.' },
	{ id: 'dev-on', url: devices('on'), size: 'w1440', element: 'text=Visible as Studio', line: 'On and visible with no code: the status dot and New code.' },
	{ id: 'dev-code', url: devices('code'), size: 'w1440', element: 'text=Ends in 9 min', line: 'A code is showing in four read-only boxes with a countdown in minutes.' },
	{ id: 'dev-ending', url: devices('ending'), size: 'w1440', element: 'text=Ends in under a minute', line: 'Under sixty seconds the countdown reads Ends in under a minute.' },
	{ id: 'dev-code-consumed', url: devices('consumed'), size: 'w1440', element: 'text=Visible as Alex’s Mac mini', line: 'A joiner paired: the host’s devices_updated reached the page’s websocket handler and the code boxes collapsed back to New code.', act: async (page) => { await expect(page.getByText('Ends in 9 min')).toBeVisible(); await page.evaluate(() => (window as unknown as { __remoteFrame: { pair: () => void } }).__remoteFrame.pair()); await expect(page.getByText('Ends in 9 min')).toHaveCount(0); } },
	{ id: 'dev-not-announced', url: devices('not-announced'), size: 'w1440', element: 'text=This computer isn’t visible on the network', line: 'On but not announced: the addresses to type, in monospace.' },
	{ id: 'dev-paused', url: devices('paused'), size: 'w1440', element: 'text=Pairing is paused after too many wrong codes.', line: 'Paused after too many wrong codes, in the error tone, with New code.' },
	{ id: 'dev-starting', url: devices('starting'), size: 'w1440', element: 'text=Starting…', line: 'Turned on and locked while the host starts.', act: async (page) => { await page.locator('button[aria-label="Let other devices connect"]').click(); } },
	{ id: 'dev-ports-taken', url: devices('ports-taken'), size: 'w1440', element: 'text=Other apps are using the connections CORSIA needs', line: 'Every port taken: off, the restart line and Try again.' },
	{ id: 'dev-load-failed', url: devices('load-failed'), size: 'w1440', element: 'text=Couldn’t load device settings.', line: 'The read failed: no switch, one line and Try again.' },
	{ id: 'dev-read-only', url: devices('read-only'), size: 'w1440', element: 'text=Manage connections on Studio itself.', line: 'Seen from a connected computer: no switch, the manage line.' },
	{ id: 'dev-long-name', url: devices('long-name-read-only'), size: 'w1440', element: 'text=Manage connections on Alex’s Mac mini (2)', line: 'A long computer name stays whole in the manage line.' },
	{ id: 'dev-code-393', url: devices('code'), size: 'w393', element: 'text=Ends in 9 min', line: 'The code row at phone width: the switch sits under the text.' },
	{ id: 'dev-not-announced-393', url: devices('not-announced'), size: 'w393', element: 'text=This computer isn’t visible on the network', line: 'The not-visible line wraps its addresses at phone width.' },
	{ id: 'dev-long-name-393', url: devices('long-name-read-only'), size: 'w393', element: 'text=Manage connections on Alex’s Mac mini (2)', line: 'The long name wraps under its first word at phone width.' },
	{ id: 'dev-code-dark', url: devices('code', '&theme=dark'), size: 'w1440', element: 'text=Ends in 9 min', line: 'The code row, dark.' },
];

test.describe.configure({ mode: 'serial' });
test.beforeAll(() => mkdirSync(OUT, { recursive: true }));

const shots = new Map<string, { base64: string; frame: Frame }>();

for (const frame of frames) {
	test(`frame ${frame.id}`, async ({ page }) => {
		await page.setViewportSize(SIZES[frame.size]);
		await page.goto(frame.url);
		await page.waitForLoadState('networkidle');
		if (frame.act) await frame.act(page);
		await page.waitForTimeout(500); // motion settles (nothing pops, so nothing is captured mid-flight)
		await expect(page.locator(frame.element).first()).toBeVisible();
		const buffer = await page.screenshot({ path: join(OUT, `${frame.id}.png`), fullPage: false });
		shots.set(frame.id, { base64: buffer.toString('base64'), frame });
	});
}

test('no two frames are pixel-identical', async ({ page }) => {
	await page.goto('about:blank');
	const entries = [...shots.entries()];
	expect(entries.length).toBe(frames.length);
	const result = await page.evaluate(async (items) => {
		const load = async (b64: string) => {
			const blob = await (await fetch(`data:image/png;base64,${b64}`)).blob();
			const bitmap = await createImageBitmap(blob);
			const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
			const context = canvas.getContext('2d')!;
			context.drawImage(bitmap, 0, 0);
			return { width: bitmap.width, height: bitmap.height, data: context.getImageData(0, 0, bitmap.width, bitmap.height).data };
		};
		const images = await Promise.all(items.map(async ([id, b64]) => [id, await load(b64)] as const));
		const rows: { a: string; b: string; differing: number }[] = [];
		for (let i = 0; i < images.length; i++) {
			for (let j = i + 1; j < images.length; j++) {
				const [a, left] = images[i]; const [b, right] = images[j];
				if (left.width !== right.width || left.height !== right.height) { rows.push({ a, b, differing: left.width * left.height }); continue; }
				let differing = 0;
				for (let p = 0; p < left.data.length; p += 4) {
					if (Math.abs(left.data[p] - right.data[p]) + Math.abs(left.data[p + 1] - right.data[p + 1]) + Math.abs(left.data[p + 2] - right.data[p + 2]) > 24) differing++;
				}
				rows.push({ a, b, differing });
			}
		}
		return rows;
	}, entries.map(([id, shot]) => [id, shot.base64] as [string, string]));
	const smallest = result.reduce((least, row) => (row.differing < least.differing ? row : least));
	writeFileSync(join(OUT, 'diff-summary.json'), JSON.stringify({ pairs: result.length, smallest }, null, 1));
	// Identical screens differ by 0; the closest honest pair (a countdown's wording) still differs by hundreds of pixels.
	const identical = result.filter((row) => row.differing < 100);
	expect(identical, `near-identical frames: ${JSON.stringify(identical)}`).toEqual([]);
});
