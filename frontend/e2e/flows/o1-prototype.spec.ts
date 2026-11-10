import path from 'node:path';
import { mkdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { isolatedDb, setDebugVersionOverlay } from '../support/heldComments';
import { injectMessage } from '../support/mock';

function fixture(action: string, artifact: string, owner: string, paused: string) {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!state || !python) throw new Error('Disposable E2E state and Python are required');
	execFileSync(python, ['-c', `
import json, sqlite3, sys
c=sqlite3.connect(sys.argv[1]); action, artifact, owner, paused=sys.argv[2:]
c.execute("UPDATE threads SET agent_enabled=1, agent_status='active' WHERE id IN (?,?)", (owner,paused))
c.execute("INSERT OR REPLACE INTO plans(id,owner_kind,owner_id,state,created_at,updated_at) VALUES ('o1-paused','thread',?,'paused','2026-01-01','2026-01-01')", (paused,))
c.execute("DELETE FROM users WHERE id='o1-second-person'")
if action == 'refused':
 c.execute("INSERT INTO users(id,password_hash,created_at,password_changed_at) VALUES ('o1-second-person','fixture','2026-01-01','2026-01-01')")
if action == 'empty':
 c.execute("UPDATE threads SET agent_enabled=0 WHERE user_id='e2e'")
kind = 'ui' if action in ('person','fallback','unavailable','empty') else owner
metadata = {'owner_user_id':'e2e','supervisor_thread_id':owner} if action == 'person' else {'supervisor_thread_id':owner} if action == 'fallback' else {'supervisor_thread_id':paused} if action == 'unavailable' else {'supervisor_thread_id':'missing-o1-supervisor'} if action == 'empty' else {}
c.execute("DELETE FROM artifact_registrations WHERE artifact_id=? AND parent_thread_id=? AND relationship_state!='owner-active'", (artifact, kind))
c.execute("UPDATE artifact_registrations SET parent_thread_id=? WHERE artifact_id=? AND relationship_state='owner-active'", (kind, artifact))
c.execute("UPDATE artifact_identities SET metadata=? WHERE id=?", (json.dumps(metadata), artifact))
c.commit()
`, `${state}/corsia.db`, action, artifact, owner, paused]);
}

