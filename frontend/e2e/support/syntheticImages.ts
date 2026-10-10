import { deflateSync } from 'node:zlib';

/** Small, deterministic image files for specs; no binary fixtures to keep in the repo. */

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
	let c = n;
	for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
	return c >>> 0;
});

function crc32(bytes: Buffer): number {
	let c = 0xffffffff;
	for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
	return (c ^ 0xffffffff) >>> 0;
}

function pngChunk(type: string, data: Buffer): Buffer {
	const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
	const out = Buffer.alloc(body.length + 8);
	out.writeUInt32BE(data.length, 0);
	body.copy(out, 4);
	out.writeUInt32BE(crc32(body), body.length + 4);
	return out;
}

/** An opaque RGB gradient PNG with a 4px frame. */
export function syntheticPng(width: number, height: number): Buffer {
	const header = Buffer.alloc(13);
	header.writeUInt32BE(width, 0);
	header.writeUInt32BE(height, 4);
	header.set([8, 2, 0, 0, 0], 8);
	const rows = Buffer.alloc((width * 3 + 1) * height);
	for (let y = 0; y < height; y++) {
		const base = y * (width * 3 + 1);
		for (let x = 0; x < width; x++) {
			const frame = x < 4 || y < 4 || x >= width - 4 || y >= height - 4;
			const at = base + 1 + x * 3;
			rows[at] = frame ? 255 : Math.round((x / width) * 255);
			rows[at + 1] = frame ? 255 : Math.round((y / height) * 200);
			rows[at + 2] = frame ? 255 : 160;
		}
	}
	return Buffer.concat([
		Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
		pngChunk('IHDR', header),
		pngChunk('IDAT', deflateSync(rows)),
		pngChunk('IEND', Buffer.alloc(0)),
	]);
}

function lzwLiteral(indices: number[]): Buffer {
	// Min code size 2: clear=4, end=5, 3-bit codes. A clear every two pixels keeps
	// the decoder's table small enough that the code width never grows.
	const bits: number[] = [];
	const put = (code: number) => { for (let i = 0; i < 3; i++) bits.push((code >> i) & 1); };
	put(4);
	indices.forEach((px, i) => { put(px); if (i % 2 === 1) put(4); });
	put(5);
	const bytes: number[] = [];
	for (let i = 0; i < bits.length; i += 8) {
		let b = 0;
		for (let j = 0; j < 8 && i + j < bits.length; j++) b |= bits[i + j] << j;
		bytes.push(b);
	}
	const out: number[] = [2];
	for (let i = 0; i < bytes.length; i += 255) {
		const block = bytes.slice(i, i + 255);
		out.push(block.length, ...block);
	}
	out.push(0);
	return Buffer.from(out);
}

/** An animated GIF that cycles through four solid-colour frames forever. */
export function syntheticGif(width: number, height: number): Buffer {
	const palette = Buffer.from([230, 60, 60, 60, 200, 90, 70, 110, 235, 245, 200, 50]);
	const parts: Buffer[] = [
		Buffer.from('GIF89a', 'ascii'),
		Buffer.from([width & 255, width >> 8, height & 255, height >> 8, 0xf1, 0, 0]),
		palette,
		Buffer.from([0x21, 0xff, 0x0b, ...Buffer.from('NETSCAPE2.0', 'ascii'), 3, 1, 0, 0, 0]),
	];
	for (let frame = 0; frame < 4; frame++) {
		parts.push(Buffer.from([0x21, 0xf9, 4, 0, 20, 0, 0, 0]));
		parts.push(Buffer.from([0x2c, 0, 0, 0, 0, width & 255, width >> 8, height & 255, height >> 8, 0]));
		parts.push(lzwLiteral(Array.from({ length: width * height }, () => frame)));
	}
	parts.push(Buffer.from([0x3b]));
	return Buffer.concat(parts);
}
