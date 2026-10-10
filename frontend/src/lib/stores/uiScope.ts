import { writable } from 'svelte/store';
import { openThread } from './openThread';

export interface UiScope {
	id: string;
	name: string;
	color: string;
}

export const uiScope = writable<UiScope | null>(null);

export function enterScope(s: UiScope) { openThread.set(null); uiScope.set(s); }
export function exitScope() { openThread.set(null); uiScope.set(null); }
