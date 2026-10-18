import { expect, test } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { seedOutcomes, readStates, expectObservedNoUpdate, openOutcomeThread, focusSetting, expandLoadedWork, assertPassiveGeometry, appendClosure, assertOutcomeOrder, assertOutcomeSidebar } from '../support/focusOutcomeProof';
import { setFocusMode } from '../support/focusMode';

for (const width of [1280, 393]) for (const theme of ['dark', 'light']) {
	test.describe(`outcomes ${width} ${theme}`, () => {
		test.use({ viewport: { width, height: 1000 }, hasTouch: width === 393, timezoneId: 'America/Los_Angeles' });
		// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
		test.afterEach(async ({ request }) => {
			await setFocusMode(request, true);
		});
		test(`@surface:focus-outcomes passive composition, query, touch and keyboard ${width} ${theme}`, async ({ page, request }, info) => {
			test.setTimeout(120_000);
			let nudged = false;
			page.on('websocket', socket => socket.on('framereceived', frame => {
				try { if (JSON.parse(String(frame.payload)).type === 'plans_changed') nudged = true; } catch { /* non-JSON frames */ }
			}));
			await page.addInitScript(value => localStorage.setItem('corsia_theme', value), theme);
			const proof = await seedOutcomes(request, `${width}-${theme}-${Date.now()}`);
			const [main, passive, failed] = proof.ids;
			const before = await readStates(request, proof.ids);
			expect(before[passive].latest_update.summary).toBe('Closed: Passive sidebar snapshot');
			expect(before[passive].has_update).toBe(false);
			expect(before[main].latest_update.summary).toBe('Completed: Latest completed snapshot');
			expect(before[main].has_update).toBe(true);
			expect(before[failed].attention_items.map(item => item.title)).toEqual(['Failure assigned to user']);
			await assertOutcomeSidebar(page, proof.titles);
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-sidebar`, { fullPage: false });
			if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await focusSetting(page, true);
			await openOutcomeThread(page, proof.titles[0]);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await expect(page.getByText('Meaningful decision update', { exact: true })).toBeVisible();
			await expect(page.locator('.passive-toggle[aria-expanded="true"]')).toHaveCount(0);
			await expandLoadedWork(page);
			await expect(page.locator('.passive-outcomes')).toHaveCount(4);
			await expect(page.locator('.plan-limit-paused')).toHaveCount(1);
			await assertOutcomeOrder(page);
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-collapsed`, { fullPage: false });
			for (const control of await page.locator('.passive-toggle').all()) {
				if (width === 393) await control.tap(); else { await control.focus(); await page.keyboard.press('Enter'); }
			}
			await expect(page.locator('.passive-toggle[aria-expanded="true"]')).toHaveCount(4);
			await expect(page.locator('.messages').getByText('Follow-up snapshot', { exact: true })).toBeVisible();
			await expect(page.locator('.messages').getByText('Deleted closed snapshot', { exact: true })).toBeVisible();
			await expect(page.locator('.passive-outcomes .deleted button')).toHaveCount(0);
			const stored = await page.locator('.passive-outcomes time').evaluateAll(times => times.map(time => time.getAttribute('datetime')));
			expect(stored).toEqual(proof.fixture.times.filter((_, i) => i !== 6));
			await page.keyboard.press('Tab');
			await assertPassiveGeometry(page);
			// PassiveOutcomeList.svelte: a node row is a `button.row` that expands in place; its detail has Open in plan.
			const node = page.locator('.passive-outcomes button.row').first();
			await node.focus();
			expect(await node.evaluate(element => getComputedStyle(element).outlineStyle)).toBe('solid');
			const disclosure = page.locator('.passive-toggle').first(); await disclosure.focus();
			expect(await disclosure.evaluate(element => getComputedStyle(element).outlineStyle)).toBe('solid');
			const green = await page.locator('.passive-outcomes .outcome-icon:not(.closed)').first().evaluate(element => getComputedStyle(element).color);
			expect(green).toBe(theme === 'light' ? 'rgb(22, 101, 52)' : 'rgb(52, 211, 153)');
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-expanded`, { fullPage: false });
			await expectObservedNoUpdate(request, main);
			nudged = false;
			await appendClosure(request, proof.fixture);
			await expect.poll(() => nudged).toBe(true);
			const finalPassive = page.locator('.messages .passive-outcomes').last();
			await expect(finalPassive.getByText('Live closure snapshot', { exact: true })).toBeVisible();
			await expect(finalPassive.locator('.passive-toggle')).toHaveAttribute('aria-expanded', 'true');
			await expectObservedNoUpdate(request, main);
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-append`, { fullPage: false });
			await focusSetting(page, false);
			await openOutcomeThread(page, proof.titles[0]);
			await expect(page.locator('.passive-outcomes')).toHaveCount(0);
			await expect(page.locator('.update-card time')).toHaveCount(10);
			await expect(page.getByText('Stopped work two', { exact: true })).toBeVisible();
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-focus-off`, { fullPage: false });
			await focusSetting(page, true);
			await openOutcomeThread(page, proof.titles[0]);
			await page.reload();
			// A reload lands on Activity once the first run is over, so the shell is the ready signal.
			await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
			await openOutcomeThread(page, proof.titles[0]);
			await expandLoadedWork(page);
			await expect(page.locator('.passive-outcomes')).toHaveCount(4);
			await expect(page.locator('.passive-toggle[aria-expanded="true"]')).toHaveCount(0);
			await expectObservedNoUpdate(request, main);
			await page.locator('.passive-toggle').first().click();
			await node.focus(); await page.keyboard.press('Enter');
			await expect(node).toHaveAttribute('aria-expanded', 'true');
			await page.getByRole('button', { name: 'Open in plan' }).first().click();
			await expect(page.getByText('Renamed after outcome', { exact: true })).toBeVisible();
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-node-navigation`, { fullPage: false });
			await page.goto('/_design/focus-outcomes');
			await expect(page.locator('.passive-toggle')).toHaveCount(2);
			for (const control of await page.locator('.passive-toggle').all()) await control.click();
			await expect(page.locator('.passive-toggle[aria-expanded="true"]')).toHaveCount(2);
			await expect(page.getByText('Time unavailable', { exact: true })).toBeVisible();
			await page.keyboard.press('Tab');
			await assertPassiveGeometry(page);
			await saveEvidenceScreenshot(page, info, `outcomes-${width}-${theme}-invalid-time`, { fullPage: false });
		});
	});
}
