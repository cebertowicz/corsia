/**
 * PROBE ONLY — this file exists on the disposable branch `probe/phone-reading-selection-ci` to measure how a phone reader
 * gets a selection in a document, and what each setting costs, under three editor settings. It must never merge: a real
 * candidate goes through the approved product path, where the setting is a decision, not a localStorage flag, and the
 * window hook below would be a hole in a shipped app.
 *
 * The arms are applied inside the document's own editor construction (`MarkdownEditor.svelte`), so what the probe observes
 * is the composed surface the person uses — the Comment offer of `DocViewerEditorSurface.svelte:189` included — and not a
 * substitute minimal editor.
 *
 * - `control`: nothing added. Today's editor: `EditorView.editable` default true, `EditorState.readOnly` false.
 *   Kept as the destructive control: it must show an edit landing, or a "no change" reading on a candidate means nothing.
 * - `A`: `EditorView.editable.of(false)` on a coarse pointer, plus `EditorState.readOnly.of(true)`.
 * - `B`: `EditorView.editable.of(true)`, `EditorState.readOnly.of(true)`, and `contentAttributes` `inputmode=none`.
 *
 * Order is load-bearing, and is why `MarkdownEditor.svelte` spreads these BEFORE its `roComp.of(...)`:
 * `EditorState.readOnly` combines as `values.length ? values[0] : false` (@codemirror/state@6.6.0 dist/index.js:2032), so
 * a `readOnly.of(true)` placed after `roComp`'s `readOnly.of(false)` is silently ignored — an arm that reads as resistant
 * while being editable. `EditorView.editable` combines the same way, defaulting true (@codemirror/view@6.43.1
 * dist/index.js:1373). `contentAttributes` has no combine and merges additively (same file:1511, applied at :8233), so
 * `inputmode` joins the editor's own `spellcheck` instead of replacing it.
 */
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { coarsePointer } from '$lib/selectionButtonPlace';

/** Set by the probe spec before the app loads. Absent or unrecognised: the editor is today's, untouched. */
export const PROBE_ARM_KEY = 'corsia:probe-phone-arm';

export type ProbeArm = 'control' | 'A' | 'B';

const ARMS: readonly ProbeArm[] = ['control', 'A', 'B'];

/** The arm this page was loaded for, or null when the probe flag is not set (every ordinary run and every other spec). */
export function probeArm(): ProbeArm | null {
	try {
		const raw = localStorage.getItem(PROBE_ARM_KEY);
		return ARMS.includes(raw as ProbeArm) ? (raw as ProbeArm) : null;
	} catch { return null; } // No localStorage (SSR, or a storage-blocked context): no arm.
}

/** What the arm claims to set, and whether its own condition held. Reported, so a degraded arm is not read as a result. */
export type ProbeArmSettings = {
	arm: ProbeArm;
	/** A's `editable=false` is a phone-only candidate: it is applied only where the existing coarse-pointer test matches. */
	coarsePointer: boolean;
	editableApplied: boolean;
	readOnlyApplied: boolean;
	inputmodeApplied: boolean;
};

export function probeArmSettings(arm: ProbeArm): ProbeArmSettings {
	const coarse = coarsePointer();
	return {
		arm,
		coarsePointer: coarse,
		editableApplied: arm === 'A' && coarse,
		readOnlyApplied: arm === 'A' || arm === 'B',
		inputmodeApplied: arm === 'B',
	};
}

/**
 * The arm's extensions, in the order they must enter the editor's extension array. `control` adds nothing, which is the
 * point: the control is the current editor, not a reconstruction of it.
 */
export function probeArmExtensions(arm: ProbeArm | null): Extension[] {
	if (!arm || arm === 'control') return [];
	const settings = probeArmSettings(arm);
	const out: Extension[] = [];
	// First provider wins for both facets, so these must precede `roComp` — see the header.
	if (settings.editableApplied) out.push(EditorView.editable.of(false));
	if (settings.readOnlyApplied) out.push(EditorState.readOnly.of(true));
	if (settings.inputmodeApplied) out.push(EditorView.contentAttributes.of({ inputmode: 'none' }));
	return out;
}

// PROBE ONLY (probe/phone-reading-selection-ci): measures three editor settings on a phone; see $lib/probePhoneReadingArms.ts. Never merges.
export function createPhoneProbe(): { extensions: Extension[]; publish(view: EditorView): void } {
	// PROBE ONLY: null on every ordinary page, so the extension array below is today's on every run but a probe run.
	const arm = probeArm();
	return {
		extensions: probeArmExtensions(arm),
		publish(view) { if (arm) probePublish(view, arm); },
	};
}

/** What the live editor says about itself. Read back from the real state and DOM, never assumed from the arm. */
export type ProbeEditorReading = {
	arm: ProbeArm;
	settings: ProbeArmSettings;
	/** `state.readOnly` — the facet the editor's own code reads (`cm/email-header.ts:46` reads it the same way). */
	readOnly: boolean;
	/** `state.facet(EditorView.editable)`; true is CodeMirror's default, so an arm that never applied reads as true. */
	editable: boolean;
	docLength: number;
	selectionFrom: number;
	selectionTo: number;
	selectionEmpty: boolean;
	/** CodeMirror's own answer to whether this view takes focus and input, read off the DOM it owns. */
	contentEditable: boolean;
	contentEditableAttribute: string | null;
	hasFocus: boolean;
};

export type ProbeEditorApi = {
	arm: ProbeArm;
	settings: ProbeArmSettings;
	/** The document text, from the editor's own state — the fixture check the arms share. */
	doc(): string;
	read(): ProbeEditorReading;
	/**
	 * A forced CodeMirror selection: the editor's own selection, dispatched. This is NOT a touch and NOT a DOM selection;
	 * it is the path a programmatic selection takes, and it is what the input-resistance readings are conditioned on.
	 */
	forceCmSelection(from: number, to: number): void;
	/** Focus the editor the way the app does when it wants the caret in the text. */
	focusEditor(): void;
};

declare global {
	interface Window {
		__cmProbe?: ProbeEditorApi;
	}
}

/**
 * Publish the probe's view hook. Called from `onMount` right after the view exists, and only when the arm flag is set, so
 * no ordinary page ever carries `window.__cmProbe`.
 */
export function probePublish(view: EditorView, arm: ProbeArm): void {
	const settings = probeArmSettings(arm);
	const read = (): ProbeEditorReading => {
		const state = view.state, selection = state.selection.main;
		return {
			arm, settings,
			readOnly: state.readOnly,
			editable: state.facet(EditorView.editable),
			docLength: state.doc.length,
			selectionFrom: selection.from, selectionTo: selection.to, selectionEmpty: selection.empty,
			// @codemirror/view@6.43.1 exposes only the static facet (index.d.ts:1284), not an instance `editable`, so the
			// DOM the browser acts on is read here and the facet is read above; the two together are the setting.
			contentEditable: view.contentDOM.isContentEditable,
			contentEditableAttribute: view.contentDOM.getAttribute('contenteditable'),
			hasFocus: view.hasFocus,
		};
	};
	window.__cmProbe = {
		arm, settings,
		doc: () => view.state.doc.toString(),
		read,
		forceCmSelection: (from, to) => { view.dispatch({ selection: { anchor: from, head: to }, scrollIntoView: true }); },
		focusEditor: () => { view.focus(); },
	};
}
