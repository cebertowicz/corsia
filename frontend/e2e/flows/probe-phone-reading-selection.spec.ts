import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

/**
 * PROBE ONLY — disposable branch `probe/phone-reading-selection-ci`. A measurement, not a feature: it asks what a phone
 * reader can actually get selected in a document, and what an input-resistant editor costs, under three editor settings.
 * Nothing here claims what SHOULD happen, and a red row is a result, not something to retry away.
 *
 * Arms (how they are applied, and why extension order decides whether they apply at all: `$lib/probePhoneReadingArms.ts`):
 * - `control`: today's editor. It MUST show a destructive edit, or a candidate's "nothing changed" proves nothing.
 * - `A`: `EditorView.editable=false` (coarse pointer) + `EditorState.readOnly=true`.
 * - `B`: `editable=true` + `readOnly=true` + `contentAttributes` `inputmode=none`.
 *
 * Three ways of getting a selection stay strictly apart, because they answer different questions:
 * 1. `native_long_press` — the browser's own touch selection. Chromium: CDP touch, the existing pattern
 *    (`doc-phone-tap-comment.spec.ts:147-153`). WebKit's driver taps and has no touch-event input, so a held press is
 *    reported unavailable and never relabelled.
 * 2. `dom_selection_api` — a Range set through the Selection API with no CodeMirror dispatch at all. Answers whether the
 *    platform's selection reaches the editor, and so whether the Comment offer can appear without the editor's say-so.
 * 3. `cm_forced_selection` — the editor's own selection, dispatched. The condition the input readings are taken under.
 *
 * What headless Linux cannot show stays UNKNOWN and is never written as green: no soft keyboard ever rises (so
 * `inputmode=none`'s whole purpose is unmeasurable — the attribute and focus are proxies only), and no native Copy/Cut menu
 * is ever drawn (so only the handlers below that menu are exercised).
 *
 * Every value crossing back from the page is plain data: `page.evaluate` serialises what a function returns and drops its
 * methods, so the editor's own methods are never handed to the test — each reading and each action is its own evaluate call.
 */

const TAG = '@probe:phone-reading @surface:documents';
const PROJ = ['probe-chromium-phone', 'probe-webkit-phone'];
const ARMS = ['control', 'A', 'B'] as const;
type Arm = (typeof ARMS)[number];

/** One prose fixture, byte-identical for every arm and engine; long enough to wrap at 393 and to lose characters visibly. */
const DOC = [
	'# Handover notes',
	'',
	'The conductors review every release before it ships, and the reviewer signs off on timing.',
	'',
	'Escalate blockers to the owner the same day. Nothing waits for a meeting.',
	'',
	'Selection probe target: bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november.',
	'',
	'This closing paragraph exists only to give the page some length to scroll.',
	'',
].join('\n');

const LEAD = 'The conductors';
const PHRASE = 'bravo charlie delta';

type Row = Record<string, unknown> & { arm: Arm | 'none'; engine: string; project: string; kind: string };
type ArmSettings = { arm: Arm; coarsePointer: boolean; editableApplied: boolean; readOnlyApplied: boolean; inputmodeApplied: boolean };
type EditorReading = {
	arm: Arm; settings: ArmSettings; readOnly: boolean; editable: boolean; docLength: number;
	selectionFrom: number; selectionTo: number; selectionEmpty: boolean;
	contentEditable: boolean; contentEditableAttribute: string | null; hasFocus: boolean;
};

const rows: Row[] = [];
const unknownsSeen = new Set<string>();
let matrixFile: string | undefined;

function emit(row: Row): Row {
	rows.push(row);
	if (matrixFile) fs.appendFileSync(matrixFile, `${JSON.stringify(row)}\n`);
	console.log(`PROBE_V1 ${JSON.stringify(row)}`);
	if (row.kind === 'unknown') unknownsSeen.add(`${row.engine}:${String(row.what)}`);
	return row;
}

/** One line per missing capability per engine, so the same gap reported 40 times does not hide its own count. */
function unknown(arm: Arm, engine: string, project: string, what: string, why: string, proxy: string): void {
	if (unknownsSeen.has(`${engine}:${what}`)) return;
	emit({ arm, engine, project, kind: 'unknown', what, why, proxy, green: false });
}

