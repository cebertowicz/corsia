import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type BrowserContext, type Page, type WebSocketRoute } from '@playwright/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { openLiveDocument, serverText, type LiveDocument } from '../support/liveDocument';

interface HeldFlush {
	server: WebSocketRoute;
	payload: string | Buffer;
}

/**
 * Forward the real live socket unchanged, holding only ordinary flushes after
 * repair. A retry frame still reaches the server, so its successful write is
 * distinguishable from the autosave that would otherwise clear the row first.
 */
async function holdRepairedAutosave(context: BrowserContext) {
	let armed = false;
	let retryCount = 0;
	const held: HeldFlush[] = [];
	await context.routeWebSocket(url => url.pathname === '/ws', socket => {
		const server = socket.connectToServer();
		socket.onMessage(payload => {
			let frame: { type?: string; retry?: boolean } | null = null;
			try {
				frame = JSON.parse(String(payload)) as { type?: string; retry?: boolean };
			} catch {
				// Live protocol frames are JSON; unrelated frames retain their normal path.
			}
			if (frame?.type === 'doc_flush' && frame.retry === true) retryCount += 1;
			if (armed && frame?.type === 'doc_flush' && frame.retry !== true) {
				held.push({ server, payload });
				return;
			}
			server.send(payload);
		});
		server.onMessage(payload => socket.send(payload));
	});
	return {
		arm: () => { armed = true; },
		async waitForHeldAutosave() {
			await expect.poll(() => held.length, { timeout: 20_000 }).toBeGreaterThan(0);
		},
		retryCount: () => retryCount,
		async release() {
			armed = false;
			for (const flush of held.splice(0)) flush.server.send(flush.payload);
		},
	};
}

async function seedLiveSkill(request: APIRequestContext, stamp: string): Promise<LiveDocument> {
	const title = `live-validator-${stamp}`;
	const threadId = await createSession(request, title);
	const tree = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find(root => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const name = `validator-${stamp}`;
	const body = `---\nname: ${name}\ndescription: A valid live validator fixture\n---\n# ${name}\n`;
	// An imported skill still uses the SKILL.md validator, without being guidance tracked in Git.
	const docPath = path.join(workspace!, 'validation-fixtures', 'skills', name, 'SKILL.md');
	const created = await request.put(`/api/files/content?token=${TEST_TOKEN}`, {
		data: { path: docPath, text: body, thread_id: threadId, create: true },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return {
		artifactId: (await created.json()).artifact.id as string,
		body,
		docPath,
		fileName: 'SKILL.md',
		threadId,
		title,
	};
}

/**
 * Replaces the frontmatter name line through the real editor. The line is reached by pointer, not
 * ControlOrMeta+Home: Playwright resolves that modifier from the host OS, while CodeMirror resolves
 * Mod from the emulated navigator.platform (Windows or Android in these projects), so on a macOS
 * host the key is unbound and the caret stays where the click left it, after the last line.
 */
async function replaceSkillName(page: Page, value: string): Promise<void> {
	const line = page.locator('.cm-content .cm-line').filter({ hasText: /^name:/ });
	await expect(line).toHaveCount(1);
	await line.click();
	await page.keyboard.press('End');
	await page.keyboard.press('Shift+Home');
	await page.keyboard.type(value);
	// The editor renders frontmatter lines verbatim, so a misplaced edit fails here, not at the row.
	await expect(line).toHaveText(value);
}

for (const [width, height, project] of [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const) {
	test(`@slice1 @surface:live-document SKILL validator refusal keeps text and Retry writes the repaired file at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const document = await seedLiveSkill(request, stamp);
		const name = `validator-${stamp}`;
		const invalidBody = document.body.replace(`name: ${name}`, 'name:');
		const context = await browser.newContext({ viewport: { width, height } });
		await context.grantPermissions(['clipboard-read', 'clipboard-write']);
		try {
			const flushes = await holdRepairedAutosave(context);
			const page = await context.newPage();
			await openLiveDocument(page, document, `name: ${name}`);
			await replaceSkillName(page, 'name:');

			const refusal = page.getByRole('status').filter({ hasText: 'Not saved to the file: Error: SKILL.md not saved — it would not load as a skill. The file is unchanged.' });
			await expect(refusal).toBeVisible({ timeout: 20_000 });
			await refusal.getByRole('button', { name: 'Copy text', exact: true }).click();
			await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()), { timeout: 20_000 }).toBe(invalidBody);
			expect(await serverText(request, document)).toBe(document.body);

			flushes.arm();
			// The real autosave is held at the wire after repair. The immediate keyboard repair and
			// visible-row click therefore prove this retry, not a debounce, writes the file.
			await replaceSkillName(page, `name: ${name}`);
			await flushes.waitForHeldAutosave();
			await refusal.getByRole('button', { name: 'Copy text', exact: true }).click();
			await expect.poll(() => page.evaluate(() => navigator.clipboard.readText()), { timeout: 20_000 }).toBe(document.body);
			await refusal.getByRole('button', { name: 'Retry', exact: true }).click();
			await expect.poll(flushes.retryCount, { timeout: 20_000 }).toBe(1);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(document.body);
			await expect(refusal).not.toBeVisible();
			await flushes.release();
		} finally {
			await context.close();
		}
	});
}
