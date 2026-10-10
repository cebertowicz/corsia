const listeners = new Map<string, Set<(data: any) => void>>();

export function on(event: string, handler: (data: any) => void) {
	if (!listeners.has(event)) listeners.set(event, new Set());
	listeners.get(event)!.add(handler);
	return () => listeners.get(event)?.delete(handler);
}

export function emit(event: string, data: any) {
	listeners.get(event)?.forEach(fn => fn(data));
}
