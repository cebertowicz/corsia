import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test, expect } from '../support/test';
import { chatComposer, createSession, openAppPastLanding, sendButton, sidebarThread, waitForWsConnected, saveEvidenceScreenshot } from '../support/corsia';
import { seedMockResponses } from '../support/mock';

test.use({ seedPerTest: true, viewport: { width: 1440, height: 900 } });

/** Calls only the disposable instance's real persistence/read owners, never a live install. */
function isolated(script: string, thread: string): string {
	const { CORSIA_E2E_STATE_DIR: state, CORSIA_E2E_WORKSPACE: workspace, CORSIA_E2E_BACKEND_PYTHON: python } = process.env;
	if (!state || !workspace || !python) throw new Error('isolated runner roots required');
	return execFileSync(python, ['-c', script, thread], {
		cwd: path.resolve(process.cwd(), '../backend'),
		env: { ...process.env, CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: workspace, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
		encoding: 'utf8',
	}).trim();
}

function seedUpdates(thread: string): string {
	return isolated(`
import sys
from tools.post_update import _persist_update
from infra.database import get_db
thread = sys.argv[1]
db=get_db()
db.execute("INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch) VALUES ('fixture',?,0,'mock/test-model','completed','2026-11-14T00:00:00','test')", (thread,))
from infra.queries import insert_message
human=insert_message(thread, 'user', 'Keep me informed about the delivery and appointment.', db=db)
db.execute("UPDATE messages SET active_turn_id='fixture',input_order=1 WHERE id=?", (human,))
db.execute("UPDATE threads SET next_input_order=2 WHERE id=?", (thread,))
db.commit(); db.close()
old = _persist_update(thread, 'The delivery arrives Monday.', None, None, 'fixture')[0]
_persist_update(thread, 'The appointment remains at noon.', None, None, 'fixture')
print(old)
`, thread);
}

test('replaces an unseen update on two open devices, then refuses after a visit', async ({ page, browser, request }, testInfo) => {
	test.setTimeout(120_000);
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'disposable instance required');
	const thread = await createSession(request, 'Replacement surface');
	const old = seedUpdates(thread);
	const secondContext = await browser.newContext({ baseURL: testInfo.project.use.baseURL as string, viewport: { width: 393, height: 852 } });
	const second = await secondContext.newPage();
	try {
		// Hold the existing three-second presence grace deterministically; no product timing change.
		for (const device of [page, second]) {
			const now = new Date();
			await device.clock.install({ time: now }); await device.clock.pauseAt(now);
			await openAppPastLanding(device);
			if ((device.viewportSize()?.width ?? 0) <= 768) await device.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(device, 'Replacement surface').click();
			await waitForWsConnected(device);
			await expect(device.locator(`[data-message-id="${old}"]`)).toBeVisible();
		}
		await saveEvidenceScreenshot(page, testInfo, 'before-desktop');
		await saveEvidenceScreenshot(second, testInfo, 'before-phone');
		await seedMockResponses(request, [
			{ content: '', tool_calls: [{ id: 'replace-now', name: 'post_update', arguments: JSON.stringify({ text: 'The delivery arrives Tuesday.', replace: [old], replace_reason: 'corrects', idempotency_key: 'surface-once' }) }], finish_reason: 'tool_calls' },
			{ content: 'The delivery check is complete.' },
		]);
		await chatComposer(page).fill('Check the delivery date.'); await sendButton(page).click();
		for (const device of [page, second]) {
			await expect(device.locator('.messages')).toContainText('The delivery arrives Tuesday.', { timeout: 30_000 });
			await expect(device.locator(`[data-message-id="${old}"]`)).toHaveCount(0);
			await expect(device.locator('.messages')).not.toContainText('The delivery arrives Monday.');
		}
		const saved = JSON.parse(isolated(`
import sys,json
from infra.database import get_db
rows=get_db().execute("SELECT message_id,result_text FROM message_update_state WHERE thread_id=? AND replaced_by IS NULL ORDER BY posted_at DESC", (sys.argv[1],)).fetchall()
print(json.dumps(dict(next(r for r in rows if r['result_text'] and 'Replaced: none.' not in r['result_text']))))
`, thread));
		expect(saved.result_text).toContain('Other unseen updates: 1.');
		expect(saved.result_text).toContain('The appointment remains at noon.');
		await saveEvidenceScreenshot(page, testInfo, 'after-desktop');
		await saveEvidenceScreenshot(second, testInfo, 'after-phone');
		// Resume the actual presence timer, then prove the real tool refuses the now-seen update.
		await page.clock.resume(); await second.clock.resume();
		await expect.poll(() => isolated(`
import sys
from infra.database import get_db
print(get_db().execute("SELECT seen_reason FROM message_update_state WHERE message_id=?", (sys.argv[1],)).fetchone()[0])
`, saved.message_id)).toBe('visit');
		await seedMockResponses(request, [
			{ content: '', tool_calls: [{ id: 'replace-seen', name: 'post_update', arguments: JSON.stringify({ text: 'The delivery arrives Wednesday.', replace: [saved.message_id], replace_reason: 'updates' }) }], finish_reason: 'tool_calls' },
			{ content: 'The second check is complete.' },
		]);
		await chatComposer(page).fill('Check again.'); await sendButton(page).click();
		await expect.poll(() => isolated(`
import sys
from infra.database import get_db
row=get_db().execute("SELECT content FROM messages WHERE thread_id=? AND role='tool' ORDER BY seq DESC LIMIT 1", (sys.argv[1],)).fetchone()
print(row[0] if row else '')
`, thread)).toContain('was seen at');
		await expect(page.locator('.messages')).toContainText('The delivery arrives Tuesday.');
		await expect(page.locator('.messages')).not.toContainText('The delivery arrives Wednesday.');
	} finally { await secondContext.close(); }
});

