import { readFileSync, writeFileSync } from 'node:fs';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { chatComposer, loginByUrl, sendButton, TEST_TOKEN } from '../support/corsia';
import { ownerItem, seedOwnedDocument } from '../support/documentOwnerSupervisor';
import { injectMessage, readMockCallCount, seedMockResponses } from '../support/mock';
import { designateConductor } from '../support/sharedLiveDocument';

test('agent gives the person a document and conductor suggests instead of editing', async ({ page, request }, info) => {
	 test.skip(info.project.name !== 'chromium-desktop', 'Desktop proof');
	 test.setTimeout(120_000);
	 const seeded = await seedOwnedDocument(page, request);
	 const header = '**Status:** current · **Owner:** Release planning · **Review:** annually';
	 const original = readFileSync(seeded.docPath, 'utf8');
	 writeFileSync(seeded.docPath, `${header}\n\n${original}`);
	 await seeded.open(page);
	 await expect(ownerItem(page)).toHaveAccessibleName('Owner: Release planning');
	 const response = await request.get('/api/files/content', { params: { token: TEST_TOKEN, path: seeded.docPath } });
	 expect(response.ok()).toBeTruthy();
	 const artifact = (await response.json()).artifact;
	 const before = readFileSync(seeded.docPath, 'utf8');
	 await seedMockResponses(request, [
		 { tool_calls: [{ id: 'give-person', name: 'artifact_rehome', arguments: JSON.stringify({ integration: artifact.integration, type: artifact.type, external_id: artifact.external_id, new_owner_thread_id: 'user', reason: 'This document belongs to the person.' }) }], finish_reason: 'tool_calls', match_thread: seeded.owner },
		 { content: 'Handed over.', finish_reason: 'stop', match_thread: seeded.owner },
	 ]);
	 await chatComposer(page).fill('Give this document to the person.');
	 await sendButton(page).click();
	 await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);
	 await seeded.open(page);
	 await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
	 const proof = process.env.CORSIA_PERSON_OWNER_PROOF;
	 const headerAfter = readFileSync(seeded.docPath, 'utf8').split('\n')[0];
	 expect(headerAfter).toBe(header);
	 if (proof) {
		 await page.screenshot({ path: `${proof}/person-owner.png` });
		 const ownerMessages = await (await request.get(`/api/threads/${seeded.owner}/messages`, { params: { token: TEST_TOKEN } })).json();
		 writeFileSync(`${proof}/handover-turn.json`, JSON.stringify(ownerMessages, null, 2));
		 writeFileSync(`${proof}/result.json`, JSON.stringify({ headerBefore: header, headerAfter, ownerBefore: 'Release planning', ownerAfter: await ownerItem(page).getAttribute('aria-label') }, null, 2));
	 }
	 const conductor = seeded.agents[0];
	 await injectMessage(request, conductor, { role: 'user', content: 'Please review the planning document.' }, TEST_TOKEN);
	 await designateConductor(request, conductor);
	 const agentPage = await page.context().newPage();
	 await loginByUrl(agentPage);
	 const conductorButton = agentPage.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: 'Research coordinator' });
	 await conductorButton.click();
	 await expect(conductorButton.locator('..')).toHaveClass(/\bactive\b/);
	 await expect(agentPage.getByText('Please review the planning document.', { exact: true })).toBeVisible();
	 await seedMockResponses(request, [
		 { tool_calls: [{ id: 'read-person-doc', name: 'read_file', arguments: JSON.stringify({ path: seeded.docPath }) }], finish_reason: 'tool_calls', match_thread: conductor },
		 { tool_calls: [{ id: 'edit-person-doc', name: 'edit_file', arguments: JSON.stringify({ path: seeded.docPath, old_string: 'Plan a community garden', new_string: 'Plan a rooftop garden' }) }], finish_reason: 'tool_calls', match_thread: conductor },
		 { tool_calls: [{ id: 'suggest-person-doc', name: 'doc_comment_create', arguments: JSON.stringify({ artifact_id: seeded.artifact, quote: 'Plan a community garden', replacement: 'Plan a rooftop garden' }) }], finish_reason: 'tool_calls', match_thread: conductor },
		 { content: 'Suggested.', finish_reason: 'stop', match_thread: conductor },
	 ]);
	 await chatComposer(agentPage).fill('Suggest the change.');
	 await sendButton(agentPage).click();
	 await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(4);
	 await expect.poll(async () => {
		 const response = await request.get(`/api/threads/${conductor}/messages`, { params: { token: TEST_TOKEN } });
		 return JSON.stringify(await response.json());
	 }, { timeout: 20_000 }).toContain('Suggested.');
	 const transcriptResponse = await request.get(`/api/threads/${conductor}/messages`, { params: { token: TEST_TOKEN } });
	 expect(transcriptResponse.ok()).toBeTruthy();
	 const transcript = await transcriptResponse.json();
	 const results: Record<string, unknown> = {};
	 for (const callId of ['read-person-doc', 'edit-person-doc', 'suggest-person-doc']) {
		 const message = transcript.messages.find((item: { metadata?: { tool_calls?: { id: string }[] } }) =>
			 item.metadata?.tool_calls?.some((call) => call.id === callId));
		 expect(message, `transcript contains ${callId}`).toBeTruthy();
		 const call = message.metadata.tool_calls.find((item: { id: string }) => item.id === callId);
		 if (callId === 'suggest-person-doc') expect(call.outcome).toBe('done');
		 const response = await request.get(`/api/threads/${conductor}/messages/${message.id}/tool-results/${callId}`, { params: { token: TEST_TOKEN } });
		 expect(response.ok(), `result for ${callId}`).toBeTruthy();
		 results[callId] = (await response.json()).result;
	 }
	 if (proof) {
		 writeFileSync(`${proof}/conductor-turn.json`, JSON.stringify(transcript, null, 2));
		 writeFileSync(`${proof}/conductor-results.json`, JSON.stringify(results, null, 2));
	 }
	 expect(JSON.stringify(results['read-person-doc'])).toContain(header);
	 expect(JSON.stringify(results['edit-person-doc'])).toContain('Only the person edits it directly.');
	 expect(readFileSync(seeded.docPath, 'utf8')).toBe(before);
	 await expect(page.locator('.cm-suggestion-insert')).toHaveAttribute('data-insert', 'Plan a rooftop garden', { timeout: 30_000 });
	 if (proof) {
		 await page.screenshot({ path: `${proof}/suggestion.png` });
		 writeFileSync(`${proof}/result.json`, JSON.stringify({ headerBefore: header, headerAfter, ownerBefore: 'Release planning', ownerAfter: await ownerItem(page).getAttribute('aria-label'), editRefused: true, suggestion: 'Plan a rooftop garden' }, null, 2));
	 }
	 await agentPage.close();
});
