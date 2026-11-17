import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { closeComments, expectCommentsShown, openComments } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { raiseAsk } from '../support/reviewAsk';
import { expectSenders, headerSend, labelLines, leaveUnsent, shownHeaderSend } from '../support/heldComments';
import { closeDetails, expectTouchTargets, openDetails } from '../support/documentBar';
import { journey, type Case, seed, type Doc, seedImage, comment, type Mode, WIDTH, open, selectThread, shot, hitAtCentre, settled, type JourneySetup } from '../support/reviewFixtures';

/**
 * The in-view review bar (DCOL-FR12.4) on the real composed canvas: an agent's raised ask on an open
 * document, answered from the reading. The ask is raised through the plan service as the owning agent
 * thread (an agent's `block_node` with `ask_artifact_id`); everything the user does goes through the UI.
 */
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');

function addPersonStep(threadId: string, artifactId: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The person-step fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'from infra.db_fts import _init_messages_fts',
		'from infra.input_admission import accept_thread_input',
		'thread, artifact = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'intent = "Set up the workspace from the instructions."',
		'if db.execute("SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=\'messages_fts\'").fetchone() is None: _init_messages_fts(db)',
		'message_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex',
		'accept_thread_input(thread, role="user", content=intent, intent="apply_now", message_id=message_id, db=db)',
		'db.execute("UPDATE messages SET active_turn_id=? WHERE id=?", (turn_id, message_id))',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'params = {"kind": "task", "title": "Set up the workspace", "acceptance_criteria": "It is ready.", "size": "unknown", "assignee": "user", "description": "Follow the document. Already tried: I opened the instructions.", "instructions_artifact_id": artifact, "intent": intent}',
		'mutate(plan.id, "add", params, actor=PlanActor(thread, user, None, turn_id=turn_id), provenance="e2e.person_step", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId], {
		cwd: backendDir, encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

/** The live-placement path derives orphaning from the shown text; make every fixture comment's quote unavailable before the UI reads it. */
function orphanComments(artifactId: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The orphan fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sqlite3, sys',
		'connection = sqlite3.connect(sys.argv[1])',
		'changed = connection.execute("UPDATE doc_comments SET anchor_text=? WHERE artifact_id=?", ("Quote missing from the document.", sys.argv[2])).rowcount',
		'assert changed == 22, changed',
		'connection.commit()',
		'connection.close()',
	].join('\n'), path.join(state, 'corsia.db'), artifactId], { cwd: backendDir, encoding: 'utf8' });
}

/** The ask: on a document, the card docked to the canvas's bottom edge; on an image, its row in the image header. */
const bar = (page: Page) => page.locator('.review-bar, .rb-dock');
/** The document's docked ask card. */
const card = (page: Page) => page.locator('[data-ask-card]');
/** Opens or closes the docked card with a press on its question, as a person does. */
async function setCardOpen(page: Page, open: boolean) {
	const question = card(page).locator('.rb-dock-ask');
	if ((await question.getAttribute('aria-expanded')) !== String(open)) await question.click();
	await expect(question).toHaveAttribute('aria-expanded', String(open));
}
/** A MiddleName shows whole: its head, the part that would ellipsize, is not cut. */
const nameWhole = (name: import('@playwright/test').Locator) => name.locator('.name-head').evaluate((el) => el.scrollWidth <= el.clientWidth);
for (const width of [1280, 1440, 393] as const) for (const theme of ['light', 'dark'] as const) test.describe(`delivery header ${width} ${theme}`, () => {
	// A desktop width is measured as a desktop (pointer, no mobile viewport), also under the phone project, where the Pixel 7 device would otherwise give it the touch layout.
	test.use(width === 393 ? { hasTouch: true, isMobile: true } : { hasTouch: false, isMobile: false });
	test('long person name and every header action remain reachable', async ({ page, request }) => {
		const doc = await seed(request, 'Person with a very long agent name closed');
		addPersonStep(doc.threadId, doc.artifactId);
		await open(page, doc, width === 1440 ? 'closed' : width === 393 ? 'phone' : 'default', theme);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 }); await settled(page);
		// The plan pane takes the overlay layout once the resize reaches it; measure only after it has.
		if (width === 1280) await expect(page.locator('.pane.overlay')).toHaveCount(1);
		// Opened, the person step's card gives who asks its own line above the question; the document's controls are Comments and
		// Details in the bar.
		await setCardOpen(page, true);
		const geometry = await card(page).evaluate((element) => {
			const who = element.querySelector('.rb-dock-who')!, range = document.createRange(); range.selectNodeContents(who);
			const text = range.getBoundingClientRect(), box = element.getBoundingClientRect(), style = getComputedStyle(element);
			const tools = [...document.querySelectorAll('[data-comments-entry], [data-document-details]')].map((button) => button.getBoundingClientRect()).filter((rect) => rect.width > 0);
			return { text: text.toJSON(), contentRight: box.right - parseFloat(style.paddingRight), toolsLeft: Math.min(...tools.map((rect) => rect.left)),
				toolsRight: Math.max(...tools.map((rect) => rect.right)), tools: tools.length, name: who.textContent,
				paneOverlay: !!document.querySelector('.pane.overlay') };
		});
		console.info('Delivery header geometry:', JSON.stringify({ width, theme, ...geometry }));
		expect(geometry.name).toBe(doc.title);
		expect(geometry.text.right, 'full name stays inside the card').toBeLessThanOrEqual(geometry.contentRight + 0.5);
		expect(geometry.tools, 'Comments and Details are in the bar').toBe(2);
		expect(geometry.toolsLeft, 'every bar tool starts inside the screen').toBeGreaterThanOrEqual(0);
		expect(geometry.toolsRight, 'every bar tool ends inside the screen').toBeLessThanOrEqual(width + 0.5);
		for (const button of await page.locator('[data-comments-entry], [data-document-details], [data-ask-card] button:visible').all()) expect(await hitAtCentre(button), await button.getAttribute('aria-label') ?? await button.innerText()).toBe(true);
		expect(await hitAtCentre(bar(page).getByRole('button', { name: 'Done', exact: true }))).toBe(true);
		if (width === 1280) expect(geometry.paneOverlay).toBe(true);
		const output = process.env.HELD_COMMENT_DELIVERY_FRAMES_OUT;
		if (output) await page.screenshot({ path: path.join(output, `reviewbar-header-long-name-${width}-${theme}.png`), scale: 'css' });
		await bar(page).getByRole('button', { name: 'Done', exact: true }).click();
		await expect(bar(page).locator('.rb-receipt')).toHaveText('Done');
	});
});
/** A phone held sideways (852x393): the card's question stays one line at rest there, whatever the pane does, so the short screen keeps the document. The numbers go to the PR body. */
for (const [width, height, mode] of [[852, 393, 'closed'], [852, 393, 'phone'], [393, 852, 'phone'], [375, 667, 'phone']] as const) test.describe(`delivery header ${width}x${height} ${mode}`, () => {
	test.use({ hasTouch: true, isMobile: true });
	test('long person name: the header height and its share of the viewport', async ({ page, request }, info) => {
		test.skip(info.project.name !== 'chromium-desktop', 'sizes are set per case on the desktop project');
		const doc = await seed(request, 'Person with a very long agent name closed');
		addPersonStep(doc.threadId, doc.artifactId);
		await open(page, doc, mode, 'light');
		await page.setViewportSize({ width, height }); await settled(page);
		await page.waitForTimeout(500);
		const header = await card(page).evaluate((el) => {
			const text = el.querySelector<HTMLElement>('.rb-dock-text')!;
			const canvas = el.closest<HTMLElement>('.document-canvas')?.getBoundingClientRect();
			return { cardHeight: Math.round(el.getBoundingClientRect().height), canvasTop: Math.round(canvas?.top ?? 0),
				lines: Math.round(text.getBoundingClientRect().height / parseFloat(getComputedStyle(text).lineHeight)) };
		});
		const share = Math.round((height - header.canvasTop - header.cardHeight) / height * 1000) / 10;
		console.info('HEADER_NUMBERS', JSON.stringify({ width, height, mode, ...header, share }));
		info.annotations.push({ type: 'header-height', description: `${width}x${height} ${mode}: card ${header.cardHeight}px below ${header.canvasTop}px of chrome, the document gets ${share}% of the viewport, question ${header.lines} line(s)` });
		if (height <= 420) expect(header.lines, 'a short viewport keeps the question on one line').toBe(1);
	});
});
/**
 * The ask's geometry per surface. A document's card is docked across the canvas's bottom edge at every width, its question one
 * line at rest above the answers; an image's ask keeps its row in the image header. Answers are compact and right-aligned at a
 * fine pointer and 44px touch targets on a phone.
 */
