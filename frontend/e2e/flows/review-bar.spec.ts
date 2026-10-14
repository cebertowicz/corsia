import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl } from '../support/corsia';
import { visibleHeaderSends } from '../support/docFeedbackFooter';
import { journey, type Case, uniqueTitle, seed, type Doc, seedImage, comment, type Mode, WIDTH, open, selectThread, shot, hitAtCentre, settled } from '../support/reviewFixtures';

/**
 * The in-view review bar (DCOL-FR12.4) on the real composed canvas: an agent's raised ask on an open
 * document, answered from the reading. The ask is raised through the plan service as the owning agent
 * thread (an agent's `block_node` with `ask_artifact_id`); everything the user does goes through the UI.
 */
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');

function raiseAsk(threadId: string, artifactId: string, reason: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The ask fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'thread, artifact, reason = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'node = service.add_node(plan.id, kind="task", title="Review the proposal", acceptance_criteria="Reviewed", db=db)',
		'db.commit()',
		'mutate(plan.id, "block_node", {"node_id": node.id, "reason_key": "review", "reason": reason, "owner_kind": "user", "owner_id": None,',
		'  "ask_artifact_id": artifact, "expected_revision": service._node_row(db, node.id)["revision"]},',
		'  actor=PlanActor(thread, user, None), provenance="e2e.review_bar", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId, reason], {
		cwd: backendDir, encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

/** Hand the artifact to another agent through the audited ownership path, so a second agent can ask about it too. */
function handOff(artifactId: string, fromThreadId: string, toThreadId: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The hand-off fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys',
		'from infra.database import get_db',
		'from registry.ownership import rehome_artifact',
		'artifact, old_owner, new_owner = sys.argv[1:]',
		'db = get_db()',
		'row = db.execute("SELECT integration, type, external_id FROM artifact_identities WHERE id=?", (artifact,)).fetchone()',
		'rehome_artifact(integration=row["integration"], type=row["type"], external_id=row["external_id"],',
		'  new_owner_thread_id=new_owner, actor_thread_id=old_owner, authority="current-owner-handoff",',
		'  reason="e2e second asker", source_path="e2e.review_bar", db=db)',
		'db.commit()',
	].join('\n'), artifactId, fromThreadId, toThreadId], {
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

const bar = (page: Page) => page.locator('.review-bar');
/** The bar's geometry per surface: one compact row, a stacked bar with compact right-aligned buttons, or 44px touch buttons. */
async function expectLayout(page: Page, mode: Mode) {
	const canvas = () => page.locator('.document-canvas, .image-canvas').first().evaluate((el) => el.getBoundingClientRect().width);
	if (mode === 'closed') {
		// The pane's width transition resizes the canvas, and the bar's one-row layout is a container query on that width:
		// measure only once the transition has finished, never mid-way.
		await page.locator('.pane').evaluate((pane) => Promise.all(pane.getAnimations().map((animation) => animation.finished)));
		await expect.poll(canvas).toBeGreaterThan(600);
	}
	else if (mode === 'default') expect(await canvas(), 'the default layout is a narrow canvas').toBeLessThan(600);
	// A 16ch ask gets a full-width ellipsized row once the 408px review-line budget cannot fit it with the name and tools.
	const line = page.locator('.document-header .review-bar.ask .rb-line');
	if (await line.count()) {
		const header = await page.locator('.document-header').evaluate((el) => {
			const style = getComputedStyle(el);
			return el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
		});
		test.info().annotations.push({ type: 'header-content-width', description: `${mode}: ${header}` });
		const fallback = header < 408;
		expect(await line.evaluate((el) => getComputedStyle(el).display), `review line at a ${header}px header`).toBe(fallback ? 'grid' : 'flex');
		if (fallback) {
			const rows = await line.evaluate((el) => {
				const rect = (selector: string) => el.querySelector<HTMLElement>(selector)!.getBoundingClientRect();
				const who = rect('.rb-who'), ask = rect('.rb-ask'), chevron = rect('.rb-chev');
				const probe = document.createElement('span'); probe.style.cssText = 'position:absolute;visibility:hidden;font:inherit;white-space:pre'; probe.textContent = '0'.repeat(16); el.append(probe);
				const sixteenCh = probe.getBoundingClientRect().width; probe.remove();
				return { who, ask, chevron, sixteenCh };
			});
			expect(rows.chevron.top, 'the chevron stays with the full asker name').toBeLessThanOrEqual(rows.who.bottom + 0.5);
			expect(rows.ask.top, 'the ask gets the row below the asker').toBeGreaterThanOrEqual(Math.max(rows.who.bottom, rows.chevron.bottom) - 0.5);
			expect(rows.ask.width, 'the ask keeps about 16 characters').toBeGreaterThanOrEqual(rows.sixteenCh - 1);
		}
	}
	const box = (await bar(page).boundingBox())!;
	const buttons = await bar(page).locator('.btn').evaluateAll((els) =>
		els.map((el) => { const r = el.getBoundingClientRect(); return { h: r.height, w: r.width, right: r.right }; }));
	expect(buttons.length).toBeGreaterThanOrEqual(2);
	if (mode === 'closed') {
		await expect(bar(page).locator('.rb-narrow')).toBeHidden();
		// The slim integrated ask row reserves its status/action line; it is not the old one-line floating card.
		expect(box.height, 'integrated ask row').toBeLessThanOrEqual(64);
	}
	if (mode === 'phone') {
		for (const button of buttons) expect(button.h, 'touch target').toBeGreaterThanOrEqual(44);
		expect(box.width).toBeLessThanOrEqual(WIDTH.phone);
		return;
	}
	for (const button of buttons) {
		expect(button.h, 'compact header-style button').toBeLessThanOrEqual(32);
		expect(button.w, 'never stretched to half the bar').toBeLessThan(box.width * 0.45);
	}
	expect(box.x + box.width - Math.max(...buttons.map((button) => button.right)), 'right-aligned').toBeLessThanOrEqual(24);
}

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
	await shot(page, 'ask', info);
	await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
	const receipt = bar(page).locator('.rb-receipt');
	await expect(receipt).toContainText('You approved this');
	await expect(receipt).toBeFocused();
	await shot(page, 'receipt', info);
	await page.reload();
	await selectThread(page, doc, mode);
	await expect(bar(page)).toHaveCount(0);
});

