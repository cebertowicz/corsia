import { get } from 'svelte/store';
import { token, user } from '../stores/auth';
import { connectionState, connectionError } from '../stores/connection';
import { ws, setWs } from './state';
import { handleMessage } from './handlers';
import './documentOpen';

let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectAttempts = 0;
const MAX_RECONNECT_DELAY = 30000;

let heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
const HEARTBEAT_TIMEOUT_MS = 45000;

// This module is the only owner of the live socket. `ownerSession` is the
// session the current socket (or its pending retry) serves; `ownerEpoch` moves
// whenever that owner is retired. Every callback a socket installs captures
// its own epoch, so a retired socket can never authenticate, deliver frames,
// arm timers or schedule a retry. The session never leaves this module.
let ownerSession: string | null = null;
let ownerEpoch = 0;

function clearReconnectTimer() {
	if (reconnectTimer) clearTimeout(reconnectTimer);
	reconnectTimer = null;
}

function clearHeartbeat() {
	if (heartbeatTimer) clearTimeout(heartbeatTimer);
	heartbeatTimer = null;
}

/** Retire the current owner before anything closes it. */
function retireOwner(): WebSocket | null {
	ownerEpoch += 1;
	ownerSession = null;
	clearReconnectTimer();
	clearHeartbeat();
	const retired = ws;
	setWs(null);
	return retired;
}

// A changed or cleared session invalidates the owner; it never starts a new
// one. Callers that sign in decide when to connect.
token.subscribe((session) => {
	if (ownerSession !== null && session !== ownerSession) retireOwner()?.close();
});

export function connect() {
	const session = get(token);
	if (!session) return;
	const current = ws;
	if (
		current
		&& ownerSession === session
		&& (current.readyState === WebSocket.CONNECTING || current.readyState === WebSocket.OPEN)
	) return;
	openOwner(session);
}

function openOwner(session: string) {
	retireOwner()?.close();
	const epoch = ownerEpoch;
	const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
	const socket = new WebSocket(`${protocol}//${location.host}/ws`);
	ownerSession = session;
	setWs(socket);

	const owns = () => (
		ownerEpoch === epoch && ws === socket && ownerSession === session && get(token) === session
	);
	const resetHeartbeat = () => {
		if (!owns()) return;
		clearHeartbeat();
		heartbeatTimer = setTimeout(() => {
			if (!owns()) return;
			console.warn('[ws] heartbeat timeout — reconnecting');
			socket.close();
		}, HEARTBEAT_TIMEOUT_MS);
	};

	connectionState.set('connecting');

	socket.onopen = () => {
		if (!owns()) return;
		socket.send(JSON.stringify({ type: 'auth', token: session }));
	};

	socket.onmessage = (event) => {
		if (!owns()) return;
		const data = JSON.parse(event.data);
		handleMessage(data, resetHeartbeat, owns);
	};

	socket.onclose = () => {
		if (!owns()) return;
		ownerEpoch += 1;
		setWs(null);
		clearHeartbeat();
		const wasAuthenticated = get(user) !== null;
		if (!wasAuthenticated) {
			ownerSession = null;
			return;
		}
		connectionState.set('disconnected');
		reconnectAttempts++;
		const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), MAX_RECONNECT_DELAY);
		connectionError.set(`Reconnecting in ${Math.round(delay / 1000)}s...`);
		const retryEpoch = ownerEpoch;
		reconnectTimer = setTimeout(() => {
			reconnectTimer = null;
			if (ownerEpoch !== retryEpoch || ws || get(token) !== session) return;
			connect();
		}, delay);
	};

	socket.onerror = () => {
		if (!owns()) return;
		connectionState.set('error');
		connectionError.set('Connection lost');
		socket.close();
	};
}

export function disconnect() {
	retireOwner()?.close();
}

export function isConnected(): boolean {
	return ws?.readyState === WebSocket.OPEN;
}

export function reconnectNow() {
	clearReconnectTimer();
	reconnectAttempts = 0;
	connectionError.set('Reconnecting...');
	const session = get(token);
	if (session) openOwner(session);
}
