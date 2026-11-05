import { randomUUID } from 'node:crypto';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const rank = { uncertain: 0, failed: 1, sent: 2, delivered: 3, read: 4 };

export function localApiUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) ||
      !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) ||
      url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('The WhatsApp bridge API must be a loopback origin');
  }
  return url.origin;
}

export function receiptStatus(status) {
  if (status === 0) return 'failed';
  if (status === 2) return 'sent';
  if (status === 3) return 'delivered';
  if (status === 4 || status === 5) return 'read';
  return undefined;
}

export class ReceiptJournal {
  constructor(path) { this.path = path; this.entries = {}; this.writes = Promise.resolve(); }
  async load() {
    try {
      const value = JSON.parse(await readFile(this.path, 'utf8'));
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid journal');
      this.entries = value;
      for (const entry of Object.values(this.entries)) {
        if (entry.awaiting_ack) {
          entry.awaiting_ack = false; entry.acknowledged = false;
          entry.detail = 'provider_ack_interrupted';
        }
      }
    } catch (error) { if (error.code !== 'ENOENT') throw new Error('Receipt journal needs local reconciliation'); }
  }
  async save() {
    const data = JSON.stringify(this.entries);
    this.writes = this.writes.then(async () => {
      await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
      const temporary = `${this.path}.${randomUUID()}.tmp`;
      await writeFile(temporary, data, { mode: 0o600, flag: 'wx' });
      await rename(temporary, this.path);
    });
    await this.writes;
  }
}

/** Retries acknowledge a receipt, never the WhatsApp send itself. */
export class BridgeWorker {
  constructor({ api, journal, sendMessage, generateId, notify = () => {} }) {
    Object.assign(this, { api, journal, sendMessage, generateId, notify });
  }
  async consume(job) {
    if (this.journal.entries[job.execution_id]) return;
    const valid = typeof job.execution_id === 'string' && job.execution_id.length > 0 && job.execution_id.length <= 128 &&
      typeof job.message_id === 'string' && job.message_id.length > 0 &&
      typeof job.phone === 'string' && /^\+[1-9][0-9]{6,14}$/.test(job.phone) &&
      typeof job.body === 'string' && job.body.trim() && job.body.length <= 4000;
    if (!valid) throw new Error('Invalid claimed job; no message was sent');
    const outgoingId = this.generateId();
    const entry = { execution_id: job.execution_id, message_id: job.message_id, outgoing_id: outgoingId,
      status: 'uncertain', detail: 'awaiting_provider_ack', acknowledged: false, awaiting_ack: true, created_at: Date.now() };
    this.journal.entries[job.execution_id] = entry;
    // A process crash after this point cannot cause a second send for this claim.
    await this.journal.save();
    try {
      const message = await this.sendMessage(`${job.phone.slice(1)}@s.whatsapp.net`, { text: job.body }, { messageId: outgoingId });
      if (typeof message?.key?.id === 'string' && message.key.id) {
        entry.provider_reference ??= message.key.id;
        await this.journal.save();
        // Completion alone is not a delivery receipt. Wait for SERVER_ACK or stronger.
        if (receiptStatus(message.status)) await this.record(message.key.id, message.status);
      } else {
        entry.awaiting_ack = false; entry.detail = 'unverified_provider_response';
        await this.journal.save();
      }
    } catch {
      if (entry.status === 'uncertain') {
        entry.detail = 'send_result_uncertain'; entry.acknowledged = false; entry.awaiting_ack = false;
        await this.journal.save();
      }
    }
  }
  async record(providerId, status) {
    const state = receiptStatus(status);
    if (!state) return;
    const entry = Object.values(this.journal.entries).find(item =>
      item.outgoing_id === providerId || item.provider_reference === providerId);
    if (!entry || entry.status === 'failed' || rank[state] <= rank[entry.status]) return;
    // A provider-issued acknowledgement is stronger evidence than an earlier timeout.
    entry.provider_reference ??= providerId;
    entry.status = state;
    entry.awaiting_ack = false;
    entry.detail = state === 'failed' ? 'provider_rejected_message' : 'provider_receipt';
    entry.acknowledged = false;
    await this.journal.save();
  }
  async flushReceipts() {
    for (const entry of Object.values(this.journal.entries)) {
      if (entry.awaiting_ack && Date.now() - entry.created_at > 30_000) {
        entry.awaiting_ack = false; entry.detail = 'provider_ack_timeout';
        await this.journal.save();
      }
    }
    for (const entry of Object.values(this.journal.entries).filter(item => !item.acknowledged && !item.blocked && !item.awaiting_ack).slice(0, 10)) {
      const receipt = { status: entry.status, detail: entry.detail };
      if (entry.provider_reference) receipt.provider_reference = entry.provider_reference;
      // Preserve this receipt generation if a later provider event arrives during HTTP.
      const fingerprint = JSON.stringify(receipt);
      try {
        await this.api.receipt(entry.execution_id, receipt);
        const current = { status: entry.status, detail: entry.detail };
        if (entry.provider_reference) current.provider_reference = entry.provider_reference;
        if (JSON.stringify(current) === fingerprint) entry.acknowledged = true;
        await this.journal.save();
      } catch (error) {
        if ([400, 401, 403, 404, 409, 422].includes(error.status)) {
          entry.blocked = true;
          await this.journal.save();
          this.notify('Receipt reconciliation required. No message will be resent.');
        }
        return;
      }
    }
  }
}