async function expectLayout(page: Page, mode: Mode) {
	const canvas = () => page.locator('.document-canvas, .image-canvas').first().evaluate((el) => el.getBoundingClientRect().width);
	if (mode === 'closed') {
		// The pane's width transition resizes the canvas: measure only once the transition has finished, never mid-way.
		await page.locator('.pane').evaluate((pane) => Promise.all(pane.getAnimations().map((animation) => animation.finished)));
		await expect.poll(canvas).toBeGreaterThan(600);
	}
	else if (mode === 'default') expect(await canvas(), 'the default layout is a narrow canvas').toBeLessThan(600);
	const docked = (await card(page).count()) > 0;
	let paddingRight = 24;
	if (docked) {
		const measured = await card(page).evaluate((el) => {
			const canvas = el.closest<HTMLElement>('.document-canvas')?.getBoundingClientRect() ?? null;
			const text = el.querySelector<HTMLElement>('.rb-dock-text');
			const style = getComputedStyle(el);
			return {
				// The answers end at the card's inner edge: its right padding plus its border.
				box: el.getBoundingClientRect().toJSON(), canvas: canvas?.toJSON() ?? null, paddingRight: parseFloat(style.paddingRight) + parseFloat(style.borderRightWidth),
				atRest: !el.classList.contains('open') && !el.classList.contains('noting'),
				text: text ? { height: text.getBoundingClientRect().height, line: parseFloat(getComputedStyle(text).lineHeight) } : null,
			};
		});
		test.info().annotations.push({ type: 'ask-card', description: `${mode}: ${JSON.stringify(measured)}` });
		paddingRight = measured.paddingRight;
		expect(measured.canvas, 'the card sits in the document canvas').not.toBeNull();
		// The card reaches past the canvas's padding to its edges; a vertical scrollbar may take the last few pixels on the right.
		expect(measured.box.left, 'the card starts at the canvas\'s left edge').toBeLessThanOrEqual(measured.canvas!.left + 1);
		expect(measured.box.right, 'the card reaches the canvas\'s right edge').toBeGreaterThanOrEqual(measured.canvas!.right - 20);
		expect(Math.abs(measured.box.bottom - measured.canvas!.bottom), 'the card sits on the canvas\'s bottom edge').toBeLessThanOrEqual(1);
		if (measured.atRest && measured.text) {
			expect(measured.text.height, 'at rest the question is one line').toBeLessThanOrEqual(measured.text.line * 1.5);
			// The question and answers form two rows, including the approved touch-target spacing.
			expect(measured.box.height, 'at rest the card is two rows: the question, then the answers').toBeLessThanOrEqual(108);
		}
	}
	const box = (await bar(page).boundingBox())!;
	const buttons = await bar(page).locator('.btn').evaluateAll((els) =>
		els.map((el) => { const r = el.getBoundingClientRect(); return { h: r.height, w: r.width, right: r.right }; }));
	expect(buttons.length).toBeGreaterThanOrEqual(2);
	if (mode === 'phone') {
		for (const button of buttons) expect(button.h, 'touch target').toBeGreaterThanOrEqual(44);
		expect(box.width).toBeLessThanOrEqual(WIDTH.phone);
		return;
	}
	for (const button of buttons) {
		expect(button.h, 'compact header-style button').toBeLessThanOrEqual(32);
		expect(button.w, 'never stretched to half the bar').toBeLessThan(box.width * 0.45);
	}
	expect(box.x + box.width - Math.max(...buttons.map((button) => button.right)), 'right-aligned').toBeLessThanOrEqual(paddingRight + 0.5);
}

test('@surface:review-bar person-step and document-review bars render at 1280px', async ({ page, request }, info) => {
	test.skip(info.project.name === 'chromium-mobile', 'the two required 1280px receipts are desktop evidence');
	const person = await seed(request, 'Person instructions');
	addPersonStep(person.threadId, person.artifactId);
	await open(page, person, 'default', 'light');
	await page.setViewportSize({ width: 1280, height: 900 });
	await expect(bar(page).locator('.rb-dock-q')).toHaveText('Needs you to do this: Set up the workspace');
	await expect(bar(page).getByRole('button', { name: 'Done', exact: true })).toBeVisible();
	await page.screenshot({ path: info.outputPath('person-step-instructions.png'), animations: 'disabled' });

	const review = await seed(request, 'Document review');
	raiseAsk(review.threadId, review.artifactId, 'Is the rollout plan right?');
	await open(page, review, 'default', 'light');
	await page.setViewportSize({ width: 1280, height: 900 });
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await page.screenshot({ path: info.outputPath('document-review.png'), animations: 'disabled' });
});

/** The person step through the review ask's shell: 1280 with the pane open (the narrow header), 1440 with it closed (the wide one) and a 393 phone. */
const PERSON_STEP: readonly Case[] = [
	['1280 light', 'default', 'light'], ['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light'],
];
/** A fresh instruction document with the person's step on it: the starting state every person-step case answers from. */
const personStepStart = (name: string): JourneySetup<Doc> => async ({ request, mode }) => {
	const doc = await seed(request, `${name} ${mode}`);
	addPersonStep(doc.threadId, doc.artifactId);
	return doc;
};

