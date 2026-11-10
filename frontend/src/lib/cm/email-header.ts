import { EditorState, Facet, StateField, Transaction, type ChangeSpec, type Extension, type Range } from '@codemirror/state';
import { Decoration, EditorView, type DecorationSet } from '@codemirror/view';
import { currentRecipients, headerLayout, type ServerHeader } from '$lib/emailDraft';
import { showAutolinkMarks } from './markdown-live';
import { emailHeaderTheme } from './email-header-theme';
import {
	AddedMarkWidget, AddressHintWidget, AttachmentsWidget, CcButtonWidget, FromWidget, QuietNotesWidget,
	type MountAttachments,
} from './email-header-widgets';

export type { MountAttachments } from './email-header-widgets';

// The compact header block of an email draft, drawn over the draft's own text: the header lines stay the document's
// text (editing them edits the draft), only their look changes. The lines are short and muted-labelled, a divider
// closes them, and the body below keeps the document's reading size. Notes live on the draft record and are drawn
// under the header. The `From:` line is the sending account: shown, not editable.

/** Fields whose value is an address list (or a line being typed): at every width the value wraps, its continuation under the value column. */
const ADDRESS_FIELDS = new Set(['to', 'cc']);

/** How many characters at the end of an added address always stay on one line with its "!". */
const ADDED_TAIL = 6;

export interface EmailHeaderOptions {
	/** The quiet notes, folded into one line under the header. */
	quiet: readonly string[];
	/** Lowercase addresses on the email being answered; an address in To or Cc that is not one of them is marked as added. Null: nothing to compare with. */
	answered: readonly string[] | null;
	/** The header as the server last read it: which To and Cc entries the send takes, and where they are. */
	header: ServerHeader | null;
	/** Whether the server reads the header again after each edit (live editing). Without it, an edit drops the old reading's marks. */
	rereads: boolean;
	/** Identifies the attachments drawn; the row is redrawn when it changes. */
	attachmentsKey: string;
	/** Draws the attachment chips; none means no row. */
	mountAttachments: MountAttachments | null;
}

function build(state: EditorState, { quiet, attachmentsKey, mountAttachments }: EmailHeaderOptions): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = state;
	// The header is every line up to the first blank line, as the backend's parse_document reads it, so a line being typed
	// (`C` on its way to `Cc:`) keeps the header look and the fields below it do not jump to body size.
	const { lines } = headerLayout(doc.iterLines());
	// The Cc button shows while the header has no Cc line and the draft can be edited; once the line is there it is the field.
	const editable = !state.readOnly && state.facet(EditorView.editable);
	const ccButtonAt = !lines.some((line) => line.key === 'cc') && editable ? lines.find((line) => line.key === 'to') : undefined;
	let lastShown: { from: number; to: number } | null = null;
	for (const line of lines) {
		const { key } = line;
		lastShown = line;
		const value = doc.sliceString(line.valueFrom, line.to);
		if (key === 'from') {
			decos.push(Decoration.replace({ widget: new FromWidget(value.trim()), block: true }).range(line.from, line.to));
			continue;
		}
		const ccButton = ccButtonAt === line ? ' cm-email-head-to-cc' : '';
		// An empty Cc line is an open field: outlined, with a hint, while the draft can be edited.
		const emptyCc = key === 'cc' && !value.trim() && editable;
		decos.push(Decoration.line({ class: `cm-email-head ${!key || ADDRESS_FIELDS.has(key) ? 'cm-email-head-address' : ''}${ccButton}${emptyCc ? ' cm-email-head-empty' : ''}` }).range(line.from));
		if (ccButton) decos.push(Decoration.widget({ widget: new CcButtonWidget(), side: 1 }).range(line.to));
		if (emptyCc) decos.push(Decoration.widget({ widget: new AddressHintWidget(), side: 1 }).range(line.to));
		if (key) decos.push(Decoration.mark({ class: 'cm-email-head-label' }).range(line.from, line.valueFrom));
	}
	if (!lastShown) return Decoration.none;
	const last = lastShown;
	if (quiet.length) {
		// The divider closes the quiet line instead of the last field.
		decos.push(Decoration.line({ class: 'cm-email-head-before-notes' }).range(last.from));
		decos.push(Decoration.widget({ widget: new QuietNotesWidget(quiet), block: true, side: 1 }).range(last.to));
	} else {
		decos.push(Decoration.line({ class: 'cm-email-head-last' }).range(last.from));
	}
	// The line number just after the header: the blank line, when there is one.
	const end = lines.length + 1;
	if (mountAttachments) {
		// Directly below the body, above the signature divider (`-- `), or at the end when there is no signature.
		let signature = -1;
		for (let n = doc.lines; n > end; n--) if (/^--\s?$/.test(doc.line(n).text)) { signature = doc.line(n).from; break; }
		const widget = new AttachmentsWidget(attachmentsKey, mountAttachments);
		decos.push(signature >= 0 ? Decoration.widget({ widget, block: true, side: -1 }).range(signature) : Decoration.widget({ widget, block: true, side: 1 }).range(doc.length));
	}
	// The blank line after the header is the gap between the divider and the body.
	if (end <= doc.lines && doc.line(end).text === '') decos.push(Decoration.line({ class: 'cm-email-head-gap' }).range(doc.line(end).from));
	return Decoration.set(decos, true);
}

