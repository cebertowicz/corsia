import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, TEST_SESSION_TOKEN, TEST_TOKEN } from './corsia';
import type { LiveDocument } from './liveDocument';
import { readMockCallCount, readUnmatchedThreads, seedMockResponses } from './mock';

/** The approved category the shared fixture lives in; the first seed creates it. */
export const SHARED_CATEGORY = 'live-typing';

/** A new agent thread shown on a new page in `beside`'s browser context; `promoted` collects it for the spec's demote when given. */
export async function openAgentBeside(request: APIRequestContext, beside: Page, title: string, promoted?: string[]): Promise<{ id: string; title: string; page: Page }> {
	const id = await createSession(request, title);
	promoted?.push(id);
	const page = await beside.context().newPage();
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	return { id, title, page };
}

/** A doc_comment_create call's JSON input cut the way a provider streams it: the quote whole, then the replacement in three pieces. */
export function streamedPieces(input: string): string[] {
	const start = input.indexOf('"replacement":"') + '"replacement":"'.length;
	const end = input.indexOf('","client_id"');
	const value = input.slice(start, end);
	const third = Math.ceil(value.length / 3);
	return [input.slice(0, start), value.slice(0, third), value.slice(third, third * 2), value.slice(third * 2) + input.slice(end)];
}

/**
 * A live document an agent creates, and so owns, in an approved shared/<category>/. Shared documents
 * enforce ownership: only the owner, the conductor or the user edits one directly, and every other agent
 * comments or suggests (registry/shared_documents.py). So the writing agent creates the document in a real
 * mock-provider turn, exactly as an agent drafting its own document would; the viewer opens it afterwards.
 * `writer` is that agent: its thread id and title, and a page showing its chat.
 */
export async function agentCreatesSharedDocument(request: APIRequestContext, writer: { id: string; title: string; page: Page }, label: string, body: string): Promise<LiveDocument> {
	const { id: threadId, title } = writer;
	// Shared documents are owned by persistent agents, so the writer is promoted the way a user would.
	const promoted = await request.patch(`/api/agents/${threadId}?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { responsibility: 'Writes the typed reveal document.', active: true },
	});
	expect(promoted.ok(), await promoted.text()).toBeTruthy();
	const tree = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find(root => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	// A fresh e2e workspace has no shared/ root yet; the server creates only the category folder.
	mkdirSync(path.join(workspace!, 'shared'), { recursive: true });
	// Another seed may have created the category already: a 409 here is the category existing.
	const index = await request.put(`/api/files/content?token=${TEST_TOKEN}`, {
		data: { path: path.join(workspace!, 'shared', SHARED_CATEGORY, 'INDEX.md'), text: `# ${SHARED_CATEGORY}\n`, thread_id: threadId, create: true },
	});
	expect([200, 409], await index.text()).toContain(index.status());

	const fileName = `live-${label}-${Date.now()}.md`;
	const docPath = path.join(workspace!, 'shared', SHARED_CATEGORY, fileName);
	// Bound to the writer; a model call from any other thread fails the seed below rather than taking these replies.
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `create-${fileName}`, name: 'write_file', arguments: JSON.stringify({
			path: docPath, content: body, description: 'Typed reveal e2e fixture', update_trigger: 'when the typed reveal spec changes',
		}) }], finish_reason: 'tool_calls', match_thread: threadId },
		{ content: 'Created the document.', finish_reason: 'stop', match_thread: threadId },
	]);
	await sendChatMessage(writer.page, 'Create the document.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(2);
	expect(await readUnmatchedThreads(request), 'a thread other than the writer called the model').toEqual([]);
	await expect.poll(async () => (await request.get(`/api/files/content?token=${TEST_TOKEN}&path=${encodeURIComponent(docPath)}`)).status(),
		{ timeout: 10_000, message: 'the agent created its document' }).toBe(200);
	const created = await (await request.get(`/api/files/content?token=${TEST_TOKEN}&path=${encodeURIComponent(docPath)}`)).json();
	expect(created.text).toBe(body);
	return { artifactId: created.artifact.id as string, body, docPath, fileName, threadId, title };
}

/** Demotes an agent `agentCreatesSharedDocument` promoted, so later cases find a short agent rail. */
export async function demoteAgent(request: APIRequestContext, threadId: string): Promise<void> {
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const response = await request.delete(`/api/agents/${threadId}?${query}`);
	expect(response.ok(), await response.text()).toBeTruthy();
}

/** Designates `threadId` the CORSIA agent, the one other agent that may edit a document it does not own; null clears it. */
export async function designateConductor(request: APIRequestContext, threadId: string | null): Promise<void> {
	const response = await request.put(`/api/corsia-agent?token=${TEST_SESSION_TOKEN}`, { data: { thread_id: threadId } });
	expect(response.ok(), await response.text()).toBeTruthy();
}
