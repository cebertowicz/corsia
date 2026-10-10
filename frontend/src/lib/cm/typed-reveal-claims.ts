import { StateEffect } from '@codemirror/state';
import type { CommentRange } from './comment-marks';
import type { Preview } from './typed-reveal-preview';
import type { Lane } from './typed-reveal-state';

/**
 * How a committed comment or suggestion takes over the preview its call streamed. A comment's arrival
 * adopts the highlight the preview began, keeping when it started; a suggestion is claimed by the
 * committed suggestion at its place, which then draws the insert while the lane types the rest.
 */
export interface AdoptSelect { threadId: string; key: string; from: number; to: number; holdMs: number }
export const adoptPreviewSelect = StateEffect.define<AdoptSelect>();

/** The select a comment preview began becomes its arrival's: the anchor it resolved to, and the body's hold. */
export function withAdopted(lanes: readonly Lane[], value: AdoptSelect): Lane[] {
	return lanes.map((lane) => {
		const select = lane.select;
		if (lane.threadId !== value.threadId || select?.key !== value.key) return lane;
		const length = value.to - value.from;
		return {
			...lane,
			select: { ...select, from: value.from, to: value.to, length, shown: Math.min(select.shown, length), holdMs: value.holdMs },
			previews: lane.previews.filter((preview) => preview.selectKey !== value.key),
		};
	});
}

const committedOf = (preview: Preview, ranges: readonly CommentRange[]) => ranges.find((range) => range.suggestion
	&& range.from === preview.from && range.to === preview.to && range.suggestion.insert.startsWith(preview.text));

/**
 * Claim each streamed suggestion once its committed suggestion is in the editor at the same place with an
 * insert that continues what streamed, whichever of the activity and comments frames came first. A claimed
 * preview types the rest of the committed insert and goes once all of it shows, or once the suggestion does.
 */
export function withClaimedSuggestions(lanes: readonly Lane[], ranges: readonly CommentRange[]): Lane[] {
	if (!lanes.some((lane) => lane.previews.some((preview) => preview.mode === 'suggest'))) return [...lanes];
	return lanes.map((lane) => {
		let grew = false, changed = false;
		const previews = lane.previews.flatMap((preview): Preview[] => {
			if (preview.mode !== 'suggest') return [preview];
			if (preview.claimId) {
				if (preview.typed < preview.text.length && ranges.some((range) => range.id === preview.claimId && range.suggestion)) return [preview];
				changed = true;
				return [];
			}
			const committed = committedOf(preview, ranges);
			if (!committed) return [preview];
			changed = true;
			const insert = committed.suggestion!.insert;
			if (preview.typed >= insert.length) return [];
			grew ||= insert.length > preview.text.length;
			return [{ ...preview, text: insert, claimId: committed.id }];
		});
		return changed ? { ...lane, previews, generation: lane.generation + (grew ? 1 : 0) } : lane;
	});
}

/** What each lane has typed of the suggestions it claimed, for the suggestion marks to draw. */
export function typedSuggestions(lanes: readonly Lane[]): ReadonlyMap<string, string> {
	const typed = new Map<string, string>();
	for (const lane of lanes) {
		for (const preview of lane.previews) if (preview.claimId) typed.set(preview.claimId, preview.text.slice(0, preview.typed));
	}
	return typed;
}
