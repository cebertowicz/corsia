
import type { APIRequestContext } from '@playwright/test';

const BACKEND_PORT = Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110);
const BASE_URL = `http://127.0.0.1:${BACKEND_PORT}`;

export interface MockReply {
    content?: string;
    reasoning_content?: string;
    completion_gate?: string;
    delay_ms?: number;
    tool_calls?: Array<{
        id: string;
        name: string;
        // Streamed as function.arguments, which the native loop concatenates:
        // pass a JSON string for a call that must execute.
        arguments: Record<string, unknown> | string;
    }>;
    finish_reason?: string;
    // Serves this reply only to the named native thread's turns; other threads get the default.
    match_thread?: string;
}

export async function seedMockResponses(
    request: APIRequestContext,
    replies: MockReply[],
): Promise<void> {
    const resp = await request.post(`${BASE_URL}/api/test/mock-responses`, {
        data: {
            replies: replies.map((r) => ({ role: 'assistant', ...r })),
        },
    });
    if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(
            `Failed to seed mock responses: ${resp.status()} ${resp.statusText()} — ${body}`,
        );
    }
}

async function readMockState(
    request: APIRequestContext,
): Promise<{ call_count: number; unmatched_calls: Array<{ thread_id: string | null }> }> {
    const resp = await request.get(`${BASE_URL}/api/test/mock-responses`);
    if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(
            `Failed to read mock state: ${resp.status()} ${resp.statusText()} — ${body}`,
        );
    }
    const body = await resp.json();
    if (!Number.isInteger(body.call_count) || body.call_count < 0) {
        throw new Error(`Invalid mock call count: ${JSON.stringify(body.call_count)}`);
    }
    return body;
}

export async function readMockCallCount(
    request: APIRequestContext,
): Promise<number> {
    return (await readMockState(request)).call_count;
}

/** Threads whose model calls no thread-bound seeded reply served since the last seed, one entry per call. */
export async function readUnmatchedThreads(
    request: APIRequestContext,
): Promise<Array<string | null>> {
    return (await readMockState(request)).unmatched_calls.map(call => call.thread_id);
}

export async function releaseMockGate(
    request: APIRequestContext,
    gateName: string,
): Promise<void> {
    const resp = await request.post(
        `${BASE_URL}/api/test/mock-gates/${encodeURIComponent(gateName)}/release`,
    );
    if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(
            `Failed to release mock gate ${gateName}: ${resp.status()} ${resp.statusText()} — ${body}`,
        );
    }
}

export async function injectMessage(
    request: APIRequestContext,
    sessionId: string,
    message: {
        role?: string;
        content: string;
        metadata?: Record<string, unknown>;
    },
    token: string,
): Promise<string> {
    const resp = await request.post(
        `${BASE_URL}/api/test/inject-message?token=${encodeURIComponent(token)}`,
        { data: { thread_id: sessionId, message } },
    );
    if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(
            `Failed to inject message: ${resp.status()} ${resp.statusText()} — ${body}`,
        );
    }
    const body = await resp.json();
    return body.message_id as string;
}

export async function seedPressuredHistory(
    request: APIRequestContext,
    sessionId: string,
    token: string,
): Promise<{ contextWindow: number; userMessageCount: number }> {
    const resp = await request.post(
        BASE_URL + '/api/test/seed-pressured-history?token=' + encodeURIComponent(token),
        { data: { thread_id: sessionId } },
    );
    if (!resp.ok()) {
        const body = await resp.text();
        throw new Error(
            `Failed to seed pressured history: ${resp.status()} ${resp.statusText()} — ${body}`,
        );
    }
    const body = await resp.json();
    if (!Number.isInteger(body.context_window) || body.context_window <= 0) {
        throw new Error(`Invalid pressure context window: ${JSON.stringify(body.context_window)}`);
    }
    if (!Number.isInteger(body.turns) || body.turns <= 0) {
        throw new Error(`Invalid pressured-history turn count: ${JSON.stringify(body.turns)}`);
    }
    return {
        contextWindow: body.context_window as number,
        userMessageCount: body.turns as number,
    };
}
