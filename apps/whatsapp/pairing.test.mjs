import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { privatePairingCodeRequester } from './pairing.mjs';

async function setup(t, phone = '+4712345678') {
  const directory = await mkdtemp(join(tmpdir(), 'office-whatsapp-pairing-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const phonePath = join(directory, 'pairing-phone'), codePath = join(directory, 'pairing-code');
  await writeFile(phonePath, phone, { mode: 0o600 });
  const notifications = [], calls = [];
  const request = privatePairingCodeRequester({ phonePath, codePath, notify: value => notifications.push(value) });
  const socket = { requestPairingCode: async digits => { calls.push(digits); await Promise.resolve(); return 'TEST1234'; } };
  return { request, socket, codePath, notifications, calls };
}

test('one explicit code request uses private digits and saves only a private code file', async t => {
  const state = await setup(t);
  await Promise.all([state.request(state.socket), state.request(state.socket)]);
  await state.request(state.socket);
  assert.deepEqual(state.calls, ['4712345678']);
  assert.equal(await readFile(state.codePath, 'utf8'), 'TEST1234\n');
  assert.equal((await stat(state.codePath)).mode & 0o777, 0o600);
  assert.equal(state.notifications.length, 1);
  assert.equal(state.notifications[0].includes(state.codePath), true);
  assert.equal(state.notifications[0].includes('TEST1234'), false);
  assert.equal(state.notifications[0].includes('4712345678'), false);
});

test('invalid private phone never makes a provider request', async t => {
  const state = await setup(t, 'not-a-phone');
  await assert.rejects(state.request(state.socket), /E.164/);
  assert.equal(state.calls.length, 0);
});

test('a failed provider request is not automatically repeated', async t => {
  const state = await setup(t);
  let calls = 0;
  state.socket.requestPairingCode = async () => { calls++; throw new Error('private provider error'); };
  await assert.rejects(state.request(state.socket));
  assert.equal(await state.request(state.socket), false);
  assert.equal(calls, 1);
  assert.equal(state.notifications.length, 0);
});