// ---- The editor bridge. Each call runs in the page and returns only data, or acts there and returns nothing.

const probeReady = (page: Page) => page.evaluate(() => Boolean((window as unknown as { __cmProbe?: unknown }).__cmProbe));
const probeSettings = (page: Page) => page.evaluate(() => {
	const api = (window as unknown as { __cmProbe?: { settings: ArmSettings } }).__cmProbe;
	return api ? api.settings : null;
});
const probeRead = (page: Page) => page.evaluate(() => {
	const api = (window as unknown as { __cmProbe?: { read(): EditorReading } }).__cmProbe;
	if (!api) throw new Error('__cmProbe is not published');
	return api.read();
});
const probeDoc = (page: Page) => page.evaluate(() => {
	const api = (window as unknown as { __cmProbe?: { doc(): string } }).__cmProbe;
	if (!api) throw new Error('__cmProbe is not published');
	return api.doc();
});
/** Forced CodeMirror selection: the editor's own selection, dispatched in the page. Not a touch, not a DOM selection. */
const probeForceSelection = (page: Page, from: number, to: number) => page.evaluate(({ from, to }) => {
	(window as unknown as { __cmProbe?: { forceCmSelection(from: number, to: number): void } }).__cmProbe?.forceCmSelection(from, to);
}, { from, to });
/** Focus the way the app focuses it, which is also what puts the editor in line to receive real keys. */
const probeFocus = (page: Page) => page.evaluate(() => {
	(window as unknown as { __cmProbe?: { focusEditor(): void } }).__cmProbe?.focusEditor();
});

// ---- Page-side observations, all plain data.

const domAttrs = (page: Page) => page.evaluate(() => {
	const content = document.querySelector('.cm-content');
	if (!content) return null;
	const names = ['contenteditable', 'inputmode', 'spellcheck', 'aria-readonly', 'role', 'autocorrect', 'autocapitalize'];
	const attrs: Record<string, string | null> = {};
	for (const name of names) attrs[name] = content.getAttribute(name);
	const active = document.activeElement;
	return { attrs, activeClass: active?.className ?? null, activeIsContent: active === content };
});

/** The emulation the probe stands on. `(pointer: coarse)` is measured, never assumed from a device name. */
const deviceEnv = (page: Page) => page.evaluate(() => ({
	coarse: matchMedia('(pointer: coarse)').matches,
	anyCoarse: matchMedia('(any-pointer: coarse)').matches,
	hover: matchMedia('(hover: hover)').matches,
	maxTouchPoints: navigator.maxTouchPoints,
	innerWidth: innerWidth,
	innerHeight: innerHeight,
	userAgent: navigator.userAgent,
}));

const clearDomSelection = (page: Page) => page.evaluate(() => document.getSelection()?.removeAllRanges());
const domSelection = (page: Page) => page.evaluate(() => {
	const sel = document.getSelection();
	if (!sel) return { present: false, rangeCount: 0, text: '', collapsed: true };
	return { present: true, rangeCount: sel.rangeCount, text: sel.toString(), collapsed: sel.isCollapsed };
});
const offerCount = (page: Page) => page.locator('button.sel-comment').count();

/** A Range over the rendered prose, placed by the Selection API alone. No CodeMirror dispatch of any kind. */
const setDomSelection = (page: Page, phrase: string) => page.evaluate((phrase) => {
	const content = document.querySelector('.cm-content');
	if (!content) return false;
	const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode(); node; node = walker.nextNode()) {
		const text = node.textContent ?? '', at = text.indexOf(phrase);
		if (at < 0) continue;
		const range = document.createRange();
		range.setStart(node, at); range.setEnd(node, at + phrase.length);
		const sel = document.getSelection()!;
		sel.removeAllRanges(); sel.addRange(range);
		return true;
	}
	return false;
}, phrase);