function field(options: EmailHeaderOptions) {
	return StateField.define<DecorationSet>({
		create: (state) => build(state, options),
		update: (deco, tr) => (tr.docChanged || tr.reconfigured ? build(tr.state, options) : deco),
		provide: (f) => EditorView.decorations.from(f),
	});
}

type RecipientSource = Pick<EmailHeaderOptions, 'header' | 'answered' | 'rereads'>;

/** The server's reading of the header and the addresses on the email being answered, for the recipient marks. */
const recipientSource = Facet.define<RecipientSource, RecipientSource>({
	combine: (values) => values.at(-1) ?? { header: null, answered: null, rereads: false },
	compare: (a, b) => a.header === b.header && a.answered === b.answered && a.rereads === b.rereads,
});

/** The To and Cc marks from the server's reading: an entry the send would refuse, and an address added to the reply. Null while the header reads differently. */
function recipientMarks(state: EditorState): DecorationSet | null {
	const { header, answered } = state.facet(recipientSource);
	const recipients = currentRecipients(header, headerLayout(state.doc.iterLines()).text);
	if (!recipients) return null;
	const known = new Set(answered ?? []);
	const decos: Range<Decoration>[] = [];
	for (const entry of recipients) {
		// An entry the send would refuse is amber where it sits, with no "!".
		if (!entry.full) { decos.push(Decoration.mark({ class: 'cm-email-bad' }).range(entry.from, entry.to)); continue; }
		if (!answered || known.has(entry.text.toLowerCase())) continue;
		// The last characters of the address and the "!" are one no-wrap piece, so the "!" is never alone on a line (the rest of the address still breaks anywhere).
		const tailFrom = Math.max(entry.from, entry.to - ADDED_TAIL);
		if (tailFrom > entry.from) decos.push(Decoration.mark({ class: 'cm-email-added' }).range(entry.from, tailFrom));
		decos.push(Decoration.mark({ class: 'cm-email-added-tail', inclusiveEnd: true }).range(tailFrom, entry.to));
		decos.push(Decoration.widget({ widget: new AddedMarkWidget(), side: -1 }).range(entry.to));
	}
	return Decoration.set(decos, true);
}

/**
 * The marks follow the server's reading only while the header reads as the server read it. With live editing, between
 * an edit and the server's next reading (one round trip), the marks it last placed move with the text instead of
 * disappearing. Without it no next reading comes, so the old marks go.
 */
const recipientField = StateField.define<DecorationSet>({
	create: (state) => recipientMarks(state) ?? Decoration.none,
	update: (deco, tr) => {
		if (!tr.docChanged && tr.startState.facet(recipientSource) === tr.state.facet(recipientSource)) return deco;
		return recipientMarks(tr.state) ?? (tr.state.facet(recipientSource).rereads ? deco.map(tr.changes) : Decoration.none);
	},
	provide: (f) => EditorView.decorations.from(f),
});

/** The From line's range, or null. A person's edit is trimmed around it; an agent's or a remote write is not. */
function fromLine(state: EditorState): { from: number; to: number } | null {
	return headerLayout(state.doc.iterLines()).lines.find((line) => line.key === 'from') ?? null;
}

/**
 * A person's edit that reaches the From line applies everywhere but there: the line and the line breaks around it stay,
 * what it deleted on either side goes, and what it typed lands on the line after From. Select-all then type leaves
 * From and the typed text; select-all then delete leaves From.
 */
const keepFrom = EditorState.transactionFilter.of((tr: Transaction) => {
	if (!tr.docChanged || !(tr.isUserEvent('input') || tr.isUserEvent('delete') || tr.isUserEvent('undo') || tr.isUserEvent('redo') || tr.isUserEvent('move'))) return tr;
	const line = fromLine(tr.startState);
	if (!line) return tr;
	const length = tr.startState.doc.length;
	const after = line.to < length ? line.to + 1 : line.to;
	const specs: ChangeSpec[] = [];
	// The caret goes after what was typed, or where the deleted text was.
	let caret: number | null = null;
	tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
		if (fromA > line.to || toA < line.from) { specs.push({ from: fromA, to: toA, insert: inserted }); return; }
		if (fromA < line.from - 1) specs.push({ from: fromA, to: line.from - 1 });
		const text = inserted.toString();
		caret = Math.max(after, toA);
		if (after > line.to) specs.push({ from: after, to: caret, insert: text });
		else if (text) specs.push({ from: line.to, insert: `\n${text}` });
	});
	if (caret === null) return tr;
	const changes = tr.startState.changes(specs);
	return {
		changes, selection: { anchor: changes.mapPos(caret, 1) }, effects: tr.effects, scrollIntoView: tr.scrollIntoView,
		annotations: Transaction.userEvent.of(tr.annotation(Transaction.userEvent) ?? 'input'),
	};
});

/** The compact header for an email draft; reconfigure it when the notes, the server's reading or the attachments change. */
export function emailHeader(options: EmailHeaderOptions): Extension {
	return [field(options), recipientSource.of({ header: options.header, answered: options.answered, rereads: options.rereads }), recipientField, keepFrom, emailHeaderTheme, showAutolinkMarks.of(true)];
}
