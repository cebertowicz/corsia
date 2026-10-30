import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { deleteRenderedLine, openLiveDocument, renderedCount, seedLiveDocument, serverText, type LiveDocument } from '../support/liveDocument';
import { armLiveFault, beginLiveFaultCase, liveFaultEvidence, liveFaultsEnabled, liveFaultStatus, observeFaultTraffic, prepareHeldDocument, refuseLiveFaultOnce, waitForLiveFaultRestart, type FaultTraffic } from '../support/liveFault';

const viewports = [
	['1440', { width: 1440, height: 1000 }],
	['393', { width: 393, height: 852 }],
] as const;
const edits = ['insert', 'delete'] as const;
const choices = ['corsia', 'file'] as const;
const heldMessage = 'This file changed outside CORSIA while a write was being saved.';

async function confirmChoice(page: Page, choice: typeof choices[number]): Promise<void> {
	const label = choice === 'corsia' ? 'Keep CORSIA text, overwrite the file' : "Use the file's text, replace CORSIA text";
	const choiceButton = page.getByRole('button', { name: label, exact: true });
	if (!await choiceButton.isVisible()) await page.getByRole('button', { name: 'Details', exact: true }).click();
	await expect(choiceButton).toBeVisible();
	await choiceButton.click();
	await page.getByRole('alertdialog').getByRole('button', { name: label, exact: true }).click();
	// The old confirmation fades out; a retry must not click its departing button.
	await expect(page.getByRole('alertdialog')).toBeHidden();
}

async function receiveUncommitted(page: Page, request: APIRequestContext, document: LiveDocument, edit: typeof edits[number], stamp: string, traffic: FaultTraffic): Promise<{ ids: string[]; storeId: string; expected: string }> {
	await refuseLiveFaultOnce(request, document.artifactId, 'commit');
	if (edit === 'insert') {
		await page.locator('.cm-content').click();
		await page.keyboard.press('ControlOrMeta+End');
		// One native input operation produces one immutable browser update.
		await page.keyboard.insertText(`received ${stamp}`);
	} else {
		await deleteRenderedLine(page, `remove ${stamp}`);
	}
	await expect.poll(async () => (await liveFaultStatus(request, document.artifactId)).received_ids.length).toBeGreaterThan(0);
	const evidence = await liveFaultEvidence(request, document.artifactId);
	const expected = edit === 'insert' ? `${document.body}received ${stamp}` : `# Held ${stamp}\n`;
	expect(evidence.live_text).toBe(expected);
	expect(evidence.durable_text).toBe(document.body);
	expect(evidence.pending).toBe('held');
	for (const id of evidence.received_ids) {
		expect(traffic.sent(evidence.store_id, id)).toBe(1);
		expect(traffic.acknowledged(evidence.store_id, id), 'Refused commit must not ACK').toBe(0);
	}
	return { ids: evidence.received_ids, storeId: evidence.store_id, expected };
}

