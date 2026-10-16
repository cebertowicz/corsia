/** A box in the shared page, in its CSS pixels. */
export type PageRect = { x: number; y: number; width: number; height: number };
export type FieldMark = { field: PageRect; label: PageRect | null };
/** Where the field marker sits in the page area; `above` hangs it up and left of its point. */
export type MarkerPlace = { left: number; top: number; above: boolean };

// The agent counts as in the page this long after its last command, since it thinks between commands.
const PRESENCE_MS = 20_000;
// A field marker shows the field the agent just wrote, until its next command or this long after.
const FIELD_MARK_MS = 5_000;
// The marker's avatar and its ring, kept inside the page.
const MARKER_SIZE = 23;

export function pageRect(value: unknown): PageRect | null {
	if (!value || typeof value !== 'object') return null;
	const rect = value as Record<string, unknown>;
	return (['x', 'y', 'width', 'height'] as const).every((key) => typeof rect[key] === 'number')
		? rect as unknown as PageRect
		: null;
}

/**
 * Whether the agent is in a shared browser page, and the field it just wrote, from the server's
 * browser_agent messages. The server sends how long ago the agent acted, so no clock has to agree with it.
 */
export class BrowserAgentPresence {
	active = $state(false);
	mark = $state<FieldMark | null>(null);
	#presenceTimer: ReturnType<typeof setTimeout> | undefined;
	#markTimer: ReturnType<typeof setTimeout> | undefined;

	acted(message: Record<string, unknown>) {
		const age = typeof message.age_ms === 'number' ? Math.max(0, message.age_ms) : 0;
		clearTimeout(this.#presenceTimer);
		this.active = age < PRESENCE_MS;
		if (this.active) this.#presenceTimer = setTimeout(() => this.reset(), PRESENCE_MS - age);
		// Every agent command reports itself; only a fill or type also names its field, so any other command ends the mark.
		this.clearMark();
		const field = pageRect(message.field);
		if (!field || !this.active || age >= FIELD_MARK_MS) return;
		this.mark = { field, label: pageRect(message.label) };
		this.#markTimer = setTimeout(() => this.clearMark(), FIELD_MARK_MS - age);
	}

	clearMark() {
		clearTimeout(this.#markTimer);
		this.mark = null;
	}

	reset() {
		clearTimeout(this.#presenceTimer);
		this.active = false;
		this.clearMark();
	}
}

/**
 * The marker sits outside the field, at the end of its label's line, or else just above the field's
 * top-right corner: a field's own border is where sites put their buttons and icons. `page` is where
 * the drawn page starts in the page area, its scale, and the page area's right edge.
 */
export function markerPlace({ field, label }: FieldMark, page: { left: number; top: number; scale: number; right: number }): MarkerPlace {
	// A label beside its field shares the field's line, so the end of that label would be on the field.
	const ownLine = label && (label.y + label.height <= field.y || label.y >= field.y + field.height) ? label : null;
	return ownLine
		? {
			left: Math.min(page.left + (ownLine.x + ownLine.width) * page.scale + 6, page.right - MARKER_SIZE),
			top: page.top + (ownLine.y + ownLine.height / 2) * page.scale,
			above: false,
		}
		: { left: page.left + (field.x + field.width) * page.scale, top: Math.max(page.top + field.y * page.scale - 4, MARKER_SIZE), above: true };
}
