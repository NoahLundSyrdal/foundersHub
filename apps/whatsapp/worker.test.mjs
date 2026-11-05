import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { BridgeWorker, localApiUrl, ReceiptJournal, receiptStatus } from './worker.mjs';

async function setup(t, overrides = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'office-whatsapp-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const journal = new ReceiptJournal(join(directory, 'receipts.json'));
  await journal.load();
  const sends = [], receipts = [];
  const worker = new BridgeWorker({ journal, generateId: () => '3EB0SYNTHETIC',
    sendMessage: async (...args) => { sends.push(args); return { key: { id: '3EB0SYNTHETIC' } }; },
    api: { receipt: async (execution, value) => { receipts.push({ execution, ...value }); } }, ...overrides });
  return { worker, journal, sends, receipts };
}
const job = { execution_id: 'execution-1', message_id: 'message-1', phone: '+4712345678', body: 'A dynamically composed test message.' };

test('API credentials can only be sent to a loopback origin without embedded credentials', () => {
  assert.equal(localApiUrl('http://127.0.0.1:3001'), 'http://127.0.0.1:3001');
  assert.equal(localApiUrl('http://[::1]:3001'), 'http://[::1]:3001');
  for (const url of ['https://example.com', 'http://localhost.attacker.test', 'http://user:secret@localhost',
    'http://localhost/path', 'http://localhost/?secret=yes', 'file:///tmp/bridge'])
    assert.throws(() => localApiUrl(url));
});

test('claimed job sends its exact recipient/body once and persists before transport', async t => {
  const state = await setup(t);
  state.worker.sendMessage = async (...args) => {
    state.sends.push(args);
    const saved = JSON.parse(await readFile(state.journal.path, 'utf8'));
    assert.equal(saved[job.execution_id].status, 'uncertain');
    return { key: { id: '3EB0SYNTHETIC' } };
  };
  await state.worker.consume(job);
  await state.worker.consume(job);
  assert.equal(state.sends.length, 1);
  assert.deepEqual(state.sends[0], ['4712345678@s.whatsapp.net', { text: job.body }, { messageId: '3EB0SYNTHETIC' }]);
  await state.worker.flushReceipts();
  assert.equal(state.receipts.length, 0, 'a pending send is not a provider acknowledgement');
  assert.equal((await stat(state.journal.path)).mode & 0o777, 0o600);
});

test('ambiguous sends remain uncertain and a restart never repeats them', async t => {
  const state = await setup(t);
  state.worker.sendMessage = async () => { state.sends.push(true); throw new Error('private provider details'); };
  await state.worker.consume(job);
  await state.worker.flushReceipts();
  assert.equal(state.receipts[0].status, 'uncertain');
  assert.equal(JSON.stringify(state.receipts).includes('private'), false);
  const restarted = new ReceiptJournal(state.journal.path);
  await restarted.load();
  state.worker.journal = restarted;
  await state.worker.consume(job);
  assert.equal(state.sends.length, 1);
});

test('restart after an interrupted send reconciles uncertainty without a second send', async t => {
  const state = await setup(t);
  await state.worker.consume(job);
  const restarted = new ReceiptJournal(state.journal.path);
  await restarted.load();
  state.worker.journal = restarted;
  await state.worker.consume(job);
  await state.worker.flushReceipts();
  assert.equal(state.sends.length, 1);
  assert.equal(state.receipts[0].detail, 'provider_ack_interrupted');
});

test('actual provider receipts advance status and never regress', async t => {
  const state = await setup(t);
  await state.worker.consume(job);
  await state.worker.record('unrelated-private-chat', 4);
  await state.worker.record('3EB0SYNTHETIC', 2);
  await state.worker.flushReceipts();
  await state.worker.record('3EB0SYNTHETIC', 3);
  await state.worker.flushReceipts();
  await state.worker.record('3EB0SYNTHETIC', 4);
  await state.worker.record('3EB0SYNTHETIC', 2);
  await state.worker.flushReceipts();
  assert.deepEqual(state.receipts.map(value => value.status), ['sent', 'delivered', 'read']);
  assert.equal(state.receipts.every(value => value.provider_reference === '3EB0SYNTHETIC'), true);
  assert.equal(receiptStatus(1), undefined);
  assert.equal(receiptStatus(5), 'read');
});

test('failed local receipt HTTP retries only the acknowledgement', async t => {
  const state = await setup(t);
  let fail = true;
  state.worker.api.receipt = async (execution, value) => {
    state.receipts.push({ execution, ...value });
    if (fail) throw new Error('API disconnected');
  };
  await state.worker.consume(job);
  await state.worker.record('3EB0SYNTHETIC', 3);
  await state.worker.flushReceipts();
  fail = false;
  await state.worker.flushReceipts();
  assert.equal(state.sends.length, 1);
  assert.equal(state.receipts.length, 2);
  assert.equal(state.journal.entries[job.execution_id].acknowledged, true);
});

test('stronger receipt arriving during HTTP is not lost', async t => {
  const state = await setup(t);
  await state.worker.consume(job);
  await state.worker.record('3EB0SYNTHETIC', 2);
  state.worker.api.receipt = async (execution, value) => {
    state.receipts.push({ execution, ...value });
    if (value.status === 'sent') await state.worker.record('3EB0SYNTHETIC', 4);
  };
  await state.worker.flushReceipts();
  assert.equal(state.journal.entries[job.execution_id].acknowledged, false);
  await state.worker.flushReceipts();
  assert.deepEqual(state.receipts.map(value => value.status), ['sent', 'read']);
});

test('provider delivery before send resolves survives later network error', async t => {
  const state = await setup(t);
  state.worker.sendMessage = async () => {
    await state.worker.record('3EB0SYNTHETIC', 3);
    throw new Error('socket closed after receipt');
  };
  await state.worker.consume(job);
  await state.worker.flushReceipts();
  assert.equal(state.receipts[0].status, 'delivered');
});

test('invalid claimed job does not send and rejected receipts require reconciliation', async t => {
  const state = await setup(t);
  await assert.rejects(state.worker.consume({ ...job, phone: 'not-a-phone' }));
  assert.equal(state.sends.length, 0);
  await state.worker.consume(job);
  await state.worker.record('3EB0SYNTHETIC', 2);
  state.worker.api.receipt = async () => { const error = new Error('private response'); error.status = 409; throw error; };
  const messages = []; state.worker.notify = value => messages.push(value);
  await state.worker.flushReceipts();
  await state.worker.flushReceipts();
  assert.equal(state.journal.entries[job.execution_id].blocked, true);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].includes('private'), false);
});