/** Opens that person step and proves the ask is on it; each answer gets its own test, so each pays for one open. */
async function personStep(page: Page, doc: Doc, mode: Mode, theme: 'light' | 'dark') {
	await open(page, doc, mode, theme);
	if (mode === 'default') await page.setViewportSize({ width: 1280, height: 900 });
	const who = bar(page).locator('.rb-dock-who');
	await expect(who).toHaveText(doc.title);
	await expect(bar(page).locator('.rb-dock-q')).toHaveText('Needs you to do this: Set up the workspace');
	// The pane and the bar animate in after the resize; measure the layout they land in.
	await settled(page);
	// At rest the card shows the question alone; opened, the asker has its own line. MiddleName's head is the innermost element
	// that ellipsizes, so it is the one to measure.
	await setCardOpen(page, true);
	await expect(who).toBeVisible();
	expect(await nameWhole(who), 'the owner’s name is never truncated').toBe(true);
	await setCardOpen(page, false);
	return (label: 'Done' | 'Won’t do') => bar(page).getByRole('button', { name: label, exact: true });
}

journey('mark a person step done from its instructions', PERSON_STEP, async ({ page, value: doc, info, mode, theme }) => {
	const button = await personStep(page, doc, mode, theme);
	await settled(page);
	await shot(page, 'person-step-ask', info);
	await button('Done').click();
	await expect(bar(page).locator('.rb-receipt')).toHaveText('Done');
	await expect(bar(page).locator('.rb-receipt')).toBeFocused();
	await settled(page);
	await shot(page, 'person-step-done', info);
}, personStepStart('Person done'));

// A name too long for the narrow canvas (1280 with the pane open) shows whole on its own line when the card is opened.
journey('a long agent name on a person step shows whole', PERSON_STEP, async ({ page, value: doc, info, mode, theme }) => {
	await personStep(page, doc, mode, theme);
	const who = bar(page).locator('.rb-dock-who');
	await setCardOpen(page, true);
	if (mode === 'default') {
		// The split keeps re-laying out for a few hundred ms after the resize to 1280, starting after `settled` has looked for
		// running animations, so the canvas reaches its narrow width a little later: poll rather than read it once.
		await expect.poll(() => nameWhole(who), { message: 'the long name shows whole in the narrow canvas' }).toBe(true);
		// Its own line: the question starts below the name, not beside it.
		const below = await card(page).evaluate((el) => el.querySelector('.rb-dock-q')!.getBoundingClientRect().top >= el.querySelector('.rb-dock-who')!.getBoundingClientRect().bottom - 0.5);
		expect(below, 'the question starts below the asker’s line').toBe(true);
	}
	await shot(page, 'person-step-long-name', info);
}, personStepStart('Person with a very long agent name'));

journey('decline a person step from its instructions', PERSON_STEP, async ({ page, value: doc, info, mode, theme }) => {
	const button = await personStep(page, doc, mode, theme);
	await button('Won’t do').click();
	await expect(bar(page).locator('.rb-receipt')).toHaveText('Won’t do');
	await settled(page);
	await shot(page, 'person-step-wont-do', info);
}, personStepStart('Person wont'));

// A stale revision: the server refuses the answer and the bar says so, keeping the step to answer again.
journey('a stale answer to a person step keeps it open', PERSON_STEP, async ({ page, value: doc, info, mode, theme }) => {
	const button = await personStep(page, doc, mode, theme);
	await page.route('**/api/plans/*/mutations', (route) => {
		const body = route.request().postDataJSON() as { params: { expected_revision: number } };
		body.params.expected_revision -= 1;
		return route.continue({ postData: JSON.stringify(body) });
	}, { times: 1 });
	await button('Done').click();
	await expect(bar(page).locator('.rb-status')).toHaveText('This task changed. Refreshing the latest state.');
	await expect(bar(page).locator('.rb-receipt')).toHaveCount(0);
	await settled(page);
	await shot(page, 'person-step-conflict', info);
}, personStepStart('Person stale'));

const ALL: readonly Case[] = [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
];

journey('approve a plain ask from the reading', ALL, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review plain ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is the rollout plan right?');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toBeVisible();
	await expect(bar(page)).toContainText('Is the rollout plan right?');
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await expectLayout(page, mode);
	if (mode === 'phone') {
		// At rest the card shows the question alone; opened, the requester has its own line, whole.
		const leadAsk = bar(page).locator('.rb-dock-q');
		const requester = bar(page).locator('.rb-dock-who');
		await expect(leadAsk).toBeVisible();
		await expect(leadAsk).toHaveText('Is the rollout plan right?');
		await expect(requester).toBeHidden();
		await setCardOpen(page, true);
		await expect(requester).toHaveText(doc.title);
		await expect(requester).toBeVisible();
		expect(await nameWhole(requester), 'the short ask leaves its requester untruncated').toBe(true);
		await setCardOpen(page, false);
	}
	await shot(page, 'ask', info);
	await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
	const receipt = bar(page).locator('.rb-receipt');
	await expect(receipt).toContainText('Approved');
	await expect(receipt).toBeFocused();
	await shot(page, 'receipt', info);
	await page.reload();
	await selectThread(page, doc, mode);
	await expect(bar(page)).toHaveCount(0);
});

journey('send back after two comments', ALL, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review changes ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Check the ring order.');
	const waiting = [await comment(request, doc, 'Ring two should wait.', 2), await comment(request, doc, 'Add a rollback line.', 5)];
	for (const id of waiting) await leaveUnsent(request, id);
	await open(page, doc, mode, theme);
	const sendBack = bar(page).getByRole('button', { name: 'Send back', exact: true });
	await expect(sendBack).toBeVisible();
	await expectLayout(page, mode);
	await expect(page.getByText('Place again')).toHaveCount(0);
	// One primary on screen: the card's Approve. The held pair has the header's Send in Details, a secondary, and Comments names the unsent pair.
	await expect(headerSend(page).locator('.send-full')).toHaveText('Send 2 comments');
	await openDetails(page);
	await expect(headerSend(page)).not.toHaveClass(/primary/);
	await expectSenders(page, ['header: Send 2 comments']);
	await closeDetails(page);
	await expect(page.locator('[data-comments-entry]')).toHaveAttribute('aria-label', /, 2 waiting for Send$/);
	await expect(sendBack).not.toHaveClass(/primary/);
	await shot(page, 'ask-with-changes', info);
	await sendBack.click();
	// With comments in the round the note is optional: the note's Send back sends without one, and the pair goes with it.
	const send = bar(page).getByRole('button', { name: 'Send back', exact: true });
	await expect(send).toBeEnabled();
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('Sent back');
	await expect(bar(page).locator('.rb-receipt')).not.toContainText('comments');
	await expect(page.locator('[data-comments-entry]')).not.toHaveAttribute('aria-label', /waiting for Send/);
	await expect(page.getByText('Place again')).toHaveCount(0);
	await shot(page, 'receipt-with-changes', info);
});