test('capture document ownership states', async ({ page }) => {
	test.setTimeout(300_000);
	await setDebugVersionOverlay(page.request, false);
	isolatedDb(['cursor = connection.execute("UPDATE models SET display_name=? WHERE id=?", ("Claude Sonnet 5.5", "mock-test-model"))', 'assert cursor.rowcount == 1']);
	await page.context().routeWebSocket('**/ws', socket => {
		const server = socket.connectToServer();
		server.onMessage(message => {
			if (typeof message !== 'string') return socket.send(message);
			const payload = JSON.parse(message);
			if (payload.type === 'auth_ok' && payload.user) payload.user.name = 'Jordan';
			socket.send(JSON.stringify(payload));
		});
	});
	const owner = await createSession(page.request, 'Documents and Artifacts');
	await injectMessage(page.request, owner, { role: 'user', content: 'Please put the project plan together so we can review the next steps.' }, TEST_TOKEN);
	await injectMessage(page.request, owner, { role: 'assistant', content: 'I created the planning document. You can review it alongside our conversation.', metadata: { for_user: true } }, TEST_TOKEN);
	const paused = await createSession(page.request, 'A very long agent name for planning and coordinating document changes');
	const params = { token: TEST_TOKEN };
	const roots = (await (await page.request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	mkdirSync(path.join(workspace, 'shared'), { recursive: true });
	const index = await page.request.put('/api/files/content', { params, data: { path: path.join(workspace, 'shared', 'planning', 'INDEX.md'), text: '# Planning\n', thread_id: owner, create: true } });
	expect(index.ok(), await index.text()).toBeTruthy();
	const docs = [] as { path: string; artifact: string; title: string }[];
	for (const [folder, title] of [['shared/planning', 'Project planning.md'], ['', 'Meeting notes.md']]) {
		const docPath = path.join(workspace, folder, title);
		const created = await page.request.put('/api/files/content', { params, data: { path: docPath, text: `# ${title.replace('.md', '')}\n\nThis document records the decisions and next steps for the work.\n\n## Next steps\n\n- Review the plan together.\n- Capture the outcome.\n`, thread_id: owner, description: 'Planning document', create: true } });
		expect(created.ok(), await created.text()).toBeTruthy();
		docs.push({ path: docPath, artifact: (await created.json()).artifact.id, title });
	}
	await page.addInitScript(({ owner, docs }) => {
		const tab = docs[0];
		if (!localStorage.getItem('corsia:artifactWorkspace:v1')) localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [owner]: { tabs: [{ instanceId: 'o1-tab', artifactId: tab.artifact, path: tab.path, title: tab.title, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'o1-tab', visible: true, activeSurface: 'artifact' } } }));
	}, { owner, docs });
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.emulateMedia({ colorScheme: 'light' });
	fixture('agent', docs[0].artifact, owner, paused);
	await loginByUrl(page, { waitForComposer: false });
	await page.getByText('Documents and Artifacts', { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toBeVisible();
	const out = process.env.O1_FRAME_DIR;
	if (!out) throw new Error('O1_FRAME_DIR is required');
	const setTab = async (index: number) => page.evaluate(({ owner, doc }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [owner]: { tabs: [{ instanceId: 'o1-tab', artifactId: doc.artifact, path: doc.path, title: doc.title, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'o1-tab', visible: true, activeSurface: 'artifact' } } }));
	}, { owner, doc: docs[index] });
	const states = ['agent', 'person', 'fallback', 'unavailable', 'picker', 'nonshared', 'refused', 'empty', 'nomatch'];
	for (const width of [1440, 900, 393]) {
		await page.setViewportSize({ width, height: 900 });
		for (const state of states) {
			if (process.env.O1_ONLY && !process.env.O1_ONLY.split(',').includes(`${state}-${width}`)) continue;
			const doc = docs[state === 'nonshared' ? 1 : 0];
			fixture(state, doc.artifact, owner, paused);
			await setTab(state === 'nonshared' ? 1 : 0);
			await page.reload();
			if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText('Documents and Artifacts', { exact: true }).first().click();
			await expect(page.locator('.cm-content')).toBeVisible();
			await expect(page.locator('.doc')).toHaveCSS('opacity', '1');
			const control = page.getByRole('button', { name: /^Owner:/ });
			await expect(control).toBeVisible();
			if (state === 'person') await expect(control).toContainText('Owner: You');
			if (state === 'fallback') await expect(control).toContainText('Owner: none');
			if (state === 'unavailable') await expect(control).toContainText('No one gets these comments');
			if (['picker', 'refused', 'empty', 'nomatch'].includes(state)) {
				await control.click();
				await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeVisible();
				if (state === 'picker') {
					await page.getByRole('combobox', { name: 'Supervisor', exact: true }).click();
					await expect(page.getByRole('option', { name: /A very long agent name.*Paused/ })).toBeVisible();
				}
			}
			if (state === 'empty') await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toHaveValue('No agents in this scope');
			if (state === 'nomatch') {
				await page.getByRole('combobox', { name: 'Supervisor', exact: true }).fill('No such agent');
				await expect(page.getByText('No matching agents.')).toBeVisible();
			}
			if (state === 'refused') {
				await page.getByRole('combobox', { name: 'Owner', exact: true }).click();
				await page.getByRole('option', { name: 'You', exact: true }).click();
				await expect(page.getByText("Taking over a document isn't available yet when more than one person uses this CORSIA.")).toBeVisible();
				await expect(page.getByRole('combobox', { name: 'Owner', exact: true })).toHaveValue('Documents and Artifacts');
				await expect(page.getByRole('combobox', { name: 'Owner', exact: true })).toHaveAttribute('aria-describedby', 'owner-error');
			}
			const box = await page.locator('.doc').boundingBox();
			if (!box) throw new Error('Document canvas is not visible');
			if (state === 'picker' || state === 'refused') {
				const card = await page.locator('[data-popover]').boundingBox();
				if (!card) throw new Error('Ownership popover is not visible');
				expect(card.x).toBeGreaterThanOrEqual(box.x - 1);
				expect(card.x + card.width).toBeLessThanOrEqual(box.x + box.width + 1);
			}
			await expect(page.locator('.version-overlay')).toHaveCount(0);
			await expect(page.getByText('Mock Test Model', { exact: true })).toHaveCount(0);
			await expect(page.locator('body')).not.toContainText(/\be2e\b/i);
			await page.screenshot({ path: path.join(out, `${state}-${width}.png`), ...(width === 900 ? { clip: box } : {}), animations: 'disabled' });
			if (state === 'picker') await page.keyboard.press('Escape');
			if (state === 'agent' && width === 1440) {
				await page.emulateMedia({ colorScheme: 'dark' });
				await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
				await page.screenshot({ path: path.join(out, 'agent-1440-dark.png'), animations: 'disabled' });
				await page.emulateMedia({ colorScheme: 'light' });
			}
		}
	}
});