journey('two agents ask about the same document', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review two askers ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Review the rollout plan.');
	const second = await createSession(request, uniqueTitle(`Research ${mode} ${theme}`));
	handOff(doc.artifactId, doc.threadId, second);
	raiseAsk(second, doc.artifactId, 'Verify the evidence links.');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toContainText('Verify the evidence links.');
	await expect(bar(page)).toContainText('+1');
	await expectLayout(page, mode);
	await shot(page, 'two-askers', info);
	await bar(page).locator('.rb-line').click();
	await expect(bar(page).locator('.rb-others')).toContainText('Review the rollout plan.');
	await shot(page, 'two-askers-expanded', info);
});

journey('approve with changes after two comments', ALL, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review changes ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Check the ring order.');
	await comment(request, doc, 'Ring two should wait.', 2);
	await comment(request, doc, 'Add a rollback line.', 5);
	await open(page, doc, mode, theme);
	const approve = bar(page).getByRole('button', { name: 'Approve with changes', exact: true });
	await expect(approve).toBeVisible();
	await expectLayout(page, mode);
	await expect(page.getByText('Place again')).toHaveCount(0);
	// One primary on screen: the bar's Approve. Send now waits in the comments drawer's footer; the header shows no Send and its pill marks the unsent pair.
	expect(await visibleHeaderSends(page), 'no Send shows in the header').toEqual([]);
	await expect(page.locator('.document-header').getByRole('button', { name: /^Send\b/ })).toHaveCount(0);
	await expect(page.locator('.comment-status')).toHaveAttribute('aria-label', /, 2 messages not sent yet$/);
	await expect(approve).toHaveClass(/primary/);
	await shot(page, 'ask-with-changes', info);
	await approve.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You approved this with changes');
	await expect(bar(page).locator('.rb-receipt')).toContainText('sent 2 comments');
	await expect(page.locator('.comment-status')).not.toHaveAttribute('aria-label', /not sent yet/);
	await expect(page.getByText('Place again')).toHaveCount(0);
	await shot(page, 'receipt-with-changes', info);
});

journey('a receipt names mixed feedback kinds', [
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review mixed receipt ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Check the handoff.');
	await open(page, doc, mode, theme);
	await page.route('**/api/attention/*/clear', async (route) => {
		await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ review: { verdict: 'approved_with_changes', note: null, feedback_counts: { comments: 2, suggestions: 1, edits: 0 } } }) });
	});
	await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
	const receipt = bar(page).locator('.rb-receipt');
	await expect(receipt).toContainText('sent 2 comments, 1 suggestion');
	expect(await receipt.evaluate((el) => el.textContent?.includes('sent 2 comments, 1 suggestion'))).toBe(true);
	await shot(page, 'receipt-mixed-counts', info);
});

