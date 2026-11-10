import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';
import { injectMessage } from '../support/mock';

export const scrollAnchor = '.cm-content';

export async function seed(page: Page, request: APIRequestContext) {
	const owner = await createSession(request, 'Release planning');
	const agents: string[] = [];
	for (let i = 1; i <= 25; i++) {
		agents.push(await createSession(request, i === 1
			? 'A very long agent name for planning and coordinating document changes across multiple teams'
			: i === 2 ? 'Research coordinator' : i === 3 ? 'Community outreach' : i === 4 ? 'Garden volunteers' : `Planning specialist ${String(i).padStart(2, '0')}`));
	}
	isolatedDb([
		"for agent in sys.argv[2:]: connection.execute(\"UPDATE threads SET agent_enabled=1, agent_status='active' WHERE id=?\", (agent,))",
		"connection.execute(\"INSERT INTO plans(id,owner_kind,owner_id,state,created_at,updated_at) VALUES (?, 'thread', ?, 'paused', '2026-01-01', '2026-01-01')\", ('sheet-paused-' + sys.argv[3], sys.argv[3]))",
		"for agent in [sys.argv[3], *sys.argv[7:]]: connection.execute(\"UPDATE threads SET spawned_by=? WHERE id=?\", (sys.argv[2], agent))",
		"connection.execute(\"UPDATE threads SET model=NULL WHERE id=?\", (sys.argv[2],))",
	], owner, ...agents);
	await injectMessage(request, owner, { role: 'user', content: 'Please put the project plan together so we can review the next steps.' }, TEST_TOKEN);
	await injectMessage(request, owner, { role: 'assistant', content: 'I created the planning document. You can review it alongside our conversation.', metadata: { for_user: true } }, TEST_TOKEN);
	const params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	mkdirSync(path.join(workspace, 'shared'), { recursive: true });
	const fixtureDir = path.join(workspace, 'shared', `planning-${owner}`);
	const index = await request.put('/api/files/content', { params, data: { path: path.join(fixtureDir, 'INDEX.md'), text: '# Planning\n', thread_id: owner, create: true } });
	expect(index.ok(), await index.text()).toBeTruthy();
	const docPath = path.join(fixtureDir, 'Project planning.md');
	const paragraphs = [
		['Purpose', 'Plan a community garden that neighbours can reach and enjoy throughout the year.'],
		['Outcomes', 'Prepare the site for a first planting day and make room for future workshops.'],
		['Discovery', 'Ask neighbours what they would like to grow and when they can take part.'],
		['Scope', 'Begin with shared beds, a tool store and a clear path through the garden.'],
		['Design', 'Sketch the route from the station to the community garden and identify accessible entrances.'],
		['Implementation', 'Prepare the planting beds and arrange tools for the volunteers joining the first session.'],
		['Review', 'Walk the site with neighbours and collect their suggestions for the layout.'],
		['Verification', 'Check the irrigation schedule against the forecast and update the planting calendar.'],
		['Dependencies', 'Confirm access to water and delivery dates for soil and timber.'],
		['Risks', 'Allow extra time for materials to arrive and keep a sheltered location available for rain.'],
		['Next steps', 'Share the planting plan and invite neighbours to the first session.'],
		['Open questions', 'Decide where to place the compost bins and how to share the watering schedule.'],
	].map(([title, body]) => `## ${title}\n\n${body}`).join('\n\n');
	const created = await request.put('/api/files/content', { params, data: { path: docPath, text: `# Project planning\n\n${paragraphs}\n`, thread_id: owner, description: 'Planning document', create: true } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const artifact = (await created.json()).artifact.id;
	isolatedDb([
		"connection.execute(\"UPDATE artifact_identities SET metadata=json_set(COALESCE(metadata, '{}'), '$.supervisor_thread_id', ?) WHERE id=?\", (sys.argv[3], sys.argv[2]))",
	], artifact, agents[1]);
	await page.context().addInitScript(({ owner, artifact, docPath }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [owner]: { tabs: [{ instanceId: 'o1-sheet', artifactId: artifact, path: docPath, title: 'Project planning.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'o1-sheet', visible: true, activeSurface: 'artifact' } } }));
	}, { owner, artifact, docPath });
	async function open(target: Page) {
		await loginByUrl(target, { waitForComposer: false });
		if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
		await target.getByText('Release planning', { exact: true }).first().click();
		await expect(target.locator('.cm-content')).toBeVisible();
		await expect(target.locator('.doc')).toHaveCSS('opacity', '1');
	}
	await open(page);
	return open;
}

export async function showPicker(target: Page) {
	const details = target.getByRole('button', { name: 'Document details' });
	const identity = target.locator('button.identity:visible').first();
	if (!(await identity.isVisible()) && await details.isVisible()) await details.click();
	await identity.click();
	await target.getByRole('combobox', { name: 'Supervisor', exact: true }).click();
	await expect(target.getByRole('option', { name: /A very long agent name.*Paused/ })).toBeVisible();
}