/** Every edit-path event the page sees, so "nothing changed" is told apart from "nothing was attempted". */
async function installEventRecorder(page: Page): Promise<void> {
	await page.addInitScript(() => {
		const held = window as unknown as {
			__probeEvents?: Array<Record<string, unknown>>;
			__probeSelectionChanges?: number;
			__probeDispatchResult?: Record<string, unknown>;
		};
		held.__probeEvents = [];
		const note = (event: Event) => {
			const detail: Record<string, unknown> = {
				type: event.type,
				inputType: (event as InputEvent).inputType ?? null,
				key: (event as KeyboardEvent).key ?? null,
				defaultPrevented: event.defaultPrevented,
				inCm: Boolean((event.target as Element | null)?.closest?.('.cm-editor')),
			};
			if (event.type === 'cut' || event.type === 'copy' || event.type === 'paste') {
				const clip = (event as ClipboardEvent).clipboardData;
				detail.hasClipboardData = Boolean(clip);
				detail.clipText = clip ? clip.getData('text/plain') : null;
			}
			held.__probeEvents!.push(detail);
		};
		// Capture, so a handler that stops propagation is still seen; on window, so an event the engine raises on the
		// document is not missed. selectionchange neither bubbles nor says where it came from, so it is only counted.
		for (const type of ['keydown', 'beforeinput', 'input', 'cut', 'copy', 'paste', 'compositionstart']) {
			window.addEventListener(type, note, true);
		}
		document.addEventListener('selectionchange', () => {
			held.__probeSelectionChanges = (held.__probeSelectionChanges ?? 0) + 1;
		});
	});
}

const readEvents = (page: Page) => page.evaluate(() => {
	const held = window as unknown as { __probeEvents?: Array<Record<string, unknown>>; __probeSelectionChanges?: number; __probeDispatchResult?: Record<string, unknown> };
	const events = held.__probeEvents ?? [];
	const dispatchResult = held.__probeDispatchResult ?? null;
	held.__probeEvents = [];
	held.__probeDispatchResult = undefined;
	return { events, selectionChanges: held.__probeSelectionChanges ?? 0, dispatchResult };
});

/**
 * Build and dispatch one edit event on the editor's content element, in the page, from data alone (a function handed to
 * `evaluate` would arrive as a serialised value, not a callable). Both outcomes are stored on the row: a dispatch that the
 * engine refused to even construct is a capability gap, and must not be read as the editor resisting an edit.
 */
type EditEventSpec =
	| { kind: 'paste'; text: string }
	| { kind: 'cut'; text: string }
	| { kind: 'beforeinput'; inputType: string };

const dispatchEditEvent = (page: Page, spec: EditEventSpec) => page.evaluate((spec) => {
	const held = window as unknown as { __probeDispatchResult?: Record<string, unknown> };
	const record = (result: Record<string, unknown>) => { held.__probeDispatchResult = result; return result; };
	try {
		let event: Event;
		if (spec.kind === 'beforeinput') {
			event = new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: spec.inputType });
		} else {
			const data = new DataTransfer();
			data.setData('text/plain', spec.text);
			event = new ClipboardEvent(spec.kind, { bubbles: true, cancelable: true, clipboardData: data });
		}
		const content = document.querySelector('.cm-content');
		if (!content) return record({ ok: false, error: 'no .cm-content on the page', spec });
		// dispatchEvent returns false once a handler cancels it, which is itself a reading.
		const notCancelled = content.dispatchEvent(event);
		return record({ ok: true, dispatched: true, type: event.type, inputType: (event as InputEvent).inputType ?? null, defaultPrevented: !notCancelled, spec });
	} catch (error) {
		return record({ ok: false, unsupported: true, error: String(error), spec });
	}
}, spec);

/** `document.execCommand('cut')`: a deprecated path with its own caveats, recorded with its return value. */
const execCut = (page: Page) => page.evaluate(() => {
	const held = window as unknown as { __probeDispatchResult?: Record<string, unknown> };
	let result: Record<string, unknown>;
	try {
		result = { ok: true, method: 'document.execCommand', command: 'cut', returned: document.execCommand('cut') };
	} catch (error) {
		result = { ok: false, unsupported: true, method: 'document.execCommand', error: String(error) };
	}
	held.__probeDispatchResult = result;
	return result;
});

