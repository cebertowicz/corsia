import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';
import { injectMessage } from '../support/mock';

export const scrollAnchor = '.cm-content';

export async function seed(page: Page, request: APIRequestContext) {
	const owner = await createSession(request, 'Documents and Artifacts');
	const agents: string[] = [];
	for (let i = 1; i <= 25; i++) {
		agents.push(await createSession(request, i === 1
			? 'A very long agent name for planning and coordinating document changes across multiple teams'
			: `Planning specialist ${String(i).padStart(2, '0')}`));
	}
	isolatedDb([
		"for agent in sys.argv[2:]: connection.execute(\"UPDATE threads SET agent_enabled=1, agent_status='active' WHERE id=?\", (agent,))",
		"connection.execute(\"INSERT INTO plans(id,owner_kind,owner_id,state,created_at,updated_at) VALUES ('sheet-paused','thread',?,'paused','2026-01-01','2026-01-01')\", (sys.argv[3],))",
	], owner, ...agents);
	await injectMessage(request, owner, { role: 'user', content: 'Please put the project plan together so we can review the next steps.' }, TEST_TOKEN);
	await injectMessage(request, owner, { role: 'assistant', content: 'I created the planning document. You can review it alongside our conversation.', metadata: { for_user: true } }, TEST_TOKEN);
	const params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	mkdirSync(path.join(workspace, 'shared'), { recursive: true });
	const index = await request.put('/api/files/content', { params, data: { path: path.join(workspace, 'shared', 'planning', 'INDEX.md'), text: '# Planning\n', thread_id: owner, create: true } });
	expect(index.ok(), await index.text()).toBeTruthy();
	const docPath = path.join(workspace, 'shared', 'planning', 'Project planning.md');
	const paragraphs = [
		['Purpose', 'Give the team a shared view of the next release and the decisions needed before work begins.'],
		['Outcomes', 'The first milestone is a usable draft that the team can review in the context of its actual work.'],
		['Discovery', 'Gather the questions raised by the people who use the current workflow and map them to the tasks they are trying to complete.'],
		['Scope', 'Keep the first release focused on the core journey. Record adjacent ideas separately so they remain visible without delaying the main work.'],
		['Design', 'Show the proposed experience in the surrounding application, including narrow screens and the states a person can encounter.'],
		['Implementation', 'Use the existing document surface and shared controls. Keep the ownership information close to the document it describes.'],
		['Review', 'Invite the people responsible for the affected experience to walk the full journey and record any gaps they find.'],
		['Verification', 'Check that changes appear after saving and that a refused change leaves the previous value visible with a useful explanation.'],
		['Dependencies', 'Confirm which decisions need an answer before implementation and which details can be resolved as the work proceeds.'],
		['Risks', 'A missing or unavailable supervisor should be visible at the point where someone decides who will receive comments.'],
		['Next steps', 'Share the draft, collect feedback, and update the plan when the review is complete.'],
		['Open questions', 'Identify the people who will maintain the document and how they will know when an action needs their attention.'],
	].map(([title, body]) => `## ${title}\n\n${body}`).join('\n\n');
	const created = await request.put('/api/files/content', { params, data: { path: docPath, text: `# Project planning\n\n${paragraphs}\n`, thread_id: owner, description: 'Planning document', create: true } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const artifact = (await created.json()).artifact.id;
	await page.context().addInitScript(({ owner, artifact, docPath }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [owner]: { tabs: [{ instanceId: 'o1-sheet', artifactId: artifact, path: docPath, title: 'Project planning.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'o1-sheet', visible: true, activeSurface: 'artifact' } } }));
	}, { owner, artifact, docPath });
	async function open(target: Page) {
		await loginByUrl(target, { waitForComposer: false });
		if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
		await target.getByText('Documents and Artifacts', { exact: true }).first().click();
		await expect(target.locator('.cm-content')).toBeVisible();
		await expect(target.locator('.doc')).toHaveCSS('opacity', '1');
		await target.getByRole('button', { name: /^Owner:/ }).click();
		await target.getByRole('combobox', { name: 'Supervisor', exact: true }).click();
		await expect(target.getByRole('option', { name: /A very long agent name.*Paused/ })).toBeVisible();
		expect(await target.getByRole('option').count()).toBeGreaterThanOrEqual(26);
		const list = target.getByRole('listbox');
		expect(await list.evaluate(el => el.scrollHeight > el.clientHeight)).toBeTruthy();
	}
	await open(page);
	return open;
}
