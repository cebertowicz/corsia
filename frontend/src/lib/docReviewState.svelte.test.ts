import { afterEach, expect, it, vi } from 'vitest';
import { createDocumentReviewState } from './docReviewState.svelte';

afterEach(() => vi.unstubAllGlobals());

it('keeps taggable agents separate from review choices and clears them on target change', async () => {
	const taggable = [{ id: 'corsia', name: 'CORSIA', responsibility: '' }, { id: 'owner', name: 'Owner', responsibility: 'Document' }];
	const reviewers = [{ id: 'reviewer', name: 'Reviewer', responsibility: '', requested: false, unavailable_reason: null, unavailable_text: null }];
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ reviews: [], reviewers, taggable_agents: taggable }), { status: 200 })));
	const model = createDocumentReviewState();
	model.setTarget('doc', 'viewer', 'doc');
	await model.load();
	expect(model.taggableAgents).toEqual(taggable);
	expect(model.choices()).toEqual(reviewers);
	model.setTarget('other', 'viewer', 'other');
	expect(model.taggableAgents).toEqual([]);
});

it('keeps the reviewer list when only the tag roster failed, and the other way round', async () => {
	const reviewers = [{ id: 'reviewer', name: 'Reviewer', responsibility: '', requested: false, unavailable_reason: null, unavailable_text: null }];
	let body: object = { reviews: [], reviewers, taggable_agents_error: 'roster_error' };
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
	const model = createDocumentReviewState();
	model.setTarget('doc', 'viewer', 'doc');
	await model.load();
	expect([model.rosterError, model.tagRosterError, model.choices()]).toEqual([false, true, reviewers]);
	body = { reviews: [], reviewers_error: 'roster_error', taggable_agents: [] };
	await model.load();
	expect([model.rosterError, model.tagRosterError]).toEqual([true, false]);
});