journey('request changes with only a note', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review note ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
	await open(page, doc, mode, theme);
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await expect(note).toBeFocused();
	const send = bar(page).getByRole('button', { name: 'Request changes', exact: true });
	await expect(send).toBeDisabled();
	await note.fill('Not yet: the rollback is missing.');
	await expectLayout(page, mode);
	await shot(page, 'noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
	await shot(page, 'receipt-requested', info);
	const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
});

journey('answer a plain ask on an image', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seedImage(request, `Review image ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this the right diagram?');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toContainText('Is this the right diagram?');
	// An image takes no comments, so Approve is always plain and Request changes always needs a note.
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await expect(bar(page).getByRole('button', { name: 'Approve with changes' })).toHaveCount(0);
	await expectLayout(page, mode);
	await shot(page, 'image-ask', info);
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const send = bar(page).getByRole('button', { name: 'Request changes', exact: true });
	await expect(send).toBeDisabled();
	await bar(page).getByRole('textbox').fill('Use the new palette.');
	await shot(page, 'image-noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
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
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await note.fill('Not yet: the rollback is missing.');
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();

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
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
});

journey('the comments drawer keeps 44px touch targets', [['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review drawer ${mode} ${theme}`);
	await comment(request, doc, 'Ring two should wait.', 2);
	await comment(request, doc, 'Add a rollback line.', 5);
	await open(page, doc, mode, theme);
	// The pill names its threads, then any unsent messages: "2 open threads, 2 messages not sent yet".
	await page.getByRole('button', { name: /^2 open threads\b/ }).click();
	const drawer = page.locator('[data-doc-comments-drawer]');
	await expect(drawer).toBeVisible();
	const close = drawer.getByRole('button', { name: 'Close comments', exact: true });
	const filters = drawer.locator('[data-drawer-filter]');
	const jump = drawer.locator('[data-comment-jump]').first();
	expect(await filters.count()).toBe(1);
	await settled(page);
	const targets = [close, jump, ...(await filters.all())];
	for (const target of targets) expect((await target.boundingBox())!.height, 'touch target').toBeGreaterThanOrEqual(44);
	// A real tap opens the menu, selects a filter, and leaves every drawer control reachable.
	const openFilter = drawer.getByRole('button', { name: 'Filter comments: Open', exact: true });
	const box = (await openFilter.boundingBox())!;
	await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
	await expect(openFilter).toHaveAttribute('aria-expanded', 'true');
	// Close the menu before tapping another drawer control; the menu intentionally owns its overlay.
	await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
	await expect(openFilter).toHaveAttribute('aria-expanded', 'false');
	const jumpBox = (await jump.boundingBox())!;
	await page.touchscreen.tap(jumpBox.x + jumpBox.width / 2, jumpBox.y + jumpBox.height / 2);
	await expect(drawer).toBeVisible();
	// A quote jump moves the phone sheet to its half detent. Wait for that finite
	// transition before calculating a touch coordinate, rather than tapping where
	// the filter was while the sheet was still moving.
	await settled(page);
	await expect(hitAtCentre(filters)).resolves.toBe(true);
	await shot(page, 'rs-09-half-sheet-after-quote-tap', info);
	const reopenedBox = (await filters.boundingBox())!;
	await page.touchscreen.tap(reopenedBox.x + reopenedBox.width / 2, reopenedBox.y + reopenedBox.height / 2);
	await expect(filters).toHaveAttribute('aria-expanded', 'true');
	const menu = drawer.getByRole('menu', { name: 'Filter comments', exact: true });
	await expect(menu.getByRole('menuitemradio')).toHaveCount(3);
	await shot(page, 'rs-10-filter-menu-open', info);
	await menu.getByRole('menuitemradio', { name: 'Resolved', exact: true }).click();
	await expect(filters).toHaveText('Filter');
	await expect(filters).toHaveAttribute('aria-label', 'Filter comments: Resolved');
	await expect(filters).toHaveAttribute('aria-expanded', 'false');
	await shot(page, 'drawer-touch', info);
	const closeBox = (await close.boundingBox())!;
	await page.touchscreen.tap(closeBox.x + closeBox.width / 2, closeBox.y + closeBox.height / 2);
	await expect(drawer).toBeHidden();
});

