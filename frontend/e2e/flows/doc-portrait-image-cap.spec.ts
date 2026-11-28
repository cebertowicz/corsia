import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { loginByUrl, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { seedImageDocument, overlay } from '../support/docImageComments';
import { PORTRAIT_CAP_PX, PORTRAIT_CAP_SVH } from '../../src/lib/cm/markdown-image-portrait';

// A portrait image in a document (taller than wide) is held to a height that fits the window, its width following; a landscape or
// square image is never capped; pins stay on the same spot of the image; a press still opens the image larger.
const PHONE = { w: 393, h: 852 };
const DESKTOP = { w: 1440, h: 900 };
const FILES = {
	'screen.png': { alt: 'Phone screen', width: 1032, height: 2237 },
	'tablet.png': { alt: 'Tablet screen', width: 1032, height: 1344 },
	'phone-short.png': { alt: 'Short phone screen', width: 393, height: 516 },
	'square.png': { alt: 'Square screen', width: 800, height: 800 },
	'settings.png': { alt: 'Desktop screen', width: 1600, height: 1000 },
} as const;
const filler = (n: number) => `Paragraph ${n}: the walkthrough continues with enough words to wrap across a few lines of the document column.\n`;
const markdown = (name: keyof typeof FILES) => `![${FILES[name].alt}](shots/${name})`;
const DOC = ['# Design walkthrough', '', filler(1), ...Object.keys(FILES).flatMap((name, i) => [markdown(name as keyof typeof FILES), '', filler(i + 2)])].join('\n');
const REGION = { x: 0.3, y: 0.3, w: 0.3, h: 0.2 };
/** The height a portrait image is held to at a window height: the same two numbers the editor's theme is built from. */
const capAt = (windowHeight: number) => Math.min((PORTRAIT_CAP_SVH / 100) * windowHeight, PORTRAIT_CAP_PX);

/** Put the pointer over the document's own column, which at 1440 sits right of the chat, so a wheel scrolls the document. */
async function overDocument(page: Page) {
	const box = (await page.locator('.cm-scroller').first().boundingBox())!;
	await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
}

async function open(page: Page, request: Parameters<typeof seedImageDocument>[1], label: string, size: { w: number; h: number }, late = false) {
	// `late`: the phone screen arrives 2.5 s after the document is drawn, so what its arrival does to the text below is seen.
	if (late) await page.route(/screen\.png/, async (route) => { await new Promise((resolve) => setTimeout(resolve, 2500)); await route.continue(); });
	await page.setViewportSize({ width: size.w, height: size.h });
	const seeded = await seedImageDocument(page, request, label, null, DOC);
	const shots = path.dirname(seeded.imagePath);
	for (const [name, { width, height }] of Object.entries(FILES)) fs.writeFileSync(path.join(shots, name), syntheticPng(width, height));
	// A comment on the phone screen's region, so its pin is drawn on the image.
	const from = DOC.indexOf(markdown('screen.png'));
	const posted = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'What is this box?', anchor_text: markdown('screen.png'), from_off: from, to_off: from + markdown('screen.png').length, region: REGION, delivery_policy: 'immediate' },
	});
	expect(posted.ok(), `comment seed failed: ${posted.status()}`).toBeTruthy();
	await loginByUrl(page);
	if (size.w <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(seeded.sessionTitle).first().click();
	if (late) await expect(page.getByText('Paragraph 1:').first()).toBeVisible();
	return seeded;
}

/** Bring one document image into view and wait for it to load. The editor draws only what is near the window, so an image far from it does not exist until the document is scrolled to it. */
async function reach(page: Page, name: keyof typeof FILES) {
	const img = page.locator(`.cm-md-image img[alt="${FILES[name].alt}"]`);
	await overDocument(page);
	await page.mouse.wheel(0, -100_000);
	await page.waitForTimeout(150);
	for (let step = 0; step < 60 && (await img.count()) === 0; step++) {
		await page.mouse.wheel(0, 400);
		await page.waitForTimeout(120);
	}
	await img.scrollIntoViewIfNeeded({ timeout: 10_000 });
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 20_000 }).toBe(FILES[name].width);
	return img;
}

/** What each document image draws as: its class, its natural and drawn size, the column it sits in, and its computed max-height. */
async function measure(page: Page) {
	const rows = [];
	for (const name of Object.keys(FILES) as (keyof typeof FILES)[]) {
		const img = await reach(page, name);
		rows.push(await img.evaluate((el: HTMLImageElement) => {
			const wrap = el.closest<HTMLElement>('.cm-md-image')!;
			const box = el.getBoundingClientRect();
			return {
				alt: el.alt, portraitClass: wrap.classList.contains('cm-md-image-portrait'), maxHeight: getComputedStyle(el).maxHeight,
				naturalWidth: el.naturalWidth, naturalHeight: el.naturalHeight, width: Math.round(box.width * 10) / 10, height: Math.round(box.height * 10) / 10,
				column: Math.round(wrap.getBoundingClientRect().width * 10) / 10, innerHeight: window.innerHeight,
			};
		}));
	}
	return rows;
}