test('keeps an open draft and recovers a replacement missed while offline', async ({ page, browser, request }, testInfo) => {
	test.setTimeout(120_000);
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'disposable instance required');
	const thread = await createSession(request, 'Draft and reconnect');
	const old = seedUpdates(thread);
	const sender = await page.context().newPage();
	const offlineContext = await browser.newContext({ baseURL: testInfo.project.use.baseURL as string });
	const offline = await offlineContext.newPage();
	try {
		for (const device of [page, sender, offline]) {
			const now = new Date(); await device.clock.install({ time: now }); await device.clock.pauseAt(now);
			await openAppPastLanding(device); await sidebarThread(device, 'Draft and reconnect').click();
			await waitForWsConnected(device); await expect(device.locator(`[data-message-id="${old}"]`)).toBeVisible();
		}
		await page.locator(`[data-message-id="${old}"] .long-message-body`).evaluate(body => {
			const range = document.createRange(); range.selectNodeContents(body);
			const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
			document.dispatchEvent(new Event('selectionchange'));
		});
		await page.locator('.sel-comment').click();
		const draft = page.getByPlaceholder('Comment…');
		await draft.fill('Keep this unsent thought');
		await offlineContext.setOffline(true);
		await seedMockResponses(request, [
			{ content: '', tool_calls: [{ id: 'replace-offline', name: 'post_update', arguments: JSON.stringify({ text: 'The delivery arrives Tuesday.', replace: [old], replace_reason: 'corrects' }) }], finish_reason: 'tool_calls' },
			{ content: 'The delivery check is complete.' },
		]);
		await chatComposer(sender).fill('Check the delivery date.'); await sendButton(sender).click();
		await expect(page.locator(`[data-message-id="${old}"]`)).toHaveCount(0);
		await expect(draft).toHaveValue('Keep this unsent thought');
		await draft.press('Control+Enter');
		await expect(page.getByRole('alert')).toHaveText('The text this comment quotes isn’t in the chat, so this comment couldn’t be sent. Your text stays here to copy.');
		await expect(draft).toHaveValue('Keep this unsent thought'); await expect(draft).toBeEditable();
		await saveEvidenceScreenshot(page, testInfo, 'draft-preserved');
		await expect(offline.locator(`[data-message-id="${old}"]`)).toBeVisible();
		await offlineContext.setOffline(false); await offline.clock.resume();
		await waitForWsConnected(offline);
		await expect(offline.locator(`[data-message-id="${old}"]`)).toHaveCount(0);
		await expect(offline.locator('.messages')).toContainText('The delivery arrives Tuesday.');
		await saveEvidenceScreenshot(offline, testInfo, 'reconnected');
	} finally { await sender.close(); await offlineContext.close(); }
});
