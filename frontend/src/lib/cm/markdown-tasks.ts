import { EditorState, Prec, Transaction, type Extension } from '@codemirror/state';
import { syntaxTree } from '@codemirror/language';
import { Decoration, type DecorationSet, EditorView, keymap, ViewPlugin, type ViewUpdate, WidgetType } from '@codemirror/view';
import { emitProductEvent } from '$lib/product_events';

interface TaskMarker {
	from: number;
	to: number;
	checked: boolean;
}

function isReadOnly(view: EditorView) {
	return view.state.readOnly;
}

function markersOnSelection(view: EditorView): TaskMarker[] {
	const { doc, selection } = view.state;
	const from = doc.lineAt(selection.main.from).from;
	const to = doc.lineAt(selection.main.to).to;
	const markers: TaskMarker[] = [];

	syntaxTree(view.state).iterate({
		from,
		to,
		enter: (node) => {
			if (node.name !== 'TaskMarker') return;
			const marker = doc.sliceString(node.from, node.to);
			const checked = /^\[[xX]\]$/.test(marker);
			if (/^\[[ xX]\]$/.test(marker)) markers.push({ from: node.from, to: node.to, checked });
		}
	});
	return markers;
}

function toggleMarkers(view: EditorView, markers: readonly TaskMarker[], input: 'click' | 'keyboard') {
	view.dispatch({
		changes: markers.map((marker) => ({
			from: marker.from + 1,
			to: marker.from + 2,
			insert: marker.checked ? ' ' : 'x'
		})),
		annotations: Transaction.userEvent.of('input.toggleTask')
	});
	emitProductEvent('document', 'task_toggled', {
		input,
		task_count: markers.length,
		completed_count: markers.filter((marker) => !marker.checked).length,
		reopened_count: markers.filter((marker) => marker.checked).length
	});
}

/**
 * Toggles every GFM task marker on a line touched by the primary selection.
 * Returns false when there is no editable task so CodeMirror can use its
 * existing Mod-Enter binding.
 */
export function toggleTasksOnSelection(view: EditorView) {
	if (isReadOnly(view)) return false;
	const markers = markersOnSelection(view);
	if (!markers.length) return false;
	toggleMarkers(view, markers, 'keyboard');
	return true;
}

class TaskCheckboxWidget extends WidgetType {
	constructor(
		readonly from: number,
		readonly checked: boolean,
		readonly label: string
	) {
		super();
	}

	eq(other: TaskCheckboxWidget) {
		return other.from === this.from && other.checked === this.checked && other.label === this.label;
	}

	toDOM(view: EditorView) {
		const control = document.createElement('span');
		control.className = 'cm-md-task-control';
		const checkbox = document.createElement('input');
		checkbox.className = 'cm-md-task-checkbox';
		checkbox.type = 'checkbox';
		checkbox.checked = this.checked;
		checkbox.disabled = isReadOnly(view);
		checkbox.setAttribute('aria-label', `${this.checked ? 'Mark task incomplete' : 'Mark task complete'}: ${this.label}`);
		control.append(checkbox);

		control.addEventListener('mousedown', (event) => {
			event.preventDefault();
			event.stopPropagation();
		});
		control.addEventListener('click', (event) => {
			event.preventDefault();
			event.stopPropagation();
			if (isReadOnly(view)) return;
			toggleMarkers(view, [{ from: this.from, to: this.from + 3, checked: this.checked }], 'click');
		});
		return control;
	}

	ignoreEvent() {
		return true;
	}
}

function taskDecorations(view: EditorView): DecorationSet {
	const decorations = [];
	const { doc } = view.state;
	const hide = (from: number, to: number, widget?: WidgetType) => {
		if (from >= to) return;
		decorations.push((widget ? Decoration.replace({ widget }) : Decoration.replace({})).range(from, to));
	};

	for (const { from, to } of view.visibleRanges) {
		syntaxTree(view.state).iterate({
			from,
			to,
			enter: (node) => {
				if (node.name !== 'TaskMarker') return;
				const marker = doc.sliceString(node.from, node.to);
				if (!/^\[[ xX]\]$/.test(marker)) return;
				const task = node.node.parent;
				const item = task?.parent;
				const listMark = item?.getChild('ListMark');
				const checked = /^\[[xX]\]$/.test(marker);
				const label = doc.sliceString(node.to, task?.to ?? node.to).trim().replace(/\s+/g, ' ').slice(0, 80) || 'task';

				if (listMark && /^[-*+]$/.test(doc.sliceString(listMark.from, listMark.to))) {
					hide(listMark.from, node.from);
				}
				hide(node.from, node.to, new TaskCheckboxWidget(node.from, checked, label));
				if (checked && task && node.to < task.to) {
					decorations.push(Decoration.mark({ class: 'cm-md-task-done' }).range(node.to, task.to));
				}
			}
		});
	}
	return Decoration.set(decorations, true);
}

const taskPreview = ViewPlugin.fromClass(
	class {
		decorations: DecorationSet;

		constructor(view: EditorView) {
			this.decorations = taskDecorations(view);
		}

		update(update: ViewUpdate) {
			if (
				update.docChanged ||
				update.viewportChanged ||
				syntaxTree(update.startState) !== syntaxTree(update.state) ||
				update.transactions.some((tr) => tr.startState.readOnly !== tr.state.readOnly)
			) {
				this.decorations = taskDecorations(update.view);
			}
		}
	},
	{ decorations: (plugin) => plugin.decorations }
);

/** Interactive, source-preserving GFM checklist rendering for the document editor. */
export const markdownTasks: Extension = [
	taskPreview,
	Prec.high(keymap.of([{ key: 'Mod-Enter', run: toggleTasksOnSelection }]))
];
