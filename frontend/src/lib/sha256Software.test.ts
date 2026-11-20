import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sha256HexWithoutSubtle } from './sha256Software';
import { textSha256 } from '$lib/stores/documentWire';

const encode = (text: string) => new TextEncoder().encode(text);
const node = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');

afterEach(() => vi.unstubAllGlobals());

describe('sha256HexWithoutSubtle', () => {
	it('matches the published FIPS 180-4 vectors', () => {
		expect(sha256HexWithoutSubtle(encode(''))).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
		expect(sha256HexWithoutSubtle(encode('abc'))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
		expect(sha256HexWithoutSubtle(encode('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq')))
			.toBe('248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1');
	});

	it.each([
		['accented Latin', 'Über café — naïve résumé'],
		['CJK', '漢字かな한국어'],
		['astral plane (4-byte UTF-8)', '𝄞 🙂 👨‍👩‍👧‍👦'],
		['a BOM and mixed line endings', '﻿line one\r\nline two\nline three\r'],
		['an em dash and curly quotes', '“quoted” — and ‘single’'],
	])('hashes %s exactly as WebCrypto does over its UTF-8 bytes', async (_label, text) => {
		const fallback = sha256HexWithoutSubtle(encode(text));
		expect(fallback).toBe(node(encode(text)));
		expect(fallback).toBe(Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encode(text))), (b) => b.toString(16).padStart(2, '0')).join(''));
	});

	it.each([0, 1, 54, 55, 56, 57, 63, 64, 65, 111, 112, 119, 120, 127, 128, 129, 1000])(
		'pads a %i-byte message across the one- and two-block boundary correctly',
		(length) => {
			const bytes = new Uint8Array(length).map((_, i) => (i * 31 + 7) & 0xff);
			expect(sha256HexWithoutSubtle(bytes)).toBe(node(bytes));
		},
	);

	it('hashes a document at the editor size limit', () => {
		const bytes = encode('lorem ipsum 漢字 \n'.repeat(55_000));
		expect(bytes.length).toBeGreaterThan(900_000);
		expect(sha256HexWithoutSubtle(bytes)).toBe(node(bytes));
	});
});

describe('textSha256 without WebCrypto', () => {
	it('answers with the same digest when crypto.subtle is absent (plain HTTP over a LAN address)', async () => {
		const text = 'Über 漢字 🙂\r\n';
		const withSubtle = await textSha256(text);
		vi.stubGlobal('crypto', {});
		expect(await textSha256(text)).toBe(withSubtle);
		expect(withSubtle).toBe(node(encode(text)));
	});

	it('answers when crypto itself is missing', async () => {
		const text = 'plain';
		const expected = node(encode(text));
		vi.stubGlobal('crypto', undefined);
		expect(await textSha256(text)).toBe(expected);
	});

	it('uses WebCrypto when it is there', async () => {
		const digest = vi.spyOn(crypto.subtle, 'digest');
		await textSha256('abc');
		expect(digest).toHaveBeenCalledOnce();
		digest.mockRestore();
	});
});
