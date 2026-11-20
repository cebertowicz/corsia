import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { beginComment } from '../support/liveCommentProof';
import { at, composer, drag, openThread, seedImage } from '../support/imageRegionComments';
import { clickDocImage } from '../support/docImageComments';
import { syntheticPng } from '../support/syntheticImages';

// A press of Comment whose POST the server stored but never answered (the answer and the lookup after it both fail)
// is pressed again. The draft posts under its one id, so the same text is stored once; text or tags changed since show
// the stored version and keep the change in the box, and the next press posts the change as its own comment.

const EARLIER = 'Your earlier version was posted. Your changes are kept here.';
/** The composer's own line under the text: with the box on screen it is the one place that says why. */
const earlierLine = (page: Page) => page.getByRole('alert').filter({ hasText: EARLIER });
/** Comment, or Retry once a press has failed: the composer's one submit button. */
const press = (page: Page) => page.locator('.composer').getByRole('button', { name: /^(Comment|Retry)$/ }).click();
const auth = () => ({ Authorization: `Bearer ${TEST_TOKEN}` });
const token = () => ({ token: TEST_TOKEN });
type Row = { id: string; body: string; tagged_agent_ids?: string[] };

async function rows(request: APIRequestContext, artifactId: string): Promise<Row[]> {
	const response = await request.get(`/api/artifacts/${artifactId}/comments`, { params: token() });
	expect(response.ok(), await response.text()).toBeTruthy();
	return (await response.json()).comments as Row[];
}
const bodies = async (request: APIRequestContext, artifactId: string) => (await rows(request, artifactId)).map((row) => row.body).sort();

/**
 * The first POST reaches the server, which stores it, and its answer is dropped; every comment list read fails too
 * until `answer()`, so the browser cannot find out what happened. Later POSTs go through.
 */
async function dropFirstAnswer(page: Page, artifactId: string) {
	const state = { blocking: false, posts: 0 };
	await page.route(`**/api/artifacts/${artifactId}/comments*`, async (route) => {
		const method = route.request().method();
		if (method === 'POST' && state.posts++ === 0) {
			await route.fetch();
			state.blocking = true;
			return route.abort('failed');
		}
		if (method === 'GET' && state.blocking) return route.abort('failed');
		return route.continue();
	});
	return { answer: () => { state.blocking = false; }, posted: () => state.posts };
}

/** The press failed: the box still holds the text and says it didn't post, in the box only, since the box is on screen. */
async function expectFailedPress(page: Page, text: string) {
	await expect(page.locator('.composer [role="alert"]').filter({ hasText: /post/i }), 'the box says why').toBeVisible({ timeout: 30_000 });
	await expect(composer(page)).toHaveValue(text);
	await expect(page.locator('.toast').filter({ hasText: /post/i }), 'no toast tells it a second time').toHaveCount(0);
}

const DOC = '# Retry once\n\nThe router decides where an inbound event goes.\n\nA second paragraph to keep the margin honest.\n';
const QUOTE = 'The router decides';

async function liveDocument(page: Page, request: APIRequestContext, label: string) {
	await page.setViewportSize({ width: 1440, height: 900 });
	const document = await seedLiveDocument(request, label, DOC);
	await openLiveDocument(page, document);
	return document;
}

