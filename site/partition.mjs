// Read one bounded partition. Verify stored gzip bytes before decompression.
const MAX_COMPRESSED_BYTES = 32 * 1024 * 1024;
const MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024;

function checkAbort(signal) {
  if (signal?.aborted) throw new DOMException('Search cancelled', 'AbortError');
}

async function readBounded(stream, maximum, signal) {
  const reader = stream.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      checkAbort(signal);
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) throw new Error('Data partition exceeds its byte limit.');
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  checkAbort(signal);
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

async function verify(bytes, expected, label) {
  if (!/^[a-f0-9]{64}$/i.test(expected || '')) throw new Error(`${label} checksum is missing or invalid.`);
  if (!globalThis.crypto?.subtle) throw new Error('Secure-context SHA-256 support is required to verify catalog data.');
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const actual = Array.from(digest, value => value.toString(16).padStart(2, '0')).join('');
  if (actual !== expected.toLowerCase()) throw new Error(`${label} checksum mismatch.`);
}

function sizeLimit(declared, maximum) {
  if (declared === undefined) return maximum;
  if (!Number.isSafeInteger(declared) || declared < 1 || declared > maximum) throw new Error('Data partition declares an invalid or oversized byte count.');
  return declared;
}

export async function readPartition(response, part, {signal, maxCompressedBytes = MAX_COMPRESSED_BYTES, maxUncompressedBytes = MAX_UNCOMPRESSED_BYTES} = {}) {
  const gzip = part.encoding === 'gzip' || part.compression === 'gzip' || /\.gz$/i.test(part.path || '');
  // Existing published manifests contain plain JSON and have no encoding key.
  if (!gzip) return response.json();
  const compressedLimit = sizeLimit(part.bytes, maxCompressedBytes);
  const uncompressedLimit = sizeLimit(part.uncompressed_bytes, maxUncompressedBytes);
  if (!response.body?.getReader) throw new Error('Streaming responses are required for compressed catalog data.');
  const compressed = await readBounded(response.body, compressedLimit, signal);
  if (part.bytes !== undefined && compressed.byteLength !== part.bytes) throw new Error('Compressed partition byte count mismatch.');
  await verify(compressed, part.sha256, 'Compressed partition');
  checkAbort(signal);
  // Instantiate the decompressor only after the stored file checksum passes.
  if (typeof DecompressionStream !== 'function') throw new Error('This browser does not support gzip catalog partitions.');
  const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
  const uncompressed = await readBounded(stream, uncompressedLimit, signal);
  if (part.uncompressed_bytes !== undefined && uncompressed.byteLength !== part.uncompressed_bytes) throw new Error('Uncompressed partition byte count mismatch.');
  if (part.uncompressed_sha256 !== undefined) await verify(uncompressed, part.uncompressed_sha256, 'Uncompressed partition');
  checkAbort(signal);
  return JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(uncompressed));
}