journey('unsent comments end the comments drawer in a Send now footer', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['1440 dark pane closed', 'closed', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review unsent ${mode} ${theme}`);
	const notes = ['Ring two should wait.', 'Add a rollback line.', 'Name the owner of ring three.', 'Link the rollout dashboard.', 'Confirm the rollback owner.', 'Add the incident channel.'];
	for (const [index, note] of notes.entries()) await comment(request, doc, note, index * 2 + 2);
	await open(page, doc, mode, theme);
	// The header keeps only the pill's unsent dot; Send now is in the drawer.
	await expect(page.locator('.comment-status .unsent-dot')).toBeVisible();
	expect(await visibleHeaderSends(page), 'no Send shows in the header').toEqual([]);
	await page.getByRole('button', { name: /^6 open threads, 6 messages not sent yet$/ }).click();
	const drawer = page.getByRole('region', { name: 'Document comments' });
	const footer = drawer.locator('[data-drawer-unsent]');
	await expect(footer.getByRole('status')).toHaveText(`6 messages not sent yet · they go to ${doc.title} when you leave.`);
	const send = footer.getByRole('button', { name: 'Send now', exact: true });
	await expect(send).toBeVisible();
	await settled(page);
	if (mode === 'closed') {
		const [sheet, column] = await Promise.all([
			page.locator('[data-adaptive-drawer] .drawer-panel').boundingBox(),
			page.locator('.leaf-body.document-canvas').boundingBox(),
		]);
		expect(sheet!.x, 'the narrow-canvas sheet starts at the document column').toBeCloseTo(column!.x, 0);
		expect(sheet!.width, 'the narrow-canvas sheet spans only the document column').toBeCloseTo(column!.width, 0);
	}

	// Pinned to the drawer's foot, opaque, and the list still scrolls above it: its last card ends above the footer.
	const list = drawer.locator('.drawer-comments');
	await list.evaluate((el) => { el.scrollTop = el.scrollHeight; });
	const fit = await footer.evaluate((element) => {
		const frame = element.closest<HTMLElement>('[data-doc-comments-drawer]')!;
		const list = frame.querySelector<HTMLElement>('.drawer-comments')!;
		const cards = [...list.querySelectorAll<HTMLElement>('[data-comment-id]')];
		const probe = document.createElement('div');
		probe.style.background = 'var(--bg-secondary)';
		frame.append(probe);
		const token = getComputedStyle(probe).backgroundColor;
		probe.remove();
		const box = element.getBoundingClientRect(), drawer = frame.getBoundingClientRect();
		return {
			bottomGap: drawer.bottom - box.bottom, inside: box.left >= drawer.left - 0.5 && box.right <= drawer.right + 0.5,
			listOverflow: getComputedStyle(list).overflowY, listBottom: list.getBoundingClientRect().bottom,
			lastCardBottom: Math.min(cards.at(-1)!.getBoundingClientRect().bottom, list.getBoundingClientRect().bottom), footerTop: box.top,
			background: getComputedStyle(element).backgroundColor, token,
			drawerHeight: drawer.height, listHeight: list.getBoundingClientRect().height, footerHeight: box.height,
			docContentHeight: (() => { const doc = frame.closest<HTMLElement>('.doc'); if (!doc) return null; const style = getComputedStyle(doc); return doc.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom); })(),
			measuredFooter: frame.closest<HTMLElement>('.doc')?.style.getPropertyValue('--doc-drawer-footer-height') ?? '',
		};
	});
	expect(fit.bottomGap, 'the footer sits on the drawer\'s bottom border').toBeLessThanOrEqual(1.5);
	expect(fit.inside, 'the footer stays inside the drawer').toBe(true);
	expect(fit.listOverflow, 'the thread list scrolls').toBe('auto');
	expect(fit.listBottom, 'the list ends where the footer starts').toBeLessThanOrEqual(fit.footerTop + 0.5);
	expect(fit.lastCardBottom, 'scrolled to the end, the last card is above the footer').toBeLessThanOrEqual(fit.footerTop + 0.5);
	expect(fit.background, 'the footer is opaque --bg-secondary').toBe(fit.token);
	await shot(page, mode === 'phone' ? 'rs-12-send-now-sheet' : 'rs-11-send-now-inline', info);
	// The inline drawer extends its grid cap by the measured footer. The phone sheet is portaled outside that grid.
	if (fit.docContentHeight !== null) {
		const drawerCap = fit.docContentHeight * 0.36;
		const preFooterDrawer = Math.min(drawerCap, 260);
		const expectedDrawer = preFooterDrawer + fit.footerHeight;
		const drawerChrome = fit.drawerHeight - fit.listHeight - fit.footerHeight;
		expect(Number.parseFloat(fit.measuredFooter), 'the grid receives the measured footer height').toBeCloseTo(fit.footerHeight, 0);
		expect(fit.drawerHeight, 'the drawer adds the footer beneath its existing cap').toBeCloseTo(expectedDrawer, 0);
		expect(fit.listHeight, 'six-thread list keeps its pre-footer height').toBeGreaterThanOrEqual(preFooterDrawer - drawerChrome - 1);
	}
	expect(await hitAtCentre(send), 'Send now is hit-testable').toBe(true);
	if (mode === 'phone') expect((await send.boundingBox())!.height, 'touch target').toBeGreaterThanOrEqual(44);
	await shot(page, 'unsent-footer', info);

	await send.click();
	await expect(footer).toHaveCount(0);
	await expect(page.locator('.comment-status')).not.toHaveAttribute('aria-label', /not sent yet/);
});

journey('a long agent name never hides the ask', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review long ${mode} ${theme} Platform Reliability Engineering Operations Release Coordination Agent`);
	const reason = 'Is the rollout plan right for every region before we promote it?';
	raiseAsk(doc.threadId, doc.artifactId, reason);
	await open(page, doc, mode, theme);
	await expectLayout(page, mode);
	const line = bar(page).locator('.rb-line');
	await expect(line).toContainText('Is the rollout plan right for every region');
	await expectNameClearOfControls(page, doc.title);
	await shot(page, 'long-name-collapsed', info);

	// The integrated row expands its full ask below the tool line, keeping the header controls in place.
	const collapsedHeight = (await bar(page).boundingBox())!.height;
	await line.click();
	await expect(bar(page).locator('.rb-ask-full')).toHaveText(reason);
	expect((await bar(page).boundingBox())!.height).toBeGreaterThan(collapsedHeight);
	await expectNameClearOfControls(page, doc.title);
	await shot(page, 'long-name-expanded', info);
});

