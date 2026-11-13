/** Why a person's flag change (mute, resolve) was not taken, in the terms the card can say: the connection dropped, CORSIA failed (with its reference), or the server refused. */
export type PatchFailure = { cause: 'connection' | 'server' | 'other'; reference: string | null };

/** Reads a failed request's cause; a thrown fetch (no answer at all) is the connection. A refused request (`RequestRefused`) carries the answer's `status` and `reference`. */
export function patchFailureOf(error: unknown): PatchFailure {
	if (error instanceof Error && 'status' in error && 'reference' in error) {
		const { status, reference } = error as Error & { status: number | null; reference: string | null };
		return { cause: status !== null && status >= 500 ? 'server' : 'other', reference };
	}
	return { cause: error instanceof TypeError ? 'connection' : 'other', reference: null };
}
