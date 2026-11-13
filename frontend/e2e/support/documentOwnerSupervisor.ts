import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';
import { isolatedDb } from './heldComments';
import { injectMessage } from './mock';

export const LONG_NAME = 'Community outreach and neighbourhood volunteer coordination for the Garden Volunteers';

/**
 * A registered planning document owned by the agent "Release planning", supervised by "Research coordinator", in a
 * scope with a few more agents ("Community outreach" has a paused plan). Returns `open(page)`, which opens the
 * document beside that agent's chat, and the ids a spec needs to move the document into another ownership state.
 */
export async function seedOwnedDocument(page: Page, request: APIRequestContext) {
	const owner = await createSession(request, 'Release planning');
	const names = ['Research coordinator', 'Community outreach', 'Garden volunteers', 'Planning specialist', LONG_NAME];
	const agents: string[] = [];
	for (const name of names) agents.push(await createSession(request, name));
	isolatedDb([
		"for agent in sys.argv[2:]: connection.execute(\"UPDATE threads SET agent_enabled=1, agent_status='active' WHERE id=?\", (agent,))",
		"connection.execute(\"INSERT INTO plans(id,owner_kind,owner_id,state,created_at,updated_at) VALUES (?, 'thread', ?, 'paused', '2026-01-01', '2026-01-01')\", ('paused-' + sys.argv[4], sys.argv[4]))",
	], owner, ...agents);
	await injectMessage(request, owner, { role: 'user', content: 'Please put the project plan together so we can review the next steps.' }, TEST_TOKEN);
	await injectMessage(request, owner, { role: 'assistant', content: 'I created the planning document. You can review it alongside our conversation.', metadata: { for_user: true } }, TEST_TOKEN);
	const params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const fixtureDir = path.join(workspace, 'shared', `planning-${owner}`);
	mkdirSync(fixtureDir, { recursive: true });
	const index = await request.put('/api/files/content', { params, data: { path: path.join(fixtureDir, 'INDEX.md'), text: '# Planning\n', thread_id: owner, create: true } });
	expect(index.ok(), await index.text()).toBeTruthy();
	const docPath = path.join(fixtureDir, 'Project planning.md');
	const sections = [
		['Purpose', 'Plan a community garden that neighbours can reach and enjoy throughout the year.'],
		['Outcomes', 'Prepare the site for a first planting day and make room for future workshops.'],
		['Scope', 'Begin with shared beds, a tool store and a clear path through the garden.'],
		['Next steps', 'Share the planting plan and invite neighbours to the first session.'],
	].map(([title, body]) => `## ${title}\n\n${body}`).join('\n\n');
	const created = await request.put('/api/files/content', { params, data: { path: docPath, text: `# Project planning\n\n${sections}\n`, thread_id: owner, description: 'Planning document', create: true } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const artifact = (await created.json()).artifact.id as string;
	isolatedDb([
		"connection.execute(\"UPDATE artifact_identities SET metadata=json_set(COALESCE(metadata, '{}'), '$.supervisor_thread_id', ?) WHERE id=?\", (sys.argv[3], sys.argv[2]))",
	], artifact, agents[0]);
	await page.context().addInitScript(({ owner, artifact, docPath }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [owner]: { tabs: [{ instanceId: 'owned-doc', artifactId: artifact, path: docPath, title: 'Project planning.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'owned-doc', visible: true, activeSurface: 'artifact' } } }));
	}, { owner, artifact, docPath });
	async function open(target: Page) {
		await loginByUrl(target, { waitForComposer: false });
		if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
		await target.getByText('Release planning', { exact: true }).first().click();
		await expect(target.locator('.cm-content .cm-md-h1')).toBeVisible();
		await expect(ownerItem(target)).toBeVisible();
	}
	return { open, artifact, owner, agents, docPath };
}

export const ownership = (page: Page) => page.getByRole('group', { name: 'Owner and supervisor' });
export const ownerItem = (page: Page) => ownership(page).locator('button.item[data-role="owner"]');
export const supervisorItem = (page: Page) => ownership(page).locator('button.item[data-role="supervisor"]');
export const rolePicker = (page: Page) => page.locator('[data-popover] .role-picker');

/** Two boxes overlap when they share any area. */
export function overlaps(a: { x: number; y: number; width: number; height: number }, b: { x: number; y: number; width: number; height: number }) {
	return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}
