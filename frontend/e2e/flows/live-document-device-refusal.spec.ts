import { expect, test, type Page } from '@playwright/test';
import {
	expectLiveSocketsClosed,
	openLiveDocument,
	routeLiveSocket,
	seedLiveDocument,
	serverText,
	typeAtDocumentEnd,
} from '../support/liveDocument';

type QuotaFault = { armed: boolean; calls: number };

async function installIndexedDbQuotaFault(page: Page, artifactId: string): Promise<void> {
	await page.addInitScript(({ artifactId }) => {
		const fault: QuotaFault = { armed: false, calls: 0 };
		Object.defineProperty(window, '__corsiaLiveDocumentQuotaFault', { configurable: false, value: fault });
		const refuse = (original: typeof IDBObjectStore.prototype.add) => function(this: IDBObjectStore, value: unknown, key?: IDBValidKey) {
			const request = key === undefined ? original.call(this, value) : original.call(this, value, key);
			if (fault.armed && this.name === 'updates' && this.transaction.db.name.startsWith(`corsia-document:${artifactId}:`)) {
				fault.armed = false;
				fault.calls += 1;
				// Quota refusal fails an asynchronous write. A real transaction abort exercises
				// that boundary without throwing into Yjs's synchronous update listeners.
				this.transaction.abort();
			}
			return request;
		};
		const add = IDBObjectStore.prototype.add;
		const put = IDBObjectStore.prototype.put;
		IDBObjectStore.prototype.add = refuse(add) as typeof IDBObjectStore.prototype.add;
		IDBObjectStore.prototype.put = refuse(put) as typeof IDBObjectStore.prototype.put;
	}, { artifactId });
}

function quotaFault(page: Page) {
	return page.evaluate(() => (window as typeof window & { __corsiaLiveDocumentQuotaFault: QuotaFault }).__corsiaLiveDocumentQuotaFault);
}

// Socket cuts use the same real-backend transport helper as the convergence proof.

for (const [width, height, project] of [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const) {
	test(`@slice1 @surface:live-document IndexedDB quota refusal keeps the full source in this tab at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const localOnly = `device-only-${stamp}`;
		const body = `# Device refusal ${stamp}\n\nserver text ${stamp}\n`;
		const document = await seedLiveDocument(request, `device-refusal-${stamp}`, body);
		const context = await browser.newContext({ viewport: { width, height } });
		await context.grantPermissions(['clipboard-read', 'clipboard-write']);
		try {
			const socket = await routeLiveSocket(context);
			const page = await context.newPage();
			await installIndexedDbQuotaFault(page, document.artifactId);
			const frames = await openLiveDocument(page, document);
			await expect(page.locator('.cm-content')).toContainText(`server text ${stamp}`);

			await socket.cut();
			await expectLiveSocketsClosed(frames);
			await expect(page.getByRole('status').filter({ hasText: 'Disconnected' })).toBeVisible();
			await page.evaluate(() => {
				(window as typeof window & { __corsiaLiveDocumentQuotaFault: QuotaFault }).__corsiaLiveDocumentQuotaFault.armed = true;
			});
			await typeAtDocumentEnd(page, localOnly);
			await expect.poll(() => quotaFault(page).then(fault => fault.calls), { timeout: 20_000 }).toBe(1);
			const status = page.getByRole('status').filter({ hasText: "Disconnected · changes aren't saved; keep this tab open" });
			await expect(status).toBeVisible();
			await status.getByRole('button', { name: 'Copy text', exact: true }).click();
			await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()), { timeout: 20_000 }).toBe(`${body}${localOnly}`);

			expect(await serverText(request, document)).toBe(body);
			const opens = frames.opened.get(document.artifactId) ?? 0;
			socket.restore();
			await expect.poll(() => frames.opened.get(document.artifactId) ?? 0, { timeout: 45_000 }).toBeGreaterThan(opens);
			await expect(page.locator('.cm-content')).toContainText(localOnly, { timeout: 20_000 });
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(`${body}${localOnly}`);
		} finally {
			await context.close();
		}
	});
}