journey('keeps an orphan-heavy review ask compact without losing recovery', [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['1440 dark pane closed', 'closed', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review orphan-heavy ${mode} ${theme}`, { sharedAsOf: '2026-09-12' });
	for (let index = 0; index < 22; index += 1) await comment(request, doc, `Unplaced comment ${index + 1}`, 1);
	orphanComments(doc.artifactId);
	raiseAsk(doc.threadId, doc.artifactId, 'Review the long summary, confirm the owner and as-of details, and request any changes needed before launch.');
	await open(page, doc, mode, theme);
	await settled(page);
	const measurements = await page.locator('.doc').evaluate((root) => {
		const firstLine = root.querySelector<HTMLElement>('.cm-line')!.getBoundingClientRect();
		const capsule = root.querySelector<HTMLElement>('.rb-capsule')!.getBoundingClientRect();
		return { firstBodyY: firstLine.top, capsuleHeight: capsule.height };
	});
	if (mode === 'phone') {
		expect(measurements.firstBodyY, 'the first document line stays in the phone reading area').toBeLessThanOrEqual(240);
		expect(measurements.capsuleHeight, 'the fixed review action bar stays compact').toBeLessThanOrEqual(64);
		for (const button of await bar(page).locator('.rb-capsule .btn').all()) expect((await button.boundingBox())!.height, 'phone review action touch target').toBeGreaterThanOrEqual(44);
		expect(bar(page).locator('.rb-ask')).toBeHidden();
	}
	await shot(page, `orphan-heavy-reading-${mode}-${theme}`, info);
	if (mode === 'phone') {
		await bar(page).locator('.rb-line').click();
		await expect(bar(page).locator('.rb-ask-full')).toHaveText('Review the long summary, confirm the owner and as-of details, and request any changes needed before launch.');
		await expect(bar(page).locator('.rb-who')).toBeVisible();
		await expect(page.locator('.identity-age')).toContainText(`Owner: ${doc.title}`);
		await expect(page.locator('.identity-age')).toContainText(/as of \w+ \d+/);
		await shot(page, `orphan-heavy-expanded-${mode}-${theme}`, info);
	}
	expect(page.locator('.document-header [aria-label="Reload comment placements"]')).toHaveCount(0);
	const comments = page.locator('.comment-status');
	await comments.click();
	const drawer = page.getByRole('region', { name: 'Document comments' });
	const recovery = drawer.locator('[data-drawer-placement]');
	await expect(recovery.getByRole('status')).toHaveText("22 comments couldn't be placed");
	await expect(recovery.getByRole('button', { name: 'Reload comment placements' })).toBeVisible();
	await settled(page);
	await shot(page, `orphan-heavy-${mode}-${theme}`, info);
	test.info().annotations.push({ type: 'orphan-heavy-layout', description: JSON.stringify(measurements) });
});

/** The asker's name is cut in the middle with its last four characters kept, and its box stays off the header's controls. */
async function expectNameClearOfControls(page: Page, title: string) {
	const name = bar(page).locator('.rb-who .name');
	await expect(name.locator('.name-tail')).toHaveText(title.slice(-4));
	expect(await name.locator('.name-head').evaluate((el) => el.scrollWidth > el.clientWidth), 'the name is truncated, not wrapped or clipped').toBe(true);
	const boxes = await page.evaluate(() => {
		const rect = (el: Element) => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, label: (el.getAttribute('aria-label') ?? el.className ?? '').toString().slice(0, 40) }; };
		return {
			name: rect(document.querySelector('.rb-who')!),
			// A touch control's 44px hit area spills a few px into the row below; the name is held clear of what is drawn (the 28px control).
			controls: [...document.querySelectorAll('.document-header .presence-row, .document-tools button, .rb-chev')].map(rect).filter((r) => r.right > r.left).map((r) => { const mid = (r.top + r.bottom) / 2; return r.bottom - r.top > 28 ? { ...r, top: mid - 14, bottom: mid + 14 } : r; }),
		};
	});
	expect(boxes.controls.length).toBeGreaterThan(0);
	for (const control of boxes.controls) {
		const apart = boxes.name.right <= control.left || control.right <= boxes.name.left || boxes.name.bottom <= control.top || control.bottom <= boxes.name.top;
		expect(apart, `the name's box ${JSON.stringify(boxes.name)} intersects a header control ${JSON.stringify(control)}`).toBe(true);
	}
}


/** At 1280 the open plan pane overlays the document instead of docking beside it, so the whole header (tools and both ask lines) ends before it rather than under it. */
for (const theme of ['light', 'dark'] as const) {
	test(`@surface:review-bar the header and its ask stay clear of the overlaid plan pane (1280 ${theme})`, async ({ page, request }, info) => {
		test.skip(info.project.name === 'chromium-mobile', 'a desktop layout is proved on the desktop project');
		// A 21-character asker: the title plus the five-character suffix that keeps it unique.
		const doc = await seed(request, `Overlay 1280 ${theme === 'light' ? 'lt' : 'dk'}`);
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
		const pane = page.locator('.pane');
		await expect(pane).toHaveClass(/\boverlay\b/);
		await expect(pane).not.toHaveClass(/\bcollapsed\b/);
		await settled(page);
		const paneLeft = (await pane.boundingBox())!.x;
		const header = page.locator('.document-header');
		await expect.poll(() => header.evaluate((el) => getComputedStyle(el).paddingInlineEnd), 'overlay inset').not.toBe('0px');

		const tools = page.locator('.document-tools');
		const pill = tools.getByRole('button', { name: /^0 open threads\b/ });
		const requestReview = tools.getByRole('button', { name: 'Request review', exact: true });
		const approve = bar(page).getByRole('button', { name: 'Approve', exact: true });
		const requestChanges = bar(page).getByRole('button', { name: 'Request changes', exact: true });
		for (const control of [pill, requestReview, approve, requestChanges]) {
			await expect(control).toBeVisible();
			const box = (await control.boundingBox())!;
			expect(box.x + box.width, 'clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
			expect(await hitAtCentre(control), `${await control.getAttribute('aria-label') ?? await control.textContent()} not covered`).toBe(true);
		}

		// The squeezed header keeps the full name and chevron together, then gives the question its own full-width row.
		const who = bar(page).locator('.rb-who');
		const question = bar(page).locator('.rb-ask');
		await expect(who).toHaveText(`${doc.title}:`);
		const geometry = await page.evaluate(() => {
			const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect();
			const header = document.querySelector('.document-header')!, style = getComputedStyle(header);
			const who = document.querySelector('.rb-who')!;
			const tools = [...document.querySelectorAll('.document-header .presence-row, .document-tools')]
				.map((el) => el.getBoundingClientRect()).filter((rect) => rect.width > 0);
			return {
				header: header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
				line: getComputedStyle(document.querySelector('.rb-line')!).display,
				avatar: box('.rb-lead > :first-child'), who: box('.rb-who'), whoScroll: who.scrollWidth, whoClient: who.clientWidth,
				lead: box('.rb-lead'), ask: box('.rb-ask'), chevron: box('.rb-chev'), pill: box('.document-tools .comment-status'),
				tools: { left: Math.min(...tools.map((rect) => rect.left)), right: Math.max(...tools.map((rect) => rect.right)), bottom: Math.max(...tools.map((rect) => rect.bottom)) },
			};
		});
		await info.attach(`geometry-1280-${theme}`, { body: JSON.stringify(geometry, null, 2), contentType: 'application/json' });
		expect(geometry.line, 'review line rearranged for the squeezed header').toBe('grid');
		expect(geometry.lead.top, 'name row follows the tools').toBeGreaterThanOrEqual(geometry.tools.bottom);
		expect(geometry.chevron.top, 'chevron stays beside the asker name').toBeLessThanOrEqual(geometry.who.bottom);
		expect(geometry.ask.top, 'question occupies the full row below the name').toBeGreaterThanOrEqual(Math.max(geometry.who.bottom, geometry.chevron.bottom) - 0.5);
		expect(geometry.whoScroll, 'asker name not clipped').toBeLessThanOrEqual(geometry.whoClient);
		expect(geometry.who.right, 'asker name inside its row').toBeLessThanOrEqual(geometry.lead.right);
		expect(geometry.lead.right, 'name row clear of the overlaid pane').toBeLessThanOrEqual(paneLeft);
		expect(await hitAtCentre(who), 'asker name not covered').toBe(true);
		expect(await hitAtCentre(bar(page).locator('.rb-chev')), 'chevron not covered').toBe(true);
		expect(geometry.ask.width, 'question retains a visible full-width row').toBeGreaterThan(0);
		expect(await hitAtCentre(question), 'question not covered').toBe(true);
		await shot(page, `overlay-1280-${theme}-pane-open`, info);

		await requestReview.click();
		await expect(page.getByRole('combobox', { name: 'Add reviewers' })).toBeVisible();
		await shot(page, `overlay-1280-${theme}-pane-open-request-review`, info);
		await page.keyboard.press('Escape');
		await expect(page.getByRole('combobox', { name: 'Add reviewers' })).toHaveCount(0);

		// A real click on each answer: Request changes opens the note, and Cancel returns to the ask.
		await requestChanges.click();
		await expect(bar(page).getByRole('textbox')).toBeFocused();
		await bar(page).getByRole('button', { name: 'Cancel', exact: true }).click();
		await expect(approve).toBeVisible();

		// With the pane closed nothing overlays the canvas, so there is no inset. The header's Toggle panel button sits under
		// the overlaid pane at this width, so the pane's keyboard shortcut toggles it.
		await page.keyboard.press('ControlOrMeta+Backslash');
		await expect(page.locator('.pane.collapsed')).toBeVisible();
		await settled(page);
		await expect.poll(() => header.evaluate((el) => getComputedStyle(el).paddingInlineEnd), 'pane closed').toBe('0px');
		expect(await bar(page).locator('.rb-line').evaluate((el) => getComputedStyle(el).display), 'line 1 whole with the pane closed').toBe('flex');
		await shot(page, `overlay-1280-${theme}-pane-closed`, info);
		await page.keyboard.press('ControlOrMeta+Backslash');
		await expect(pane).toHaveClass(/\boverlay\b/);
		await expect(pane).not.toHaveClass(/\bcollapsed\b/);
		await settled(page);

		await approve.click();
		await expect(bar(page).locator('.rb-receipt')).toContainText('You approved this');

		// Where the pane docks beside the canvas, the header keeps its place: no inset at all.
		for (const width of [1440, 1920]) {
			await page.setViewportSize({ width, height: 900 });
			await expect(pane).not.toHaveClass(/\boverlay\b/);
			await expect.poll(() => header.evaluate((el) => getComputedStyle(el).paddingInlineEnd), `${width} docked`).toBe('0px');
		}
	});
}