journey('an approval receipt counts comments only', [
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review mixed receipt ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Check the handoff.');
	await open(page, doc, mode, theme);
	await page.route('**/api/attention/*/clear', async (route) => {
		await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ review: { verdict: 'approved', note: null, feedback_counts: { comments: 2, suggestions: 1, edits: 0 } } }) });
	});
	await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
	const receipt = bar(page).locator('.rb-receipt');
	await expect(receipt).toContainText('2 comments sent');
	await expect(receipt).not.toContainText('suggestion');
	await shot(page, 'receipt-mixed-counts', info);
});

journey('request changes with only a note', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review note ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
	await open(page, doc, mode, theme);
	await bar(page).getByRole('button', { name: 'Send back', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await expect(note).toBeFocused();
	const send = bar(page).getByRole('button', { name: 'Send back', exact: true });
	await expect(send).toBeDisabled();
	await note.fill('Not yet: the rollback is missing.');
	await expectLayout(page, mode);
	await shot(page, 'noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('Sent back');
	await shot(page, 'receipt-requested', info);
	const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
});

journey('answer a plain ask on an image', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seedImage(request, `Review image ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this the right diagram?');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toContainText('Is this the right diagram?');
	// An image takes no comments, so Approve is always plain and Send back always needs a note.
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await expect(bar(page).getByRole('button', { name: 'Send back', exact: true })).toBeVisible();
	await expectLayout(page, mode);
	await shot(page, 'image-ask', info);
	await bar(page).getByRole('button', { name: 'Send back', exact: true }).click();
	const send = bar(page).getByRole('button', { name: 'Send back', exact: true });
	await expect(send).toBeDisabled();
	await bar(page).getByRole('textbox').fill('Use the new palette.');
	await shot(page, 'image-noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('Sent back');
	await expect(page.getByRole('img', { name: doc.name })).toBeVisible();
	await shot(page, 'image-receipt', info);
	const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
});

journey('a note in flight keeps its layout', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review sending ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/attention/*/clear', async (route) => { await held; await route.continue(); });
	await open(page, doc, mode, theme);
	await bar(page).getByRole('button', { name: 'Send back', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await note.fill('Not yet: the rollback is missing.');
	await bar(page).getByRole('button', { name: 'Send back', exact: true }).click();

	// Mid-send the bar is still the note layout: the same input, only the sender busy, nothing else pressable.
	await expect(bar(page).locator('.spinner')).toHaveCount(1);
	await expect(note).toBeVisible();
	await expect(note).toHaveValue('Not yet: the rollback is missing.');
	await expect(note).toHaveJSProperty('readOnly', true);
	await expect(bar(page).getByRole('button', { name: 'Approve' })).toHaveCount(0);
	await expect(bar(page).getByRole('button', { name: 'Cancel' })).toBeDisabled();
	await expect(bar(page).getByRole('status').filter({ hasText: 'Sending your review' })).toHaveCount(1);
	// The buttons ease into their disabled look; measure once that has settled.
	await page.waitForTimeout(400);
	await expectLayout(page, mode);
	await shot(page, 'note-sending', info);
	release();
	await expect(bar(page).locator('.rb-receipt')).toContainText('Sent back');
});

journey('the comments drawer keeps 44px touch targets', [['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review drawer ${mode} ${theme}`);
	await comment(request, doc, 'Ring two should wait.', 2);
	await comment(request, doc, 'Add a rollback line.', 5);
	await open(page, doc, mode, theme);
	// The pill names its comments, then what waits for Send: "2 comments, 2 waiting for Send".
	await openComments(page, /^2 comments\b/);
	const drawer = page.locator('[data-doc-comments-drawer]');
	await expectCommentsShown(drawer);
	const close = drawer.getByRole('button', { name: 'Close comments', exact: true });
	const jump = drawer.locator('[data-comment-jump]').first();
	await expect(drawer.locator('[data-drawer-filter]'), 'a text document has no filter').toHaveCount(0);
	await settled(page);
	for (const target of [close, jump]) expect((await target.boundingBox())!.height, 'touch target').toBeGreaterThanOrEqual(44);
	// A real tap on a quote jump leaves every drawer control reachable.
	const jumpBox = (await jump.boundingBox())!;
	await page.touchscreen.tap(jumpBox.x + jumpBox.width / 2, jumpBox.y + jumpBox.height / 2);
	await expectCommentsShown(drawer);
	// A quote jump moves the phone sheet to its half detent. Wait for that finite transition before measuring the close control.
	await settled(page);
	await expect(hitAtCentre(close)).resolves.toBe(true);
	await shot(page, 'rs-09-half-sheet-after-quote-tap', info);
	await shot(page, 'drawer-touch', info);
	await close.click();
	await expect(drawer).toBeHidden();
});

journey('the review controls remain usable in the phone comments sheet', [['393 light', 'phone', 'light']], async ({ page, request, mode, theme }) => {
	const doc = await seed(request, 'Review controls in comments');
	raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout.');
	await comment(request, doc, 'Check the first step.', 1);
	await open(page, doc, mode, theme);
	await openComments(page, /^1 comment\b/);
	const drawer = page.locator('[data-doc-comments-drawer]');
	// No verdict button is on screen while the sheet is up, in it or behind it; closing the sheet brings the bar back.
	const approve = page.getByRole('button', { name: 'Approve', exact: true });
	await expect(drawer.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
	await expect(approve).toBeHidden();
	await drawer.getByPlaceholder('Reply…').first().focus();
	await expect(approve).toBeHidden();
	await closeComments(drawer);
	await expect(approve).toBeVisible();
	await approve.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('Approved');
});

test.describe('375px review sheet', () => {
	test.use({ hasTouch: true, isMobile: true });
	test('@surface:review-bar suggestion actions fit the bar at 375px and the sheet holds none of them', async ({ page, request }) => {
		const doc = await seed(request, 'Review sheet suggestion actions');
		raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout.');
		const reviewer = await createSession(request, 'Sheet suggester');
		for (const step of [1, 2]) {
			const result = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { artifact_id: doc.artifactId, quote: `Step ${step}: the rollout`, replacement: `Phase ${step}: the rollout`, body: 'Use phase.', as_session_id: reviewer } });
			expect(result.ok()).toBeTruthy();
		}
		await open(page, doc, 'phone', 'light');
		await page.setViewportSize({ width: 375, height: 852 });
		// The card's two answers sit on one row at 375px, Approve saying it also accepts the open suggestions; the sheet holds no copy of them.
		const buttons = card(page).locator('.rb-actions .btn');
		const approve = card(page).getByRole('button', { name: 'Approve', exact: true });
		await expect(buttons).toHaveCount(2);
		await expect(approve).toBeVisible();
		await expect(card(page).locator('.rb-derived')).toHaveText('Also accepts 2 suggestions');
		// Every answer on the card is a 44 px target with 8 px to each neighbour.
		await expectTouchTargets(card(page), '.rb-actions .btn', { min: 44, gap: 8 });
		// The question's tap area (its box plus the ::after reach) keeps 8 px from the answers, and the note is whole, never cut.
		const clearance = await card(page).evaluate((root) => {
			const ask = root.querySelector<HTMLElement>('.rb-dock-ask')!;
			const tapBottom = ask.getBoundingClientRect().bottom - parseFloat(getComputedStyle(ask, '::after').bottom);
			const answersTop = Math.min(...[...root.querySelectorAll<HTMLElement>('.rb-actions .btn')].map((button) => button.getBoundingClientRect().top));
			const note = root.querySelector<HTMLElement>('.rb-derived')!;
			return { gap: answersTop - tapBottom, noteCut: note.scrollWidth > note.clientWidth + 0.5 };
		});
		expect(clearance.gap, 'the question tap area keeps 8 px from the answers').toBeGreaterThanOrEqual(7.5);
		expect(clearance.noteCut, 'the note is whole').toBe(false);
		await openComments(page, /^\d+ comments?\b/);
		// So is every suggestion's Accept in the sheet.
		const sheet = page.locator('[data-doc-comments-drawer]');
		await expect(sheet.locator('.ct-suggestion-accept').first()).toBeVisible();
		await expectTouchTargets(sheet, '.ct-suggestion-accept', { min: 44, gap: 8 });
		await expect(page.locator('[data-doc-comments-drawer]').getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
		await expect(page.locator('[data-doc-comments-drawer] [data-ask-card], [data-doc-comments-drawer] .review-bar')).toHaveCount(0);
		await closeComments(page.locator('[data-doc-comments-drawer]'));
		await expect(approve).toBeVisible();
		// Both answers share one row, inside the 375px width.
		const boxes = await buttons.evaluateAll((items) => items.map((item) => { const r = item.getBoundingClientRect(); return { top: Math.round(r.top), left: r.left, right: r.right }; }));
		expect(new Set(boxes.map((box) => box.top)).size, 'the answers are one row').toBe(1);
		for (const box of boxes) { expect(box.left).toBeGreaterThanOrEqual(0); expect(box.right).toBeLessThanOrEqual(375); }
	});
});

journey('a review note survives moving into and out of the phone comments sheet', [['393 light', 'phone', 'light']], async ({ page, request, mode, theme }) => {
	const doc = await seed(request, 'Review note across comments');
	raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout.');
	await comment(request, doc, 'Check the first step.', 1);
	await open(page, doc, mode, theme);
	await page.getByRole('button', { name: 'Send back', exact: true }).click();
	await page.getByRole('textbox', { name: /^Note for / }).fill('Keep this note');
	await openComments(page, /^1 comment\b/);
	const drawer = page.locator('[data-doc-comments-drawer]');
	// The sheet holds comments only: the review controls leave the screen while it is up, and the note waits in the bar.
	await expect(drawer.getByRole('textbox', { name: /^Note for / })).toHaveCount(0);
	await expect(page.getByRole('textbox', { name: /^Note for / })).toHaveCount(0);
	await closeComments(drawer);
	await expect(page.getByRole('textbox', { name: /^Note for / })).toHaveValue('Keep this note');
	await page.getByRole('button', { name: 'Cancel', exact: true }).click();
	await expect(page.getByRole('button', { name: 'Send back', exact: true })).toBeFocused();
});

journey('a pending review send survives moving through the phone comments sheet', [['393 light', 'phone', 'light']], async ({ page, request, mode, theme }) => {
	const doc = await seed(request, 'Review send across comments');
	raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout.');
	await comment(request, doc, 'Check the first step.', 1);
	await open(page, doc, mode, theme);
	let release!: () => void;
	let started!: () => void;
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	const sent = new Promise<void>((resolve) => { started = resolve; });
	await page.route(`**/api/attention/${doc.artifactId}/clear`, async (route) => { started(); await blocked; await route.continue(); });
	await page.getByRole('button', { name: 'Send back', exact: true }).click();
	await page.getByRole('textbox', { name: /^Note for / }).fill('Please revise');
	await page.getByRole('button', { name: 'Send back', exact: true }).click();
	await sent;
	await openComments(page, /^1 comment\b/);
	const drawer = page.locator('[data-doc-comments-drawer]');
	await expect(drawer).toBeVisible();
	await expect(drawer.getByText('Sending your review…')).toHaveCount(0);
	await expect(drawer.getByRole('textbox', { name: /^Note for / })).toHaveCount(0);
	await closeComments(drawer);
	await expect(page.getByText('Sending your review…')).toBeVisible();
	release();
	await expect(page.locator('.rb-receipt')).toContainText('Sent back');
});

journey('held comments have the header Send, whole at every size, and the drawer has no footer', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['1440 dark pane closed', 'closed', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review unsent ${mode} ${theme}`);
	const notes = ['Ring two should wait.', 'Add a rollback line.', 'Name the owner of ring three.', 'Link the rollout dashboard.', 'Confirm the rollback owner.', 'Add the incident channel.'];
	const waiting: string[] = [];
	for (const [index, note] of notes.entries()) waiting.push(await comment(request, doc, note, index * 2 + 2));
	for (const id of waiting) await leaveUnsent(request, id);
	await open(page, doc, mode, theme);
	// Details carries the held comments' one Send and Comments names what waits for it; the drawer has no footer.
	await expect(page.locator('[data-comments-entry]')).toHaveAttribute('aria-label', /, 6 waiting for Send$/);
	const send = await shownHeaderSend(page);
	await expect(send.locator('.send-full')).toHaveText('Send 6 comments');
	await expectSenders(page, ['header: Send 6 comments']);
	await settled(page);
	await expect(send).toBeInViewport({ ratio: 1 });
	const fit = await send.evaluate((button) => {
		const label = document.createRange();
		label.selectNodeContents(button);
		const box = button.getBoundingClientRect(), text = label.getBoundingClientRect();
		return {
			clipped: Math.max(text.right - box.right, box.left - text.left, 0),
			right: box.right,
			page: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
		};
	});
	expect(await labelLines(send), 'the label is one line').toBe(1);
	expect(fit.clipped, 'the label is not clipped').toBeLessThanOrEqual(0);
	expect(fit.right, 'the Send is inside the screen').toBeLessThanOrEqual(WIDTH[mode] + 0.5);
	expect(fit.page, 'nothing scrolls sideways').toBe(0);
	expect(await hitAtCentre(send), 'Send is hit-testable').toBe(true);
	if (mode === 'phone') {
		const touchArea = await send.evaluate((button) => {
			const box = button.getBoundingClientRect(), pseudo = getComputedStyle(button, '::after');
			return { box: box.toJSON(), pseudo: { top: pseudo.top, bottom: pseudo.bottom, content: pseudo.content },
				paddingHits: [(box.top + box.bottom) / 2 - 21, (box.top + box.bottom) / 2 + 21].map((y) => button.contains(document.elementFromPoint(box.x + box.width / 2, y))) };
		});
		console.info('Header Send touch area:', JSON.stringify(touchArea));
		expect(touchArea.paddingHits, 'both ends of the touch padding remain inside the header clip').toEqual([true, true]);
		expect(touchArea.box.height, 'native touch target').toBeGreaterThanOrEqual(44);
	}
	await shot(page, mode === 'phone' ? 'rs-12-header-send-phone' : 'rs-11-header-send', info);
	const output = process.env.HELD_COMMENT_DELIVERY_FRAMES_OUT;
	if (output && mode === 'phone') await page.screenshot({ path: path.join(output, `reviewbar-held-send-393-${theme}.png`), scale: 'css' });

	// The drawer lists the threads and ends with the last card: no footer, no second Send.
	await openComments(page, /^6 comments, 6 waiting for Send$/);
	const drawer = page.getByRole('region', { name: 'Document comments' });
	await expectCommentsShown(drawer);
	await expect(drawer.locator('[data-drawer-unsent]')).toHaveCount(0);
	await expect(drawer.getByRole('button', { name: /^Send\b/ })).toHaveCount(0);
	await settled(page);
	if (mode === 'closed') {
		// This canvas has room for the margin column, so the list is that column inside the document canvas, not a sheet over it.
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		const [list, column] = await Promise.all([drawer.boundingBox(), page.locator('.leaf-body.document-canvas').boundingBox()]);
		expect(list!.x, 'the margin column starts inside the document column').toBeGreaterThanOrEqual(column!.x);
		expect(list!.x + list!.width, 'the margin column ends inside the document column').toBeLessThanOrEqual(column!.x + column!.width + 0.5);
	}
	await shot(page, 'drawer-without-footer', info);
	await closeComments(drawer);
	await expect(drawer).toBeHidden();
	// Opening Comments closed Details; open it again to reach the Send.
	await openDetails(page);
	await settled(page);

	if (mode === 'phone') {
		const pointer = await send.evaluateHandle((button) => {
			const seen = { hit: false, pointerType: '' };
			window.addEventListener('pointerdown', (event) => { seen.hit = button.contains(event.target as Node); seen.pointerType = event.pointerType; }, { capture: true, once: true });
			return seen;
		});
		const box = (await send.boundingBox())!;
		await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2 + (theme === 'light' ? -21 : 21));
		const native = await pointer.jsonValue(); await pointer.dispose();
		console.info('Header Send native padding tap:', JSON.stringify({ edge: theme === 'light' ? 'top' : 'bottom', ...native }));
		expect(native, 'native touch reaches the padding outside Send’s former 28px box').toEqual({ hit: true, pointerType: 'touch' });
	} else await send.click();
	await expect(send).toHaveCount(0);
	await expect(page.locator('[data-comments-entry]')).not.toHaveAttribute('aria-label', /waiting for Send/);
	if (output && mode === 'phone') {
		await expect(page.locator('.document-header .doc-panel .sent')).toHaveText('Sent');
		await settled(page);
		await page.screenshot({ path: path.join(output, `reviewbar-held-sent-393-${theme}.png`), scale: 'css' });
	}
});

journey('a long agent name never hides the ask', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review long ${mode} ${theme} Platform Reliability Engineering Operations Release Coordination Agent`);
	const reason = 'Is the rollout plan right for every region before we promote it?';
	raiseAsk(doc.threadId, doc.artifactId, reason);
	await open(page, doc, mode, theme);
	await expectLayout(page, mode);
	// At rest the card shows the question alone, on one line; the asker waits for the card to open.
	await expect(card(page).locator('.rb-dock-q')).toContainText('Is the rollout plan right for every region');
	await expect(card(page).locator('.rb-dock-who')).toBeHidden();
	await shot(page, 'long-name-collapsed', info);

	// Opened, the card grows to give the asker its own line above the whole question, and its answers stay where they were.
	const collapsedHeight = (await card(page).boundingBox())!.height;
	await setCardOpen(page, true);
	await expect(card(page).locator('.rb-dock-q')).toHaveText(reason);
	expect((await card(page).boundingBox())!.height).toBeGreaterThan(collapsedHeight);
	await expectNameClearOfControls(page, doc.title);
	await shot(page, 'long-name-expanded', info);
});

