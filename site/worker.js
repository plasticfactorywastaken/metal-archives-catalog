import {matches, csvRow, CSV_FIELDS, csvCell, safeDataURL} from './query.mjs';
import {readPartition} from './partition.mjs';

let generation = 0;
let activeFetch;
const EXPORT_LIMIT = 10000;

self.addEventListener('message', async ({data}) => {
  activeFetch?.abort();
  const controller = new AbortController();
  activeFetch = controller;
  const current = ++generation;
  const {requestId, action, manifest, filters, baseURL, page = 1, pageSize = 50, excludedEntityTypes = []} = data;
  const partitions = manifest.partitions.filter(part => !excludedEntityTypes.includes(part.entity_type) && (!filters.entity_type || part.entity_type === filters.entity_type));
  const offset = (page - 1) * pageSize;
  let total = 0, scanned = 0, failures = 0, exported = 0, previewCount = 0;
  let rows = [];
  const chunks = action === 'export' ? [CSV_FIELDS.map(csvCell).join(',') + '\r\n'] : null;
  try {
    for (let i = 0; i < partitions.length; i++) {
      if (current !== generation) return;
      const part = partitions[i];
      try {
        const response = await fetch(safeDataURL(part.path, baseURL), {signal:controller.signal});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await readPartition(response, part, {signal:controller.signal});
        if (current !== generation) return;
        const records = Array.isArray(payload) ? payload : payload.records;
        if (!Array.isArray(records) || records.length > 2000) throw new Error('Invalid or oversized data partition.');
        if (Number.isInteger(part.count) && records.length !== part.count) throw new Error('Data partition record count mismatch.');
        for (const record of records) {
          scanned++;
          if (!matches(record, filters)) continue;
          if (action === 'export') {
            if (exported < EXPORT_LIMIT) { chunks.push(csvRow(record)); exported++; }
          } else if (total >= offset && rows.length < pageSize) rows.push(record);
          total++;
        }
      } catch (error) {
        if (current !== generation || controller.signal.aborted) return;
        failures++;
        self.postMessage({requestId, kind:'partition-error', path:part.path, error:error.message});
      }
      if (current !== generation) return;
      if (action !== 'export' && rows.length > previewCount) {
        previewCount = rows.length;
        self.postMessage({requestId, kind:'preview', rows:rows.slice(), total, scanned, failures, page});
      }
      self.postMessage({requestId, kind:'progress', action, scanned, total, completed:i + 1, partitions:partitions.length, failures});
    }
    if (current !== generation) return;
    if (action === 'export') self.postMessage({requestId, kind:'export', csv:chunks.join(''), total, exported, failures, capped:total > EXPORT_LIMIT});
    else self.postMessage({requestId, kind:'result', rows, total, scanned, failures, page});
  } catch (error) {
    if (current === generation) self.postMessage({requestId, kind:'error', error:error.message});
  }
});