for (const size of [PHONE, DESKTOP]) {
	test.describe(`document images at ${size.w}`, () => {
		test.beforeEach(({}, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			test.setTimeout(90_000);
		});

		test('a portrait image is capped to the window and keeps its shape; a landscape or square one is untouched', async ({ page, request }, testInfo) => {
			await open(page, request, `portrait-cap-${size.w}`, size);
			const rows = await measure(page);
			expect(rows.length, 'every image of the document was measured').toBe(Object.keys(FILES).length);
			await testInfo.attach(`sizes-${size.w}.json`, { body: JSON.stringify(rows, null, 1), contentType: 'application/json' });
			const cap = capAt(size.h);
			for (const row of rows) {
				const ratio = row.naturalHeight / row.naturalWidth;
				const uncappedWidth = Math.min(row.naturalWidth, row.column);
				const uncapped = { width: uncappedWidth, height: uncappedWidth * ratio };
				if (ratio > 1) {
					const height = Math.min(uncapped.height, cap);
					expect(row.height, `${row.alt}: a portrait image is at most ${cap}px high, as drawn ${JSON.stringify(row)}`).toBeLessThanOrEqual(cap + 0.5);
					expect(row.height, `${row.alt}: it takes its uncapped height when that fits, else the cap`).toBeCloseTo(height, 0);
					expect(row.width, `${row.alt}: its width follows to keep its shape`).toBeCloseTo(height / ratio, 0);
				} else {
					expect(row.portraitClass, `${row.alt}: a landscape or square image is not marked`).toBe(false);
					expect(row.maxHeight, `${row.alt}: it has no height cap`).toBe('none');
					expect(row.width, `${row.alt}: width unchanged`).toBeCloseTo(uncapped.width, 0);
					expect(row.height, `${row.alt}: height unchanged`).toBeCloseTo(uncapped.height, 0);
				}
			}
		});

		test('the text under a late-loading portrait image is pushed down by no more than the image draws', async ({ page, request }, testInfo) => {
			await open(page, request, `portrait-late-${size.w}`, size, true);
			const gap = () => page.evaluate(() => {
				const top = (text: string) => [...document.querySelectorAll<HTMLElement>('.cm-line')].find((line) => line.textContent?.startsWith(text))!.getBoundingClientRect().top;
				return top('Paragraph 2:') - top('Paragraph 1:');
			});
			const before = await gap();
			const img = page.locator('.cm-md-image img[alt="Phone screen"]');
			const loadedBefore = await img.evaluate((el: HTMLImageElement) => el.naturalWidth);
			expect(loadedBefore, 'the phone screen has not arrived yet when the first reading is taken').toBe(0);
			await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth), { timeout: 20_000 }).toBe(FILES['screen.png'].width);
			await page.waitForTimeout(800);
			const after = await gap();
			const drawn = (await img.boundingBox())!.height;
			await testInfo.attach(`late-${size.w}.json`, { body: JSON.stringify({ before, after, shift: after - before, drawn, loadedBefore }), contentType: 'application/json' });
			expect(after - before, `the text moves down by at most the image's drawn height plus its padding (before ${before}, after ${after}, drawn ${drawn})`).toBeLessThanOrEqual(drawn + 20);
			expect(after - before, `and by no more than the cap plus its padding, so an image that was not held to the cap fails here (cap ${capAt(size.h)})`).toBeLessThanOrEqual(capAt(size.h) + 20);
		});

		test('a pin stays on the same spot of its image, and a press opens the image larger', async ({ page, request }, testInfo) => {
			await open(page, request, `portrait-pin-${size.w}`, size);
			const image = await reach(page, 'screen.png');
			const pin = page.locator('.cm-image-pin .disc').first();
			await expect(pin).toBeVisible();
			await page.waitForTimeout(600);
			const spot = await page.evaluate(() => {
				const box = document.querySelector<HTMLElement>('.cm-md-image img[alt="Phone screen"]')!.getBoundingClientRect();
				const disc = document.querySelector<HTMLElement>('.cm-image-pin .disc')!.getBoundingClientRect();
				return { fx: (disc.left + disc.width / 2 - box.left) / box.width, fy: (disc.top + disc.height / 2 - box.top) / box.height, width: box.width, height: box.height };
			});
			await testInfo.attach(`pin-${size.w}.json`, { body: JSON.stringify(spot), contentType: 'application/json' });
			expect(spot.fx, 'the pin is inside the image, across').toBeGreaterThan(0);
			expect(spot.fx).toBeLessThan(1);
			expect(spot.fy, 'the pin is inside the image, down').toBeGreaterThan(0);
			expect(spot.fy).toBeLessThan(1);
			await page.screenshot({ path: testInfo.outputPath(`frame-${size.w}.png`) });
			await testInfo.attach(`frame-${size.w}.png`, { path: testInfo.outputPath(`frame-${size.w}.png`), contentType: 'image/png' });
			// A press on bare image opens it in the viewer at its full size.
			const bare = await image.evaluate((el) => {
				const box = el.getBoundingClientRect();
				for (const [fx, fy] of [[0.5, 0.8], [0.5, 0.5], [0.8, 0.9], [0.2, 0.9]]) {
					if (document.elementFromPoint(box.left + box.width * fx, box.top + box.height * fy) === el) return { x: box.width * fx, y: box.height * fy };
				}
				return null;
			});
			expect(bare, 'part of the image is bare of pins').not.toBeNull();
			await image.click({ position: bare! });
			await expect(overlay(page)).toBeVisible();
			await expect.poll(() => overlay(page).locator('.frame img').evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(FILES['screen.png'].width);
		});
	});
}
