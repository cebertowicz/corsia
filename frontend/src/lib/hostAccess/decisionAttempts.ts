import { get, writable } from 'svelte/store';

export const inFlightDecisionAttempts = writable(new Map<string, symbol>());

export function claimDecision(leaseId: string): symbol | null {
	const attempt = Symbol(leaseId);
	let claimed = false;
	inFlightDecisionAttempts.update((current) => {
		if (current.has(leaseId)) return current;
		const next = new Map(current);
		next.set(leaseId, attempt);
		claimed = true;
		return next;
	});
	return claimed ? attempt : null;
}

export function isCurrentDecision(leaseId: string, attempt: symbol): boolean {
	return get(inFlightDecisionAttempts).get(leaseId) === attempt;
}

export function releaseDecision(leaseId: string, attempt: symbol): void {
	inFlightDecisionAttempts.update((current) => {
		if (current.get(leaseId) !== attempt) return current;
		const next = new Map(current);
		next.delete(leaseId);
		return next;
	});
}