/**
 * A review with 22 comments whose quotes are all gone by the time the person opens it: the seeded quotes are
 * orphaned in the database, and the ask is raised on the thread. None of it is what the case measures.
 */
const orphanHeavyStart: JourneySetup<Doc> = async ({ request, mode, theme }) => {
	const doc = await seed(request, `Review orphan-heavy ${mode} ${theme}`, { sharedAsOf: '2026-09-12' });
	for (let index = 0; index < 22; index += 1) await comment(request, doc, `Unplaced comment ${index + 1}`, 1);
	orphanComments(doc.artifactId);
	raiseAsk(doc.threadId, doc.artifactId, 'Review the long summary, confirm the owner and as-of details, and request any changes needed before launch.');
	return doc;
};

journey('keeps an orphan-heavy review ask compact and offers no Place again once every quote is gone', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['1440 dark pane closed', 'closed', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, value: doc, info, mode, theme }) => {
	await open(page, doc, mode, theme);
	await settled(page);
	const measurements = await page.locator('.doc').evaluate((root) => {
		const firstLine = root.querySelector<HTMLElement>('.cm-line')!.getBoundingClientRect();
		const askCard = root.querySelector<HTMLElement>('[data-ask-card]')!.getBoundingClientRect();
		const question = root.querySelector<HTMLElement>('.rb-dock-q')!.getBoundingClientRect();
		const comments = document.querySelector<HTMLElement>('[data-comments-entry]')!;
		return {
			firstBodyY: firstLine.top,
			cardHeight: askCard.height,
			questionWidth: question.width,
			commentsPillName: comments.getAttribute('aria-label'),
		};
	});
	if (mode === 'phone') {
		// No header rows above the document: the first line sits within the old title-and-owner allowance (240px + one 56px touch row).
		expect(measurements.firstBodyY, 'the first document line sits near the top, with no header rows above it').toBeLessThanOrEqual(240 + 56);
		// The question line and answers stay compact with the approved touch-target spacing.
		expect(measurements.cardHeight, 'the docked review card stays compact').toBeLessThanOrEqual(108);
		for (const button of await card(page).locator('.rb-actions .btn').all()) expect((await button.boundingBox())!.height, 'phone review action touch target').toBeGreaterThanOrEqual(44);
		const leadAsk = card(page).locator('.rb-dock-q');
		await expect(leadAsk).toBeVisible();
		await expect(leadAsk).toHaveText('Review the long summary, confirm the owner and as-of details, and request any changes needed before launch.');
		await expect(card(page).locator('.rb-dock-who'), 'at rest the card shows the question alone').toBeHidden();
		expect(await card(page).locator('.rb-dock-text').evaluate((element) => element.scrollHeight > element.clientHeight + 1 && getComputedStyle(element).webkitLineClamp === '1'),
			'the long ask is one line at rest and ends in an ellipsis').toBe(true);
	}
	await shot(page, `orphan-heavy-reading-${mode}-${theme}`, info);
	if (mode === 'phone') {
		await setCardOpen(page, true);
		await expect(card(page).locator('.rb-dock-q')).toHaveText('Review the long summary, confirm the owner and as-of details, and request any changes needed before launch.');
		const requester = card(page).locator('.rb-dock-who');
		await expect(requester).toBeVisible();
		await expect(requester).toHaveText(doc.title);
		expect(await nameWhole(requester), 'the long ask leaves its requester untruncated').toBe(true);
		await shot(page, `orphan-heavy-expanded-${mode}-${theme}`, info);
		await setCardOpen(page, false);
		// The owner and the status live in Details: the owner and supervisor stay one touch row, and the status keeps its as-of date.
		const panel = await openDetails(page);
		const ownership = page.locator('[data-ownership-row]');
		expect((await ownership.boundingBox())!.height, 'the owner and supervisor row is one touch row').toBeLessThanOrEqual(56);
		const owner = page.locator('[data-ownership-row] [data-role="owner"]');
		await expect(owner).toBeVisible();
		expect(await owner.getAttribute('aria-label'), 'the owner item names the owner in full').toContain(`: ${doc.title}`);
		await expect(panel).toContainText(/Status: as of \w+ \d+/);
		await shot(page, `orphan-heavy-details-${mode}-${theme}`, info);
		await closeDetails(page);
	}
	// Recovery remains exercised through the comments drawer below.
	await openComments(page);
	const drawer = page.getByRole('region', { name: 'Document comments' });
	// Every quote is gone from the document: the cards say so.
	// A margin card says "This text isn’t in the document"; a row in the list or sheet says "Text changed:".
	await expect(drawer.getByText(/^(Text changed:|This text isn’t in the document$)/)).toHaveCount(22);
	await settled(page);
	// Drawer geometry is covered by its focused recovery journey; this flow records the phone layout annotation below.
	await shot(page, `orphan-heavy-${mode}-${theme}`, info);
	test.info().annotations.push({ type: 'orphan-heavy-layout', description: JSON.stringify(measurements) });
}, orphanHeavyStart);