test.describe('@flow:document-comments a comment pressed again after an unanswered post is stored once', () => {
	test.beforeEach(({}, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(150_000);
	});

	test('document, leg (ii): the same text pressed again is one comment', async ({ page, request }) => {
		const document = await liveDocument(page, request, 'retry-same');
		const wire = await dropFirstAnswer(page, document.artifactId);
		await beginComment(page, QUOTE, 'Name the fallback route');
		await press(page);
		await expectFailedPress(page, 'Name the fallback route');
		expect(await bodies(request, document.artifactId), 'the first press was stored').toEqual(['Name the fallback route']);
		wire.answer();
		await press(page);
		await expect(composer(page)).toHaveCount(0);
		expect(await bodies(request, document.artifactId), 'one comment for one draft').toEqual(['Name the fallback route']);
	});

	test('document, leg (i): text edited after an unanswered press shows the first version and keeps the edit', async ({ page, request }) => {
		const document = await liveDocument(page, request, 'retry-edit');
		const wire = await dropFirstAnswer(page, document.artifactId);
		await beginComment(page, QUOTE, 'Name the fallback route');
		await press(page);
		await expectFailedPress(page, 'Name the fallback route');
		wire.answer();
		await composer(page).fill('Name the fallback route and its timeout');
		await press(page);
		await expect(earlierLine(page)).toBeVisible();
		await expect(composer(page)).toHaveValue('Name the fallback route and its timeout');
		expect(await bodies(request, document.artifactId)).toEqual(['Name the fallback route']);
		await press(page);
		await expect(composer(page)).toHaveCount(0);
		expect(await bodies(request, document.artifactId), 'each press stored exactly one comment').toEqual([
			'Name the fallback route', 'Name the fallback route and its timeout',
		]);
	});

	test('document, leg (iii): only the tagged agent changed after an unanswered press', async ({ page, request }) => {
		const stamp = Date.now();
		const twins: string[] = [];
		for (const n of [1, 2]) {
			const id = await createSession(request, `Twin reviewer ${stamp}`);
			const promoted = await request.patch(`/api/agents/${id}`, { headers: auth(), data: { active: true, responsibility: `Reviews the document (${n}).` } });
			expect(promoted.ok(), await promoted.text()).toBeTruthy();
			twins.push(id);
		}
		const document = await liveDocument(page, request, 'retry-tag');
		const wire = await dropFirstAnswer(page, document.artifactId);
		const pick = async (nth: number) => {
			await composer(page).fill('');
			await composer(page).pressSequentially('@Twin reviewer');
			await page.getByRole('option', { name: new RegExp(`Twin reviewer ${stamp}`) }).nth(nth).click();
			await composer(page).pressSequentially('please check');
		};
		await beginComment(page, QUOTE);
		await pick(0);
		const text = await composer(page).inputValue();
		await press(page);
		await expectFailedPress(page, text);
		const [first] = await rows(request, document.artifactId);
		expect(first.tagged_agent_ids).toHaveLength(1);
		wire.answer();
		await pick(1);
		expect(await composer(page).inputValue(), 'the text is the same; only the tag differs').toBe(text);
		await press(page);
		await expect(earlierLine(page)).toBeVisible();
		expect(await rows(request, document.artifactId)).toHaveLength(1);
		await press(page);
		await expect(composer(page)).toHaveCount(0);
		const stored = await rows(request, document.artifactId);
		expect(stored.map((row) => row.tagged_agent_ids).sort()).toEqual([[twins[0]], [twins[1]]].sort());
	});

	test('image artifact, leg (i): an edited box comment shows the first version and keeps the edit', async ({ page, request }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const seeded = await seedImage(page, request, 'retry-image');
		const wire = await dropFirstAnswer(page, seeded.artifactId);
		await openThread(page, seeded.sessionTitle, 1440);
		await drag(page, await at(page, 0.2, 0.25), await at(page, 0.45, 0.5));
		await composer(page).fill('The header is too tall');
		await page.keyboard.press('ControlOrMeta+Enter');
		await expectFailedPress(page, 'The header is too tall');
		wire.answer();
		await composer(page).fill('The header is too tall and too dark');
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(earlierLine(page)).toBeVisible();
		await expect(composer(page)).toHaveValue('The header is too tall and too dark');
		expect(await bodies(request, seeded.artifactId)).toEqual(['The header is too tall']);
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(composer(page)).toHaveCount(0);
		expect(await bodies(request, seeded.artifactId)).toEqual(['The header is too tall', 'The header is too tall and too dark']);
	});

	test('image in a document, leg (i): an edited box comment shows the first version and keeps the edit', async ({ page, request }) => {
		await page.setViewportSize({ width: 1440, height: 900 });
		const stamp = Date.now();
		const title = `retry-doc-image-${stamp}`;
		const threadId = await createSession(request, title);
		const tree = await (await request.get('/api/files/tree', { params: token() })).json();
		const workspace = (tree.roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')!.path;
		const dir = path.join(workspace, title);
		fs.mkdirSync(path.join(dir, 'shots'), { recursive: true });
		fs.writeFileSync(path.join(dir, 'shots', 'screen.png'), syntheticPng(1600, 1000));
		const docPath = path.join(dir, 'walkthrough.md');
		fs.writeFileSync(docPath, '# Walkthrough\n\nThe screen below is the first step.\n\n![Mock screen](shots/screen.png)\n\nThe end.\n');
		const registered = await request.post('/api/files/register', { params: token(), data: { path: docPath, thread_id: threadId } });
		expect(registered.ok(), await registered.text()).toBeTruthy();
		const artifactId = (await registered.json()).artifact.id as string;
		await page.addInitScript(({ threadId, artifactId, docPath }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: {
					tabs: [{ instanceId: 'tab-doc', artifactId, path: docPath, title: 'walkthrough.md', loadedRevision: null, latestRevision: null, status: 'loading' }],
					activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact',
				} },
				splitWidth: null,
			}));
		}, { threadId, artifactId, docPath });
		const wire = await dropFirstAnswer(page, artifactId);
		await loginByUrl(page);
		await page.getByText(title).first().click();
		const image = page.locator('.cm-md-image img').first();
		await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(1600);
		await clickDocImage(page);
		const regions = page.locator('.image-overlay [data-image-regions]');
		await expect(regions).toBeVisible();
		await page.locator('.image-overlay .bar [data-image-comment]').click();
		const box = (await regions.boundingBox())!;
		await drag(page, { x: box.x + box.width * 0.2, y: box.y + box.height * 0.25 }, { x: box.x + box.width * 0.45, y: box.y + box.height * 0.5 });
		await composer(page).fill('The logo is cut off');
		await page.keyboard.press('ControlOrMeta+Enter');
		await expectFailedPress(page, 'The logo is cut off');
		wire.answer();
		await composer(page).fill('The logo is cut off on the left');
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(earlierLine(page)).toBeVisible();
		await expect(composer(page)).toHaveValue('The logo is cut off on the left');
		expect(await bodies(request, artifactId)).toEqual(['The logo is cut off']);
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(composer(page)).toHaveCount(0);
		expect(await bodies(request, artifactId)).toEqual(['The logo is cut off', 'The logo is cut off on the left']);
	});
});
