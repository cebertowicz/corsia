import fs from 'node:fs';
import path from 'node:path';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from './corsia';
import { syntheticPng } from './syntheticImages';
import { settled, uniqueTitle } from './reviewFixtures';

/**
 * "Fully loaded": one document at the comment drawer's worst realistic load, for any spec that proves the drawer holds up
 * under it. Two agents leave twelve threads and eight open suggestions (four each, so each has its own Accept all); three
 * threads run to sixteen replies; one comment and one reply are over 600 characters with a long unbroken URL; one
 * suggestion's replacement is over 400 characters; one thread is on a table cell and one on an image. Past the length at which
 * chat collapses a message (`isLongMessageText`, 900 characters): one unread agent comment, which must show whole, and one comment
 * of the person's own, read, which collapses, with an unread agent reply that must show whole.
 */

/** No hyphen, slash or other break opportunity for 160 characters: it can only wrap where the layout forces it. */
const LONG_URL = `https://example.com/reports/rollout?evidence=${'a1b2c3d4e5'.repeat(16)}`;
export const LONG_COMMENT = `The rollout plan says the second ring waits for the first, but the evidence for that is spread over three documents and a dashboard. ${LONG_URL} has the latest numbers, though it does not say which build they were measured on. Before we commit to the date, someone should confirm the build, the measurement window and who signed off on the error budget. Otherwise the team will argue about the numbers in the review instead of the decision itself, which is what happened last time and cost us a week.`;
export const LONG_REPLY = `Agreed on the build question. I checked the dashboard behind ${LONG_URL} and it reads from the nightly job, not the release candidate, so the numbers are from the wrong build. I have asked for a rerun on the candidate, which takes about a day. Until then I would keep the date tentative in the plan and say so in the summary, so nobody reads it as committed. I will update this thread with the rerun once it lands and link the exact build number so the review can settle it in one pass.`;
const MORE = ' One more thing for the record: the owners of the second ring asked for the dashboard link to be pinned in the plan, so the numbers from the rerun replace these in place rather than landing in a new thread that nobody finds. The summary should also say which of the three documents is the source of truth, because two of them disagree on the window and the third predates the budget change entirely.';
/** Over 900 characters, so only its being unread keeps it whole. */
export const LONG_UNREAD = `${LONG_COMMENT}${MORE}`;
export const LONG_UNREAD_REPLY = `${LONG_REPLY}${MORE}`;
/** The person's own comment, so never unread to them; over 900 characters, so it collapses with Show more. */
export const LONG_READ = `Which budget is this? ${LONG_COMMENT}${MORE}`;
export const LONG_REPLACEMENT = 'Ring two starts only after ring one has run for seven full days with an error rate under the agreed budget, the on-call team has signed off in writing, the support queue shows no new class of report, the dashboards for latency and saturation are green for the whole window, and the rollback drill has been rehearsed on the candidate build itself rather than on the previous release, so that the decision to proceed rests on evidence from the build that will actually ship.';

const TERMS = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel', 'india', 'juliet', 'kilo', 'lima', 'mike', 'november', 'oscar', 'papa', 'quebec', 'romeo', 'sierra', 'tango'];
const IMAGE = '![Launch screen](shots/screen.png)';
export const TABLE_CELL = 'navigation note stays in the cell';
export const TEXT = [
	'# Launch review', '',
	'The plan covers the launch in rings, and ring two waits for ring one to settle before it starts.', '',
	IMAGE, '',
	'| Area | Owner | Note |', '| --- | --- | --- |',
	`| Navigation | Dana | The ${TABLE_CELL} while the page scrolls. |`,
	'| Search | Lee | Results load within the budget. |', '',
	...TERMS.map((term, index) => `Step ${index + 1}: the ${term} milestone closes once its checks pass and the owner signs off.\n`),
].join('\n');

export interface LoadedDocument {
	ownerId: string; ownerTitle: string; artifactId: string; docPath: string;
	agents: [string, string];
	/** The three threads with sixteen replies each. */
	longThreads: string[];
	/** The thread whose root comment and one reply are over 600 characters. */
	longTextThread: string;
	/** An unread agent comment over 900 characters. */
	longUnreadThread: string;
	/** The person's own comment over 900 characters, read, so it collapses; its one reply, from an agent, is unread and over 900. */
	longReadThread: string;
	/** The suggestion with a replacement over 400 characters. */
	longSuggestion: string;
	tableThread: string; imageThread: string;
	suggestions: string[];
	threads: string[];
}

const params = { token: TEST_TOKEN };