// ---- Fixture seeding and opening, following the existing phone spec's pattern without touching it.

async function ready(page: Page): Promise<void> {
	await expect(page.locator('.cm-content').first(), 'the document editor is up').toBeVisible();
	await expect.poll(() => probeReady(page), { message: 'the probe published its view bridge' }).toBe(true);
}

async function openDocument(page: Page, arm: Arm): Promise<{ artifactId: string; docPath: string }> {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `probe-phone-${arm}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace, 'a workspace root to write the fixture into').toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	// Establish the app origin first, then set this fixture's state before reloading. Init scripts would accumulate across
	// fresh fixtures, and Playwright does not guarantee their execution order.
	await loginByUrl(page, { waitForComposer: false });
	await page.evaluate(({ threadId, docPath, fileName, artifactId, arm }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:probe-phone-arm', arm);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'probe-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'probe-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId, arm });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await ready(page);
	return { artifactId, docPath };
}

/** Character boxes of the line that starts with `lead`, in the real layout (the existing spec reads them the same way). */
const charRects = (page: Page, lead: string) => page.evaluate(({ lead }) => {
	const line = [...document.querySelectorAll('.cm-line')].find((l) => l.textContent?.startsWith(lead));
	if (!line) throw new Error(`no line starting with "${lead}"`);
	const out: Array<{ left: number; right: number; top: number; bottom: number }> = [];
	const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode(); node; node = walker.nextNode()) {
		for (let i = 0; i < node.textContent!.length; i += 1) {
			const range = document.createRange();
			range.setStart(node, i); range.setEnd(node, i + 1);
			const box = range.getClientRects()[0] ?? out[out.length - 1];
			if (box) out.push({ left: box.left, right: box.right, top: box.top, bottom: box.bottom });
		}
	}
	if (!out.length) throw new Error(`"${lead}" has no measurable character boxes`);
	return out;
}, { lead });

/** Where the probe phrase sits in the editor's own text, and where it sits on screen. */
async function locate(page: Page): Promise<{ from: number; to: number; x: number; y: number }> {
	const doc = await probeDoc(page);
	const from = doc.indexOf(PHRASE);
	expect(from, `the fixture holds "${PHRASE}"`).toBeGreaterThan(-1);
	type Point = { x: number; y: number; inViewport: boolean; hitContent: boolean; hitText: string | null };
	let point: Point | undefined;
	let stableFrames = 0;
	// scrollIntoView dispatch schedules CodeMirror's measurement on rAF. Read on distinct frames until the same
	// valid target survives two frame transitions, bounded at 5s; never reuse pre-scroll coordinates.
	await expect.poll(async () => {
		await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
		const next = await page.evaluate((phrase) => {
			const content = document.querySelector('.cm-content');
			if (!content) throw new Error('missing editor content');
			const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				const at = (node.textContent ?? '').indexOf(phrase);
				if (at < 0) continue;
				const index = at + Math.floor(phrase.length / 2);
				const range = document.createRange();
				range.setStart(node, index);
				range.setEnd(node, index + 1);
				const rect = range.getBoundingClientRect();
				const x = (rect.left + rect.right) / 2;
				const y = (rect.top + rect.bottom) / 2;
				const hit = document.elementFromPoint(x, y);
				return { x, y, inViewport: x > 0 && x < innerWidth && y > 0 && y < innerHeight, hitContent: Boolean(hit && content.contains(hit)), hitText: hit?.textContent ?? null };
			}
			throw new Error('phrase has no rendered text node');
		}, PHRASE);
		const valid = next.inViewport && next.hitContent && Boolean(next.hitText?.includes(PHRASE));
		stableFrames = valid && point?.x === next.x && point?.y === next.y ? stableFrames + 1 : 0;
		point = next;
		return stableFrames >= 2;
	}, { timeout: 5_000, intervals: [0], message: 'phrase target settles after CodeMirror scrolling' }).toBe(true);
	if (!point) throw new Error('no measured target');
	expect(point.inViewport, 'target is inside both viewport axes').toBe(true);
	expect(point.hitContent, 'hit target belongs to editor content').toBe(true);
	expect(point.hitText, 'hit target contains the measured phrase').toContain(PHRASE);
	return { from, to: from + PHRASE.length, x: point.x, y: point.y };
}

/** A press held still through Chromium's own touch input (the existing long-press pattern). Chromium only. */
async function cdpLongPress(page: Page, ms = 900): Promise<{ x: number; y: number }> {
	const session = await page.context().newCDPSession(page);
	try {
		const { x, y } = await locate(page);
		await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }] });
		try { await page.waitForTimeout(ms); }
		finally { await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); }
		return { x, y };
	} finally { await session.detach(); }
}

/** Where the text differs, so a row says what was lost rather than only that something was. */
function describe(before: string, after: string): { from: number; gone: string; added: string } {
	let i = 0;
	while (i < before.length && i < after.length && before[i] === after[i]) i += 1;
	let tail = 0;
	while (tail < before.length - i && tail < after.length - i && before[before.length - 1 - tail] === after[after.length - 1 - tail]) tail += 1;
	return { from: i, gone: before.slice(i, before.length - tail), added: after.slice(i, after.length - tail) };
}

/**
 * One input attempt: the editor's text before and after, the events the page saw, and whether the attempt was even
 * possible to make. `proof` says what kind of evidence the row can carry, so a synthetic event is never read as a finger.
 */
async function attempt(
	page: Page, arm: Arm, engine: string, project: string, kind: string,
	selection: string, proof: string, setup: () => Promise<void>, action: () => Promise<void>,
): Promise<Row> {
	await openDocument(page, arm);
	await setup();
	const before = await probeDoc(page);
	expect(before, `fresh fixture before ${selection}/${kind}`).toBe(DOC);
	const stateBefore = await probeRead(page);
	const domBefore = await domSelection(page);
	expect(stateBefore.arm).toBe(arm);
	expect(stateBefore.readOnly).toBe(arm !== 'control');
	if (selection === 'cm_forced_selection') {
		expect([stateBefore.selectionFrom, stateBefore.selectionTo]).toEqual([DOC.indexOf(PHRASE), DOC.indexOf(PHRASE) + PHRASE.length]);
	} else {
		expect(domBefore.text).toBe(PHRASE);
	}
	await readEvents(page); // Drop what the selection step itself produced, so this row's events are this action's.
	let actionThrew: string | null = null;
	try { await action(); } catch (error) { actionThrew = String(error); }
	await page.waitForTimeout(120);
	const after = await probeDoc(page);
	const stateAfter = await probeRead(page);
	const { events, selectionChanges, dispatchResult } = await readEvents(page);
	const cutEvent = events.some((event) => event.type === 'cut' && event.inCm === true);
	const unknownCut = kind === 'cut_execcommand' && dispatchResult?.returned === false && !cutEvent && before === after;
	return emit({
		arm, engine, project, kind, selection, proof, unknown: unknownCut, cutEvent,
		textBefore: before.length, textAfter: after.length, changed: before !== after,
		delta: after.length - before.length,
		removed: before === after ? null : describe(before, after),
		cmSelectionBefore: [stateBefore.selectionFrom, stateBefore.selectionTo], domBefore, stateBefore, stateAfter,
		cmSelectionAfter: [stateAfter.selectionFrom, stateAfter.selectionTo],
		focused: stateAfter.hasFocus, events, selectionChanges, dispatchResult, actionThrew,
	});
}

const engineOf = (project: string) => (project.includes('webkit') ? 'webkit' : 'chromium');

test.beforeEach(({}, testInfo) => {
	test.skip(!PROJ.includes(testInfo.project.name), 'Runs only in the probe projects, which set the phone geometry and touch');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires the isolated mock backend');
	const arm = ARMS.find((candidate) => testInfo.title.startsWith(`arm ${candidate}:`));
	if (!arm) throw new Error(`Unrecognized probe test: ${testInfo.title}`);
	const project = testInfo.project.name, engine = engineOf(project);
	rows.length = 0;
	unknownsSeen.clear();
	matrixFile = path.join(process.cwd(), 'test-results', `probe-${project}-${arm}-attempt-${testInfo.retry}-${randomUUID()}.jsonl`);
	fs.mkdirSync(path.dirname(matrixFile), { recursive: true });
	fs.writeFileSync(matrixFile, '');
	emit({ arm, engine, project, kind: 'started', attempt: testInfo.retry, fixture: DOC, expectedRows: ['settings', 'native_long_press', 'dom_selection_api', 'cm_forced_selection', 'key_backspace/dom_selection_api', 'key_printable/dom_selection_api', 'key_backspace/cm_forced_selection', 'key_printable/cm_forced_selection', 'paste/cm_forced_selection', 'cut_execcommand/cm_forced_selection', 'cut_synthetic_event/cm_forced_selection', 'beforeinput_deleteContentBackward/cm_forced_selection', 'beforeinput_deleteByCut/cm_forced_selection'] });
});

// Independent tests, not a serial describe: a red control must not stop arms A and B from being measured.
// Order is declaration order, which the suite's `workers: 1, fullyParallel: false` keeps (playwright.config.ts).
test.describe(`${TAG} phone reading selection arms`, () => {
	test.describe.configure({ retries: 0 });

	for (const arm of ARMS) test(`arm ${arm}: selection reach and input resistance`, async ({ page }, testInfo) => {
		test.setTimeout(600_000);
		const project = testInfo.project.name, engine = engineOf(project);
		await installEventRecorder(page);
		await openDocument(page, arm);
		await page.waitForTimeout(300); // Let the load-time list and any focus land before anything is measured.

		const settings = await probeSettings(page);
		const reading = await probeRead(page);
		const env = await deviceEnv(page);
		const dom = await domAttrs(page);
		emit({
			arm, engine, project, kind: 'settings', settings, facets: reading, dom, env,
			fixture: { chars: DOC.length, lines: DOC.split('\n').length, docMatchesFixture: reading.docLength === DOC.length },
		});
		// Settings gate everything after them: an arm that did not apply makes every later row meaningless.
		expect(env.innerWidth, 'the phone width the probe is defined at').toBe(393);
		expect(env.innerHeight, 'the phone height the probe is defined at').toBe(852);
		expect(await probeDoc(page), 'exact fixture contents').toBe(DOC);
		expect(reading.docLength, 'the same fixture text in the same editor, every arm and engine').toBe(DOC.length);
		expect(settings, 'the probe published its arm settings').not.toBeNull();
		if (arm === 'control') {
			expect(reading.readOnly, "the control is today's editor").toBe(false);
			expect(reading.editable, "the control is today's editor").toBe(true);
			expect(dom?.attrs.contenteditable, "the control's content element stays editable in the DOM").toBe('true');
		} else {
			expect(reading.readOnly, `arm ${arm} sets EditorState.readOnly=true (first provider wins, so placement matters)`).toBe(true);
			expect(settings?.readOnlyApplied, `arm ${arm}'s readOnly was applied`).toBe(true);
			if (arm === 'A') {
				if (env.coarse) {
					expect(reading.editable, 'arm A sets EditorView.editable=false on a coarse pointer').toBe(false);
					expect(dom?.attrs.contenteditable, 'contenteditable follows the editable facet').toBe('false');
					expect(reading.contentEditable, 'the DOM the browser acts on agrees with the facet').toBe(false);
				} else {
					unknown(arm, engine, project, 'arm A on a coarse pointer', 'the emulated device did not report (pointer: coarse), so A applied no editable=false and this arm read as today\'s editor plus readOnly', 'the facet values on the settings row');
				}
			}
			if (arm === 'B') {
				expect(reading.editable, 'arm B leaves editable true').toBe(true);
				expect(dom?.attrs.inputmode, "arm B's contentAttributes inputmode reaches the content element additively").toBe('none');
				expect(dom?.attrs.spellcheck, "inputmode is added, not substituted for the editor's own attribute").toBe('true');
			}
			unknown(arm, engine, project, 'soft keyboard rise', 'a headless Linux browser has no virtual keyboard, so nothing here can show whether a keyboard stays down: the attribute and focus are proxies only, not the behaviour', 'inputmode attribute and focus state');
			unknown(arm, engine, project, 'native selection menu', 'no native Copy/Cut menu is drawn in a headless Linux browser, so only the handlers under the menu are exercised', 'cut/copy handlers and execCommand rows');
		}

		const from = DOC.indexOf(PHRASE), to = from + PHRASE.length;

		// --- 1. Scroll first, then measure the same phrase immediately before touch.
		await clearDomSelection(page);
		await probeForceSelection(page, from, from);
		let pressedAt: { x: number; y: number } | null = null;
		await readEvents(page);
		const touch: Row = { arm, engine, project, kind: 'native_long_press', attempted: false, route: 'unavailable' };
		let touchError: string | null = null;
		if (engine === 'chromium') {
			touch.attempted = true;
			touch.route = 'CDP Input.dispatchTouchEvent, held 900ms (existing pattern: doc-phone-tap-comment.spec.ts:147-153)';
			try { pressedAt = await cdpLongPress(page); } catch (error) { touchError = String(error); }
		} else {
			unknown(arm, engine, project, 'WebKit held touch', 'Playwright\'s WebKit driver taps and has no touch-event input, so a held press cannot be issued; this is not a negative result about a real phone', 'a plain tap, recorded on this row');
			// The closest the engine does support: a real tap. Recorded, and never labelled a long press.
			try { pressedAt = await locate(page); await page.touchscreen.tap(pressedAt.x, pressedAt.y); touch.tapInstead = true; } catch (error) { touchError = String(error); }
		}
		await page.waitForTimeout(400);
		emit(Object.assign(touch, {
			pressedAt, touchError,
			dom: await domSelection(page), cm: await probeRead(page), offer: await offerCount(page),
			...(await readEvents(page)),
		}));

		// --- 2. The DOM Selection API alone: does the platform's selection reach the editor, and the offer?
		await clearDomSelection(page);
		await probeForceSelection(page, from, from);
		await clearDomSelection(page);
		await readEvents(page);
		const applied = await setDomSelection(page, PHRASE);
		await page.waitForTimeout(400);
		emit({
			arm, engine, project, kind: 'dom_selection_api', applied,
			dom: await domSelection(page), cm: await probeRead(page), offer: await offerCount(page),
			...(await readEvents(page)),
		});
		expect(applied, 'the fixture phrase sits in one text node the Selection API can reach').toBe(true);

		// An edit attempted from a browser selection the editor never agreed to: the state a platform menu would be in.
		// Focus first, because a key has to reach the editor to mean anything; then the selection is placed again.
		await attempt(page, arm, engine, project, 'key_backspace', 'dom_selection_api', 'real key event through the engine\'s input pipeline', async () => {
			await probeFocus(page);
			expect(await setDomSelection(page, PHRASE), 'the selection is placed after focus').toBe(true);
		}, async () => { await page.keyboard.press('Backspace'); });
		await attempt(page, arm, engine, project, 'key_printable', 'dom_selection_api', 'real key event through the engine\'s input pipeline', async () => {
			await probeFocus(page);
			expect(await setDomSelection(page, PHRASE), 'the selection is placed after focus').toBe(true);
		}, async () => { await page.keyboard.type('Z'); });

		// --- 3. Use a fresh fixture after the independent DOM-selection input attempts.
		await openDocument(page, arm);
		expect(await probeDoc(page)).toBe(DOC);
		await clearDomSelection(page);
		await readEvents(page);
		await probeForceSelection(page, from, to);
		await page.waitForTimeout(400);
		const forcedCm = await probeRead(page);
		emit({
			arm, engine, project, kind: 'cm_forced_selection',
			dom: await domSelection(page), cm: forcedCm, offer: await offerCount(page),
			...(await readEvents(page)),
		});
		expect(forcedCm.selectionTo, "the forced selection is the editor's own selection").toBe(to);
		await saveEvidenceScreenshot(page, testInfo, `${arm}-${project}-cm-selection`);

		const selectAgain = async () => { await probeForceSelection(page, from, to); await page.waitForTimeout(80); };

		// Each kind of input, labelled by what its proof can and cannot show. A real key goes through the engine's input
		// path; a synthetic event only reaches handlers, and a no-op event changes nothing in ANY engine.
		await attempt(page, arm, engine, project, 'key_backspace', 'cm_forced_selection', 'real key event through the engine\'s input pipeline', async () => { await probeFocus(page); await selectAgain(); }, async () => {
			await page.keyboard.press('Backspace');
		});
		await attempt(page, arm, engine, project, 'key_printable', 'cm_forced_selection', 'real key event through the engine\'s input pipeline', async () => { await probeFocus(page); await selectAgain(); }, async () => {
			await page.keyboard.type('Z');
		});
		await attempt(page, arm, engine, project, 'paste', 'cm_forced_selection', 'synthetic ClipboardEvent reaching the paste handler: handler proof only, not the platform paste menu', async () => { await probeFocus(page); await selectAgain(); }, async () => {
			await dispatchEditEvent(page, { kind: 'paste', text: 'PASTED' });
		});
		await attempt(page, arm, engine, project, 'cut_execcommand', 'cm_forced_selection', 'document.execCommand("cut"): deprecated path, its return value recorded; no native menu involved', async () => { await probeFocus(page); await selectAgain(); }, async () => {
			await execCut(page);
		});
		await attempt(page, arm, engine, project, 'cut_synthetic_event', 'cm_forced_selection', 'synthetic ClipboardEvent("cut") reaching the cut handler: event-handler proof only', async () => { await probeFocus(page); await selectAgain(); }, async () => {
			await dispatchEditEvent(page, { kind: 'cut', text: 'ignored' });
		});
		for (const inputType of ['deleteContentBackward', 'deleteByCut']) {
			await attempt(page, arm, engine, project, `beforeinput_${inputType}`, 'cm_forced_selection', 'synthetic beforeinput dispatch: event-handler proof only. A no-op event changes nothing in ANY engine, so unchanged text here is not resistance proof', async () => { await probeFocus(page); await selectAgain(); }, async () => {
				await dispatchEditEvent(page, { kind: 'beforeinput', inputType });
			});
		}

		// The control must destroy something, or every candidate row that reads "no change" is meaningless.
		const own = rows.filter((r) => r.arm === arm && typeof r.changed === 'boolean');
		if (arm === 'control') {
			const realKey = own.filter((r) => String(r.proof).startsWith('real key') && (r.kind === 'key_backspace' || r.kind === 'key_printable'));
			expect(realKey.length, 'the control was asked for real-key edits').toBeGreaterThan(0);
			expect(realKey.some((r) => r.changed === true), "the control must show a destructive edit, or a candidate's unchanged text proves nothing").toBe(true);
		} else {
			// Soft, so a candidate that does NOT resist is still recorded in the matrix and reported rather than aborting the
			// rows after it. A soft failure still reddens the run: it is a finding, not something to retry away.
			for (const row of own) {
				const dispatch = row.dispatchResult as Record<string, unknown> | null;
				// beforeinput remains handler-only evidence; still flag any observed mutation in a candidate.
				if (dispatch?.unsupported === true || row.actionThrew || row.unknown === true) {
					unknown(arm, engine, project, `${String(row.kind)} on ${engine}`, `the attempt itself was refused (${String(dispatch?.error ?? row.actionThrew)}), so the row says nothing either way about resistance`, 'the rows that did run');
					continue;
				}
				const degraded = arm === 'A' && !env.coarse ? ' [degraded: no coarse pointer, so A applied no editable=false]' : '';
				expect.soft(row.changed, `${String(row.kind)} (${String(row.proof)}) must leave the text alone in ${arm}${degraded}`).toBe(false);
			}
		}
	});
});

test.afterEach(async ({}, testInfo) => {
	if (!matrixFile) return;
	fs.appendFileSync(matrixFile, `${JSON.stringify({ kind: 'test_end', status: testInfo.status, expectedStatus: testInfo.expectedStatus, errors: testInfo.errors.map((error) => error.message) })}\n`);
	console.log(`PROBE_MATRIX ${matrixFile} rows=${rows.length}`);
	matrixFile = undefined;
});