/**
 * The opened card's asker keeps its distinguishing suffix whenever its prefix truncates, never cut into two lines, and its box stays off
 * the card's answers and the bar's Comments and Details.
 */
async function expectNameClearOfControls(page: Page, title: string) {
	const name = card(page).locator('.rb-dock-who');
	await expect(name).toHaveText(title);
	await expect(name).toBeVisible();
	await expect(name.locator('.name'), 'the whole name is one hover away').toHaveAttribute('title', title);
	const boxes = await card(page).evaluate((element) => {
		const rect = (node: Element) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, height: r.height, label: (node.getAttribute('aria-label') ?? node.textContent ?? '').slice(0, 40) }; };
		const who = element.querySelector('.rb-dock-who')!, head = who.querySelector<HTMLElement>('.name-head')!, tail = who.querySelector('.name-tail');
		const line = parseFloat(getComputedStyle(who).lineHeight) || parseFloat(getComputedStyle(who).fontSize) * 1.5;
		// A touch control's 44px hit area is held to what is drawn, as the review answers' rows are.
		const controls = [...element.querySelectorAll('.rb-actions .btn'), ...document.querySelectorAll('[data-comments-entry], [data-document-details]')]
			.map(rect).filter((r) => r.right > r.left);
		return { who: rect(who), tail: tail ? rect(tail) : null, cut: head.scrollWidth > head.clientWidth, line, controls };
	});
	expect(boxes.who.height, `the name is one line: ${JSON.stringify(boxes.who)}`).toBeLessThanOrEqual(boxes.line * 1.5);
	if (boxes.cut) expect(boxes.tail, 'a cut name keeps its suffix').not.toBeNull();
	if (boxes.tail) {
		const tail = boxes.tail, who = boxes.who;
		const inside = tail.right > tail.left && tail.left >= who.left - 0.5 && tail.right <= who.right + 0.5 && tail.top >= who.top - 0.5 && tail.bottom <= who.bottom + 0.5;
		expect(inside, `the distinguishing suffix stays visible within the requester box: ${JSON.stringify(boxes)}`).toBe(true);
	}
	expect(boxes.controls.length).toBeGreaterThan(0);
	for (const control of boxes.controls) {
		const apart = boxes.who.right <= control.left || control.right <= boxes.who.left || boxes.who.bottom <= control.top || control.bottom <= boxes.who.top;
		expect(apart, `the name's box ${JSON.stringify(boxes.who)} intersects a control ${JSON.stringify(control)}`).toBe(true);
	}
	// Reachable once the card is open: on screen, and nothing drawn over it.
	await expect(name).toBeInViewport({ ratio: 1 });
	expect(await hitAtCentre(name), 'the asker is not covered once the card is open').toBe(true);
}


