import fs from 'node:fs';
import path from 'node:path';
import { test } from './test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';
import { syntheticPng } from './syntheticImages';

/** Seeded documents, images and comments for the review bar and the comments drawer, shared by their specs. */
/** The desktop and mobile projects share one database, so every seeded thread needs a title no other run reuses. */
export const uniqueTitle = (title: string) => `${title} ${Math.random().toString(36).slice(2, 7)}`;
const seededThreadIds: string[] = [];

export async function seed(request: APIRequestContext, baseTitle: string, options: { sharedAsOf?: string } = {}) {
	const title = uniqueTitle(baseTitle);
	const threadId = await createSession(request, title);
	seededThreadIds.push(threadId);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${options.sharedAsOf ? `${options.sharedAsOf}-` : ''}${title} ${Date.now()}.md`;
	const docPath = options.sharedAsOf ? path.join(workspace, 'shared', 'documents', name) : path.join(workspace, name);
	const text = '# Rollout plan\n\n' + Array.from({ length: 12 }, (_, index) => `Step ${index + 1}: the rollout proceeds to the next ring.\n\n`).join('');
	if (options.sharedAsOf) {
		fs.mkdirSync(path.dirname(docPath), { recursive: true });
		fs.writeFileSync(path.join(workspace, 'shared', 'documents', 'INDEX.md'), '# Documents\n');
		fs.writeFileSync(docPath, text);
		const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: docPath, thread_id: threadId } });
		expect(registered.ok(), await registered.text()).toBeTruthy();
		return { threadId, title, name, docPath, text, artifactId: (await registered.json()).artifact.id as string };
	}
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { threadId, title, name, docPath, text, artifactId: (await seeded.json()).artifact.id as string };
}
export type Doc = Awaited<ReturnType<typeof seed>>;

/** A registered image the owning thread asks about; the same fixture shape as a document, with no text. */
export async function seedImage(request: APIRequestContext, baseTitle: string): Promise<Doc> {
	const title = uniqueTitle(baseTitle);
	const threadId = await createSession(request, title);
	seededThreadIds.push(threadId);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title} ${Date.now()}.png`, docPath = path.join(workspace, name);
	fs.writeFileSync(docPath, syntheticPng(640, 400));
	const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: docPath, thread_id: threadId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	const artifact = (await registered.json()).artifact;
	expect(artifact.type).toBe('image');
	return { threadId, title, name, docPath, text: '', artifactId: artifact.id as string };
}

/** A comment on the real text of "Step <n>", anchored the way the editor anchors a selection. */
export async function comment(request: APIRequestContext, doc: Doc, body: string, step: number) {
	const from = doc.text.indexOf(`Step ${step}:`), to = from + 'Step'.length;
	expect(from).toBeGreaterThan(-1);
	const created = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body, anchor_text: 'Step', from_off: from, to_off: to, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
}

/**
 * The three surfaces the bar is proved on. `default` is a desktop user's first layout: the plan pane open
 * and the default split, so the document canvas is under 600px and the bar stacks, with compact buttons
 * under a fine pointer. `closed` closes the pane for the one-row layout. `phone` emulates a touch screen,
 * where the stacked buttons are 44px.
 */
export type Mode = 'default' | 'closed' | 'phone';
export const WIDTH: Record<Mode, number> = { default: 1440, closed: 1440, phone: 393 };

export async function open(page: Page, doc: Doc, mode: Mode, theme: 'light' | 'dark') {
	await page.setViewportSize({ width: WIDTH[mode], height: mode === 'phone' ? 852 : 900 });
	await page.addInitScript(({ doc, theme, wide }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
			tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth: wide ? 700 : null }));
	}, { doc, theme, wide: mode === 'closed' });
	await loginByUrl(page, { waitForComposer: false });
	await selectThread(page, doc, mode);
	// The plan pane opens beside the document by default; the one-row layout needs the canvas's full width.
	if (mode === 'closed' && !(await page.locator('.pane').evaluate((pane) => pane.classList.contains('collapsed')))) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
		// A collapsed pane is zero wide, so it reads hidden once any width transition ends (at once under reduced motion).
		await expect(page.locator('.pane')).toHaveClass(/\bcollapsed\b/);
	}
}
export async function selectThread(page: Page, doc: Doc, mode: Mode) {
	const image = doc.name.endsWith('.png');
	if (mode === 'phone') await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: doc.title }).click();
	if (image) await expect(page.getByRole('img', { name: doc.name })).toBeVisible();
	else await expect(page.locator('.cm-content')).toContainText('Step 1:');
}