async function expectConverged(page: Page, request: APIRequestContext, document: LiveDocument, expected: string): Promise<void> {
	await expect.poll(async () => {
		const state = await liveFaultEvidence(request, document.artifactId);
		return state.pending === null && state.received_ids.length === 0
			&& [state.live_text, state.durable_text, state.mirror_text, state.file_text].every(text => text === expected)
			&& state.live_state_digest === state.durable_state_digest && state.durable_state_digest === state.mirror_state_digest;
	}, { timeout: 30_000, message: 'Live CRDT, durable CRDT, mirror and file bytes must converge exactly' }).toBe(true);
	expect(await serverText(request, document)).toBe(expected);
	await expect(page.getByText(heldMessage)).toBeHidden();
	for (const line of expected.split('\n').filter(Boolean)) {
		await expect(page.locator('.cm-content')).toContainText(line.replace(/^#{1,6}\s+/, ''));
	}
}

for (const [width, viewport] of viewports) {
	for (const edit of edits) {
		for (const choice of choices) {
			// File choice has one atomic apply transaction: no separate record, rename or clear.
			const scenarios = choice === 'file'
				? ['settle', 'refuse:apply', 'kill:choice_apply']
				: ['settle', 'refuse:record', 'refuse:rename', 'refuse:clear', 'kill:choice_record', 'kill:choice_rename', 'kill:choice_apply', 'kill:choice_clear'];
			for (const scenario of scenarios) {
				test(`@case-21 @surface:live-document ${width} held ${edit} ${choice} ${scenario} converges exactly after received-uncommitted edit`, async ({ browser, request }) => {
					test.skip(!liveFaultsEnabled(), 'Requires mock live-fault mode with freshly prepared roots.');
					test.setTimeout(120_000);
					const stamp = `${width}-${edit}-${choice}-${scenario.replace(':', '-')}-${Date.now()}`;
					const body = `# Held ${stamp}\nremove ${stamp}\n`;
					const outside = `# Outside ${stamp}\nremove ${stamp}\n`;
					const document = await seedLiveDocument(request, `held-${stamp}`, body);
					const context = await browser.newContext({ viewport });
					try {
						const traffic = await observeFaultTraffic(context);
						const page = await context.newPage();
						const frames = await openLiveDocument(page, document);
						const initial = await liveFaultStatus(request, document.artifactId);
						await expect.poll(() => traffic.syncIds(initial.store_id).length).toBe(1);
						await expect.poll(() => traffic.acknowledged(initial.store_id, traffic.syncIds(initial.store_id)[0])).toBe(1);
						await prepareHeldDocument(request, document.artifactId, outside);
						await expect(page.getByText(heldMessage)).toBeVisible();
						const received = await receiveUncommitted(page, request, document, edit, stamp, traffic);
						const expected = choice === 'file' ? outside : received.expected;
						let resentSyncs = () => [] as string[];
						if (scenario.startsWith('refuse:')) {
							await refuseLiveFaultOnce(request, document.artifactId, scenario.slice(7) as 'record' | 'apply' | 'rename' | 'clear');
							await confirmChoice(page, choice);
							await expect(page.getByText(heldMessage)).toBeVisible();
							const refused = await liveFaultEvidence(request, document.artifactId);
							expect(refused.pending).toBe('held');
							expect(refused.file_text).toBe(outside);
							expect(refused.live_text).toBe(received.expected);
							await expect.poll(async () => (await liveFaultStatus(request, document.artifactId)).choice).toBeNull();
							await confirmChoice(page, choice);
						} else if (scenario.startsWith('kill:')) {
							await beginLiveFaultCase(request);
							const cut = scenario.slice(5);
							// Clearing is reachable only after a rename refusal.
							if (cut === 'choice_clear') await refuseLiveFaultOnce(request, document.artifactId, 'rename');
							const armed = await armLiveFault(request, document.artifactId, cut);
							traffic.holdCurrentSocketAcks();
							const openedBefore = frames.opened.get(document.artifactId)!;
							const previousOpenedSyncs = new Set(traffic.openedSyncIds(document.artifactId, received.storeId));
							await confirmChoice(page, choice);
							await waitForLiveFaultRestart(request, armed);
							await expect.poll(() => frames.opened.get(document.artifactId) ?? 0, { timeout: 30_000 }).toBeGreaterThan(openedBefore);
							// Reopen sends the first CRDT delta (including deletion set) after its matching doc_opened.
							resentSyncs = () => traffic.openedSyncIds(document.artifactId, received.storeId).filter(id => !previousOpenedSyncs.has(id));
							await expect.poll(() => resentSyncs().length).toBe(1);
							await expect.poll(() => traffic.sent(received.storeId, resentSyncs()[0])).toBe(1);
							await expect.poll(() => traffic.acknowledged(received.storeId, resentSyncs()[0])).toBe(1);
							const restarted = await liveFaultStatus(request, document.artifactId);
							expect(restarted.store_id).toBe(received.storeId);
							// Cuts before file settlement preserve the held barrier and need a fresh choice.
							if (restarted.pending === 'held') await confirmChoice(page, choice);
						} else {
							await confirmChoice(page, choice);
						}
						await expectConverged(page, request, document, expected);
						if (scenario.startsWith('kill:')) {
							expect(resentSyncs()).toHaveLength(1);
							expect(traffic.sent(received.storeId, resentSyncs()[0])).toBe(1);
							expect(traffic.acknowledged(received.storeId, resentSyncs()[0])).toBe(1);
						}
						for (const id of received.ids) {
							expect(traffic.sent(received.storeId, id)).toBe(1);
							expect(traffic.acknowledged(received.storeId, id)).toBe(scenario.startsWith('kill:') ? 0 : 1);
						}
						if (edit === 'insert') expect(await renderedCount(page, `received ${stamp}`)).toBe(choice === 'corsia' ? 1 : 0);
						else expect(await renderedCount(page, `remove ${stamp}`)).toBe(choice === 'corsia' ? 0 : 1);
						// A true reopen must reconstruct the same item identities and exact bytes.
						const settledState = (await liveFaultEvidence(request, document.artifactId)).durable_state_digest;
						await page.reload();
						// Reload returns to the thread list; reopen the owning thread's saved canvas.
						if (width === '393') await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
						await page.getByText(document.title, { exact: true }).first().click();
						await expect(page.locator('.cm-content')).toBeVisible();
						await expectConverged(page, request, document, expected);
						expect((await liveFaultEvidence(request, document.artifactId)).durable_state_digest).toBe(settledState);
					} finally {
						await context.close();
					}
				});
			}
		}
	}
}
