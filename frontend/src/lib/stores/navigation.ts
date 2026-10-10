import { writable } from 'svelte/store';

export const features = writable({
	homeView: true,
	activeWork: true,
	projectProgress: true,
	conversationPinning: true,
});
