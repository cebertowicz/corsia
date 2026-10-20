import { test } from '../support/test';
import { expect } from '@playwright/test';
import {
	deleteRenderedLine,
	expectLiveSocketsClosed,
	expectRenderedCaret,
	openLiveDocument,
	routeLiveSocket,
	seedLiveDocument,
	serverText,
	typeAtDocumentEnd,
} from '../support/liveDocument';

const escaped = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

for (const [width, height, project] of [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const) {
	test(`@slice1 @surface:live-document offline concurrent writers converge and retain a deletion-only device copy at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(180_000);
		const stamp = `${width}-${Date.now()}`;
		const deleted = `delete-${stamp}`;
		const firstEdit = `first-writer-${stamp}`;
		const secondEdit = `second-writer-${stamp}`;
		const seed = `# Shared ${stamp}\n\nkeep-${stamp}\n${deleted}\n`;
		const document = await seedLiveDocument(request, `convergence-${stamp}`, seed);
		const firstContext = await browser.newContext({ viewport: { width, height } });
		const secondContext = await browser.newContext({ viewport: { width, height } });
		try {
			const firstLink = await routeLiveSocket(firstContext);
			const secondLink = await routeLiveSocket(secondContext);
			const first = await firstContext.newPage();
			const second = await secondContext.newPage();
			const firstFrames = await openLiveDocument(first, document);
			const secondFrames = await openLiveDocument(second, document);
			await Promise.all([firstContext.setOffline(true), secondContext.setOffline(true)]);
			await Promise.all([firstLink.cut(), secondLink.cut()]);
			await expectLiveSocketsClosed(firstFrames);
			await expectLiveSocketsClosed(secondFrames);
			await expect(first.getByRole('status').filter({ hasText: 'Disconnected' })).toBeVisible();
			await expect(second.getByRole('status').filter({ hasText: 'Disconnected' })).toBeVisible();

			await Promise.all([typeAtDocumentEnd(first, firstEdit), typeAtDocumentEnd(second, secondEdit)]);
			await expect(first.locator('.cm-content')).toContainText(firstEdit);
			await expect(first.locator('.cm-content')).not.toContainText(secondEdit);
			await expect(second.locator('.cm-content')).toContainText(secondEdit);
			await expect(second.locator('.cm-content')).not.toContainText(firstEdit);
			await expectRenderedCaret(second, secondEdit);
			await deleteRenderedLine(second, deleted);
			await expect(second.getByRole('status').filter({ hasText: 'Disconnected · changes stay on this device' })).toBeVisible();
			await expect.poll(() => firstLink.refused(), { timeout: 15_000 }).toBeGreaterThan(0);
			await expect.poll(() => secondLink.refused(), { timeout: 15_000 }).toBeGreaterThan(0);
			await expectLiveSocketsClosed(firstFrames);
			await expectLiveSocketsClosed(secondFrames);
			expect(await serverText(request, document)).toBe(seed);
			await second.close();

			const firstOpens = firstFrames.opened.get(document.artifactId) ?? 0;
			await firstContext.setOffline(false);
			firstLink.restore();
			await expect.poll(() => firstFrames.opened.get(document.artifactId) ?? 0, { timeout: 45_000 }).toBeGreaterThan(firstOpens);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toContain(firstEdit);
			expect(await serverText(request, document)).not.toContain(secondEdit);
			await secondContext.setOffline(false);
			secondLink.restore();
			const reopened = await secondContext.newPage();
			await openLiveDocument(reopened, document, `# Shared ${stamp}`);
			await expect(reopened.locator('.cm-content')).toContainText(firstEdit);
			await expect(reopened.locator('.cm-content')).toContainText(secondEdit);
			await expect(reopened.locator('.cm-content')).not.toContainText(deleted);
			await expect(first.locator('.cm-content')).toContainText(secondEdit);
			await expect(first.locator('.cm-content')).not.toContainText(deleted);

			// Both offline inserts sit at the old end; the CRDT orders them, so either order is the one merge.
			const merged = new RegExp(`^${escaped(`# Shared ${stamp}\n\nkeep-${stamp}\n`)}(${escaped(firstEdit + secondEdit)}|${escaped(secondEdit + firstEdit)})$`);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toMatch(merged);
			const edits = (await serverText(request, document)).match(merged)![1];
			await expect(first.locator('.cm-content')).toContainText(edits);
			await expect(reopened.locator('.cm-content')).toContainText(edits);
		} finally {
			await firstContext.close();
			await secondContext.close();
		}
	});
}