export async function seedLoaded(request: APIRequestContext, label = 'loaded'): Promise<LoadedDocument> {
	const ownerTitle = uniqueTitle(`Comments ${label}`);
	const ownerId = await createSession(request, ownerTitle);
	const agents = [await createSession(request, uniqueTitle('Review Agent North')), await createSession(request, uniqueTitle('Review Agent South'))] as [string, string];
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const directory = path.join(workspace!, `loaded-${ownerId}`);
	fs.mkdirSync(path.join(directory, 'shots'), { recursive: true });
	fs.writeFileSync(path.join(directory, 'shots', 'screen.png'), syntheticPng(800, 500));
	const docPath = path.join(directory, 'launch-review.md');
	fs.writeFileSync(docPath, TEXT);
	const registered = await request.post('/api/files/register', { params, data: { path: docPath, thread_id: ownerId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;

	const asAgent = async (agent: number, data: Record<string, unknown>) => {
		const created = await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: artifactId, as_session_id: agents[agent], ...data } });
		expect(created.ok(), await created.text()).toBeTruthy();
		const result = await created.json();
		return (result.comment ?? result).id as string;
	};
	const asPerson = async (data: Record<string, unknown>) => {
		const created = await request.post(`/api/artifacts/${artifactId}/comments`, { params, data: { delivery_policy: 'immediate', ...data } });
		expect(created.ok(), await created.text()).toBeTruthy();
		return (await created.json()).comment.id as string;
	};
	const step = (n: number) => `${TERMS[n]} milestone`;

	// Twelve threads: ten from the agents on the text (one in a table cell), one on the image and one from the person.
	const threads: string[] = [];
	const longTextThread = await asAgent(0, { quote: 'ring two waits for ring one', body: LONG_COMMENT });
	threads.push(longTextThread);
	const tableThread = await asAgent(1, { quote: TABLE_CELL, body: 'Does this note still hold on the phone layout?' });
	threads.push(tableThread);
	for (let n = 0; n < 7; n++) threads.push(await asAgent(n % 2, { quote: step(n), body: `Is the ${TERMS[n]} owner confirmed for this step?` }));
	const from = TEXT.indexOf(IMAGE);
	const imageThread = await asPerson({ body: 'The title is hard to read on this screenshot.', anchor_text: IMAGE, from_off: from, to_off: from + IMAGE.length, region: { x: 0.15, y: 0.15, w: 0.4, h: 0.2 } });
	threads.push(imageThread);
	// An agent's comment is unread to the person only once the person has commented on the document, so this one comes after theirs.
	const longUnreadThread = await asAgent(0, { quote: step(7), body: LONG_UNREAD });
	threads.push(longUnreadThread);
	const own = TEXT.indexOf('Results load within the budget');
	const longReadThread = await asPerson({ body: LONG_READ, anchor_text: 'Results load within the budget', from_off: own, to_off: own + 'Results load within the budget'.length });
	threads.push(longReadThread);

	// Three of them run to sixteen replies, the agents and the person taking turns; one reply is over 600 characters.
	const longThreads = [threads[0], threads[2], threads[3]];
	for (const root of longThreads) {
		for (let index = 0; index < 16; index++) {
			const body = root === longTextThread && index === 3 ? LONG_REPLY : `Reply ${index + 1}: noted, and the owner will confirm before the review.`;
			if (index % 3 === 2) await asPerson({ body, parent_id: root });
			else await asAgent(index % 2, { parent_id: root, body });
		}
	}

	// An agent's reply on the person's own comment is unread to them.
	await asAgent(1, { parent_id: longReadThread, body: LONG_UNREAD_REPLY });

	// Eight open suggestions, four from each agent; one replacement is over 400 characters.
	const suggestions: string[] = [];
	for (let n = 8; n < 16; n++) {
		const replacement = n === 8 ? LONG_REPLACEMENT : `the ${TERMS[n]} milestone closes when its checks pass`;
		const quote = n === 8 ? 'Step 9: the india milestone closes once its checks pass and the owner signs off.' : `the ${TERMS[n]} milestone closes once its checks pass`;
		suggestions.push(await asAgent(n % 2, { quote, replacement, body: n === 8 ? 'Spell out the gate.' : 'Shorter.' }));
	}
	return { ownerId, ownerTitle, artifactId, docPath, agents, longThreads, longTextThread, longUnreadThread, longReadThread, longSuggestion: suggestions[0], tableThread, imageThread, suggestions, threads };
}

/** `canvas: 'wide'` drags the document's pane to its widest beside the chat, so a desktop canvas has room for the margin column. */
export type LoadedView = { width: number; height: number; theme: 'light' | 'dark'; touch: boolean; pane: 'open' | 'closed'; canvas?: 'wide' };

/** Opens the owner's thread with the document in its canvas, at the view's size and theme; `pane: 'closed'` collapses the side panel. */
export async function openLoaded(page: Page, doc: LoadedDocument, view: LoadedView) {
	await page.setViewportSize({ width: view.width, height: view.height });
	await page.addInitScript(({ doc, theme, wide }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.ownerId]: {
			tabs: [{ instanceId: 'loaded', artifactId: doc.artifactId, path: doc.docPath, title: 'launch-review.md', loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: 'loaded', visible: true, activeSurface: 'artifact',
		} }, splitWidth: wide ? 4000 : null }));
	}, { doc: { ownerId: doc.ownerId, artifactId: doc.artifactId, docPath: doc.docPath }, theme: view.theme, wide: view.canvas === 'wide' });
	await loginByUrl(page, { waitForComposer: false });
	if (view.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, doc.ownerTitle).click();
	await expect(page.locator('.cm-content')).toContainText('Launch review');
	if (view.width > 768) {
		// The side panel is beside the canvas at 1440 and over it at 900; either way it is open when it takes room on screen.
		const pane = page.locator('.pane');
		const isOpen = () => pane.evaluate((el) => el.getBoundingClientRect().width > 50 && el.checkVisibility({ visibilityProperty: true }));
		if ((await pane.count()) && (await isOpen()) !== (view.pane === 'open')) {
			await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
			await expect.poll(isOpen).toBe(view.pane === 'open');
		}
	}
	await settled(page);
}
