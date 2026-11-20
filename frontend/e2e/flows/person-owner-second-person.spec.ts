import { createHash, randomBytes } from 'node:crypto';
import { expect, type Browser, type Page } from '@playwright/test';
import { test } from '../support/test';
import { e2eModel, TEST_SESSION_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';
import { ownerItem, rolePicker, seedOwnedDocument } from '../support/documentOwnerSupervisor';
import { beginComment, submitButton } from '../support/liveCommentProof';

for (const [label, width, height, project] of [
	['wide', 1440, 900, 'chromium-desktop'],
	['phone', 393, 852, 'chromium-mobile'],
] as const) {
	test(`a second person takes ownership and comments at ${label}`, async ({ page, request, browser }, info) => {
		test.skip(info.project.name !== project);
		test.setTimeout(120_000);
		const seeded = await seedOwnedDocument(page, request, 'Garden plan');
		const mayaToken = randomBytes(32).toString('hex');
		const jordanToken = randomBytes(32).toString('hex');
		const digest = (token: string) => createHash('sha256').update(token).digest('hex');
		isolatedDb([
			"connection.execute(\"INSERT OR IGNORE INTO users (id,password_hash,created_at,password_changed_at) VALUES ('Maya Chen','x','2026-01-01','2026-01-01'),('Jordan Lee','x','2026-01-01','2026-01-01')\")",
			"connection.execute(\"INSERT INTO auth_tokens(token_hash,user_id,kind,created_at) VALUES (?,'Maya Chen','session','2026-01-01'),(?,'Jordan Lee','session','2026-01-01')\", (sys.argv[2],sys.argv[3]))",
		], digest(mayaToken), digest(jordanToken));
		// The creator first takes the agent-owned document, making it shared through the product route.
		const share = await request.put(`/api/artifacts/${seeded.artifact}/ownership`, { params: { token: TEST_SESSION_TOKEN }, data: { owner: 'user' } });
		expect(share.ok(), await share.text()).toBeTruthy();
		const claim = await request.put(`/api/artifacts/${seeded.artifact}/ownership`, { params: { token: mayaToken }, data: { owner: 'user' } });
		expect(claim.ok(), await claim.text()).toBeTruthy();

		async function openAs(person: string, token: string): Promise<{ view: Page; close: () => Promise<void> }> {
			const made = await request.post('/api/threads', { params: { token }, data: { title: `${person} garden`, model: e2eModel() } });
			expect(made.ok(), await made.text()).toBeTruthy();
			const threadId = (await made.json()).thread_id as string;
			const context = await (browser as Browser).newContext({ viewport: { width, height } });
			await context.addInitScript(({ threadId, artifact, docPath }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: { tabs: [{ instanceId: 'shared', artifactId: artifact, path: docPath, title: 'Project planning.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'shared', visible: true, activeSurface: 'artifact' } } })), { threadId, artifact: seeded.artifact, docPath: seeded.docPath });
			const view = await context.newPage();
			await view.goto(`/?token=${encodeURIComponent(token)}`);
			await expect(view.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
			if (width < 768) await view.getByRole('button', { name: 'Toggle sidebar' }).click();
			await view.getByText(`${person} garden`, { exact: true }).first().click();
			await expect(ownerItem(view)).toBeVisible();
			return { view, close: () => context.close() };
		}

		const maya = await openAs('Maya', mayaToken);
		const jordan = await openAs('Jordan', jordanToken);
		try {
			await expect(ownerItem(jordan.view)).toHaveAccessibleName('Owner: Maya Chen');
			await ownerItem(jordan.view).click();
			await rolePicker(jordan.view).getByRole('option', { name: /^You/ }).click();
			await expect(ownerItem(jordan.view)).toHaveAccessibleName('Owner: You');
			await maya.view.reload();
			if (width < 768) await maya.view.getByRole('button', { name: 'Toggle sidebar' }).click();
			await maya.view.getByText('Maya garden', { exact: true }).first().click();
			await expect(ownerItem(maya.view)).toHaveAccessibleName('Owner: Jordan Lee');
			const body = `Jordan's garden comment ${label}`;
			await beginComment(jordan.view, 'Plan a community garden that neighbours can reach and enjoy throughout the year.', body);
			await submitButton(jordan.view).click();
			await expect(jordan.view.getByText(body, { exact: true })).toBeVisible();
			await expect.poll(() => JSON.parse(isolatedDb([
				"rows = connection.execute(\"SELECT b.recipient_id, b.state, b.delivered_node_id FROM doc_feedback_batches b JOIN doc_feedback_batch_items i ON i.batch_id=b.id JOIN doc_comments c ON c.id=i.comment_id WHERE b.artifact_id=? AND c.body=? ORDER BY b.recipient_id\", (sys.argv[2],sys.argv[3])).fetchall()",
				'print(json.dumps(rows))',
			], seeded.artifact, body))).toEqual([[seeded.agents[0], 'delivered', expect.any(String)]]);
		} finally {
			await jordan.close();
			await maya.close();
		}
	});
}