/** At 1280 the open plan pane overlays the canvas instead of docking beside it. The document is then the one visible surface and ends at the pane's edge,
    so the whole header (tools, identity and the ask) sits beside the pane, never under it, and the ask lays itself out for the width that is left. */
for (const theme of ['light', 'dark'] as const) {
	test(`@surface:review-bar the header and its ask stay clear of the overlaid plan pane (1280 ${theme})`, async ({ page, request }, info) => {
		test.setTimeout(120_000);
		test.skip(info.project.name === 'chromium-mobile', 'a desktop layout is proved on the desktop project');
		// A 21-character asker: the title plus the five-character suffix that keeps it unique.
		const doc = await seed(request, `Overlay 1280 ${theme === 'light' ? 'lt' : 'dk'}`, { sharedAsOf: '2026-09-12' });
		expect(doc.title).toHaveLength(21);
		raiseAsk(doc.threadId, doc.artifactId, 'Is the rollout plan right?');
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.addInitScript(({ doc, theme }) => {
			localStorage.setItem('corsia_theme', theme);
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
				tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
			} }, splitWidth: null }));
		}, { doc, theme });
		await loginByUrl(page, { waitForComposer: false });
		await selectThread(page, doc, 'default');
		await expect(bar(page)).toContainText('Is the rollout plan right?');
		// Identity is the owner row in Details, whatever the visible width.
		const identityPanel = await openDetails(page);
		await expect(identityPanel.locator('[data-ownership-row]'), 'the canvas beside the pane keeps document identity in Details').toContainText(doc.title);
		await closeDetails(page);
		const pane = page.locator('.pane');
		await expect(pane).toHaveClass(/\boverlay\b/);
		await expect(pane).not.toHaveClass(/\bcollapsed\b/);
		await settled(page);
		const paneLeft = (await pane.boundingBox())!.x;
		const header = page.locator('.document-header');
		// The split reserves the overlay: the document, its header and its card end at the pane's edge.
		const split = page.locator('.split');
		await expect.poll(async () => { const box = (await header.boundingBox())!; return box.x + box.width; }, 'the header ends at the overlaid pane').toBeLessThanOrEqual(paneLeft + 0.5);

		const pill = page.locator('[data-comments-entry]');
		await expect(pill).toHaveAttribute('aria-label', /^0 comments\b/);
		const details = page.locator('[data-document-details]');
		const approve = bar(page).getByRole('button', { name: 'Approve', exact: true });
		const requestChanges = bar(page).getByRole('button', { name: 'Send back', exact: true });
		for (const control of [pill, details, approve, requestChanges]) {
			await expect(control).toBeVisible();
			const box = (await control.boundingBox())!;
			expect(box.x + box.width, 'clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
			expect(await hitAtCentre(control), `${await control.getAttribute('aria-label') ?? await control.textContent()} not covered`).toBe(true);
		}
		// Request review is in Details, which opens beside the pane too.
		await openDetails(page);
		const requestReview = page.locator('.document-header').getByRole('button', { name: 'Request review', exact: true });
		await expect(requestReview).toBeVisible();
		expect((await requestReview.boundingBox())!.x + (await requestReview.boundingBox())!.width, 'Request review clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(await hitAtCentre(requestReview), 'Request review not covered').toBe(true);
		await closeDetails(page);

		// At rest the card's question is one uncovered line left of the pane; opened, the full name has its own line, whole.
		const question = card(page).locator('.rb-dock-q');
		const atRest = await card(page).evaluate((element) => {
			const text = element.querySelector<HTMLElement>('.rb-dock-text')!;
			return { lines: Math.round(text.getBoundingClientRect().height / parseFloat(getComputedStyle(text).lineHeight)), card: element.getBoundingClientRect().toJSON(), ask: element.querySelector('.rb-dock-q')!.getBoundingClientRect().toJSON() };
		});
		expect(atRest.lines, 'the question is one line at rest').toBe(1);
		expect(atRest.ask.right, 'question clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(atRest.ask.width, 'question retains a visible row').toBeGreaterThan(0);
		expect(atRest.card.right, 'card clear of the overlaid pane').toBeLessThanOrEqual(paneLeft + 0.5);
		expect(await hitAtCentre(question), 'question not covered').toBe(true);
		await setCardOpen(page, true);
		const who = card(page).locator('.rb-dock-who');
		await expect(who).toHaveText(doc.title);
		const geometry = await page.evaluate(() => {
			const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect().toJSON();
			const askCard = document.querySelector<HTMLElement>('[data-ask-card]')!, style = getComputedStyle(askCard);
			const tools = [...document.querySelectorAll('[data-comments-entry], [data-document-details]')].map((el) => el.getBoundingClientRect()).filter((rect) => rect.width > 0);
			return {
				who: box('.rb-dock-who'), ask: box('.rb-dock-q'), contentRight: askCard.getBoundingClientRect().right - parseFloat(style.paddingRight),
				tools: { left: Math.min(...tools.map((rect) => rect.left)), right: Math.max(...tools.map((rect) => rect.right)) },
			};
		});
		await info.attach(`geometry-1280-${theme}`, { body: JSON.stringify(geometry, null, 2), contentType: 'application/json' });
		expect(geometry.tools.right, 'tools clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(geometry.ask.right, 'question clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(await nameWhole(who), 'asker name not clipped').toBe(true);
		expect(geometry.who.right, 'asker name inside the card').toBeLessThanOrEqual(geometry.contentRight + 0.5);
		expect(geometry.who.right, 'name row clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(geometry.ask.top, 'the question starts below the asker').toBeGreaterThanOrEqual(geometry.who.bottom - 0.5);
		expect(await hitAtCentre(who), 'asker name not covered').toBe(true);
		await shot(page, `overlay-1280-${theme}-pane-open`, info);
		await setCardOpen(page, false);

		await openDetails(page);
		await requestReview.click();
		await expect(page.getByRole('combobox', { name: 'Add reviewers' })).toBeVisible();
		await shot(page, `overlay-1280-${theme}-pane-open-request-review`, info);
		await page.keyboard.press('Escape');
		await expect(page.getByRole('combobox', { name: 'Add reviewers' })).toHaveCount(0);
		await closeDetails(page);

		// A real click on each answer: Send back opens the note, and Cancel returns to the ask.
		await requestChanges.click();
		await expect(bar(page).getByRole('textbox')).toBeFocused();
		await bar(page).getByRole('button', { name: 'Cancel', exact: true }).click();
		await expect(approve).toBeVisible();

		// With the pane closed nothing overlays the canvas, so there is no inset. The app bar's Toggle panel button sits under
		// the overlaid pane at this width, so the pane's keyboard shortcut toggles it.
		await page.keyboard.press('ControlOrMeta+Backslash');
		// A collapsed pane is 0px wide, so it is "visible" only while its width transition runs; assert the state, not the animation.
		await expect(page.locator('.pane.collapsed')).toBeAttached();
		await settled(page);
		await expect.poll(() => split.evaluate((el) => getComputedStyle(el).paddingInlineEnd), 'pane closed').toBe('0px');
		expect(await card(page).locator('.rb-dock-text').evaluate((el) => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight))), 'the question is one whole line with the pane closed').toBe(1);
		await shot(page, `overlay-1280-${theme}-pane-closed`, info);
		await page.keyboard.press('ControlOrMeta+Backslash');
		await expect(pane).toHaveClass(/\boverlay\b/);
		await expect(pane).not.toHaveClass(/\bcollapsed\b/);
		await settled(page);

		await approve.click();
		await expect(bar(page).locator('.rb-receipt')).toContainText('Approved');

		// At 1440 and above this seeded desktop canvas can host the minimum split, so the pane docks beside it and nothing is inset.
		for (const width of [1440, 1920]) {
			await page.setViewportSize({ width, height: 900 });
			await expect(pane).not.toHaveClass(/\boverlay\b/);
			await expect.poll(() => split.evaluate((el) => getComputedStyle(el).paddingInlineEnd), `${width} docked`).toBe('0px');
		}
	});
}