export const shot = (page: Page, name: string, info: TestInfo) => page.screenshot({ path: info.outputPath(`${name}.png`), animations: 'disabled' });

/** A pointer at the element's centre lands on the element, not on whatever is drawn above it (named in the failure). */
export const hitAtCentre = (locator: ReturnType<Page['locator']>) => locator.evaluate((el) => {
	const rect = el.getBoundingClientRect();
	const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
	return el.contains(top) ? true : `${top?.tagName.toLowerCase()}.${[...(top?.classList ?? [])].join('.')}`;
});
/** Wait out the finite animations (the ask's entrance, the pane's slide) so geometry and hit tests read the settled layout. */
export const settled = (page: Page) => page.evaluate(() => Promise.all(document.getAnimations()
	.filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
	.map((animation) => animation.finished.catch(() => undefined))));

export type Case = readonly [label: string, mode: Mode, theme: 'light' | 'dark'];
/** The starting state a journey begins from — seeded documents, comments and asks — built before its own budget. */
export type JourneySetup<Value> = (args: { request: APIRequestContext; mode: Mode; theme: 'light' | 'dark' }) => Promise<Value>;
export type Run<Value = void> = (args: { page: Page; request: APIRequestContext; info: TestInfo; mode: Mode; theme: 'light' | 'dark'; value: Value }) => Promise<void>;
/** Deletes the threads seeded since the last call, for a spec that needs a clean database between its cases. */
export async function deleteSeededThreads(request: APIRequestContext) {
	for (const threadId of seededThreadIds.splice(0)) {
		// The backend refuses a delete with 409 while a turn is still running on the thread (a comment sent for immediate delivery starts one); retry until that turn ends.
		const deleteThread = () => request.delete(`/api/threads/${threadId}?token=${TEST_TOKEN}`);
		await expect.poll(async () => (await deleteThread()).status(), { message: `delete seeded thread ${threadId}`, timeout: 20_000 }).toBeLessThan(300);
	}
}

/** The starting state is the journey's fixture work, not its subject, so it gets a budget of its own. */
const SETUP_TIMEOUT = 120_000;

/**
 * One test per case; a phone case runs with a touch screen so `(pointer: coarse)` applies.
 *
 * `setup` builds the case's starting state as a fixture with `SETUP_TIMEOUT` of its own, so the test's own timeout
 * covers the journey it asserts. That work is neither cheap nor what these journeys prove: every `raiseAsk` and
 * `addPersonStep` starts a cold Python service (its `plans.boundary` import pulls in `litellm`, 2.2 s of the 2.9 s
 * import), measured at 2.4–4.0 s on origin/dev 8a52ccc0e — up to 45 % of the journey's own 30 s, and the part of it
 * that scales worst with host load.
 */
export function journey<Value = void>(name: string, cases: readonly Case[], run: Run<Value>, setup?: JourneySetup<Value>) {
	for (const [label, mode, theme] of cases) {
		test.describe(label, () => {
			if (mode === 'phone') test.use({ hasTouch: true, isMobile: true });
			// The mobile project's Pixel 7 device is a coarse pointer, so it would turn a 1440 case's compact buttons into 44px ones.
			// Declared, not in the body: a skipped case must not build a starting state nobody opens.
			test.skip(() => mode !== 'phone' && test.info().project.name === 'chromium-mobile', 'a desktop layout is proved on the desktop project');
			const withStart = test.extend<{ journeyStart: Value }>({
				// The file's reset returns the backend to its seed, and the delivery preference with it, so seed after both.
				journeyStart: [async ({ seededBackend, deliveryPreferenceOn, request }, use) => {
					void seededBackend;
					void deliveryPreferenceOn;
					await use(setup ? await setup({ request, mode, theme }) : (undefined as Value));
				}, { timeout: SETUP_TIMEOUT }],
			});
			withStart(`@surface:review-bar ${name} (${label})`, async ({ page, request, journeyStart }, info) => {
				await run({ page, request, info, mode, theme, value: journeyStart });
			});
		});
	}
}
