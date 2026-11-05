import { randomUUID } from 'node:crypto';
import { chmod, mkdir, readFile, unlink, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { BridgeWorker, localApiUrl, ReceiptJournal } from './worker.mjs';
import { privatePairingCodeRequester } from './pairing.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const privateRoot = resolve(root, '.data/whatsapp');
const output = text => process.stdout.write(`${text}\n`);
class StartupError extends Error {}

async function privateFile(path, value) {
  await writeFile(path, value, { mode: 0o600, flag: 'wx' });
}

async function main() {
  process.umask(0o077);
  // libsignal can print cryptographic session objects through console.*.
  // This isolated process emits only its own fixed operational messages.
  for (const key of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[key] = () => {};
  const pairingCode = process.argv.includes('--pair-code');
  const pairing = process.argv.includes('--pair') || pairingCode;
  const apiOrigin = localApiUrl(process.env.OFFICE_WHATSAPP_API_URL || 'http://127.0.0.1:3001');
  await mkdir(privateRoot, { recursive: true, mode: 0o700 });
  await chmod(privateRoot, 0o700);
  const token = (await readFile(resolve(privateRoot, 'bridge-token'), 'utf8')).trim();
  if (token.length < 32) throw new Error('Start the Python API to create its local bridge token');
  const authRoot = resolve(privateRoot, 'auth');
  let registered = false;
  try { registered = JSON.parse(await readFile(resolve(authRoot, 'creds.json'), 'utf8')).registered === true; }
  catch (error) { if (error.code !== 'ENOENT') throw new Error('Local WhatsApp session needs reconciliation'); }
  if (!registered && !pairing) throw new StartupError('No paired session. Run npm --prefix apps/whatsapp run pair and scan the private QR file.');
  const lockPath = resolve(privateRoot, 'worker.lock');
  try { await privateFile(lockPath, String(process.pid)); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const owner = Number(await readFile(lockPath, 'utf8'));
    if (!Number.isInteger(owner) || owner <= 0) throw new Error('Worker lock needs reconciliation');
    try { process.kill(owner, 0); throw new Error('The WhatsApp bridge is already running'); }
    catch (check) { if (check.code !== 'ESRCH') throw check; }
    await unlink(lockPath);
    await privateFile(lockPath, String(process.pid));
  }
  let consumer;
  try { consumer = (await readFile(resolve(privateRoot, 'consumer-id'), 'utf8')).trim(); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    consumer = `personal-${randomUUID()}`;
    await privateFile(resolve(privateRoot, 'consumer-id'), consumer);
  }
  const api = async (path, body) => {
    const response = await fetch(`${apiOrigin}/api/whatsapp${path}`, {
      method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ consumer_id: consumer, ...body }), redirect: 'error', signal: AbortSignal.timeout(4000),
    });
    if (!response.ok) { const error = new Error('Local bridge API request failed'); error.status = response.status; throw error; }
    return response.json();
  };
  let socket, connected = false, stopping = false, reconnectTimer, lastSent = 0;
  const journal = new ReceiptJournal(resolve(privateRoot, 'receipts.json'));
  await journal.load();
  const { default: makeWASocket, DisconnectReason, fetchLatestBaileysVersion, generateMessageIDV2, makeCacheableSignalKeyStore, useMultiFileAuthState } = await import('@whiskeysockets/baileys');
  const { default: pino } = await import('pino');
  const { default: QRCode } = await import('qrcode');
  const logger = pino({ level: 'silent' });
  const worker = new BridgeWorker({ journal, notify: output,
    api: { receipt: (id, receipt) => api(`/jobs/${encodeURIComponent(id)}/receipt`, receipt) },
    generateId: () => generateMessageIDV2(socket?.user?.id),
    sendMessage: (...args) => {
      if (!connected || stopping) throw new Error('WhatsApp disconnected before send');
      return socket.sendMessage(...args);
    },
  });
  const heartbeat = () => api('/heartbeat', { connected }).catch(() => {});
  const qrPath = resolve(privateRoot, 'pairing.png');
  const codePath = resolve(privateRoot, 'pairing-code');
  const requestCode = privatePairingCodeRequester({ phonePath: resolve(privateRoot, 'pairing-phone'), codePath, notify: output });
  let pairingCodeRequested = false, pairingEstablished = false;
  let reconnectDelay = 1000;
  async function connect() {
    if (stopping) return;
    const { state, saveCreds } = await useMultiFileAuthState(authRoot);
    const { version } = await fetchLatestBaileysVersion();
    const current = makeWASocket({ version, logger, auth: { creds: state.creds, keys: makeCacheableSignalKeyStore(state.keys, logger) },
      syncFullHistory: false, shouldSyncHistoryMessage: () => false, fireInitQueries: false,
      markOnlineOnConnect: false, maxMsgRetryCount: 0, enableAutoSessionRecreation: false,
      enableRecentMessageCache: false, getMessage: async () => undefined,
      generateHighQualityLinkPreview: false,
      shouldIgnoreJid: jid => ['@g.us', '@broadcast', '@newsletter'].some(suffix => jid.endsWith(suffix)),
    });
    socket = current;
    current.ev.on('creds.update', () => { void saveCreds().catch(() => stop('Session could not be saved. No sends will be retried.')); });
    current.ev.on('connection.update', update => {
      if (socket !== current || stopping) return;
      if (update.isNewLogin) pairingEstablished = true;
      if (update.qr) {
        if (!pairing) { void stop('WhatsApp needs pairing. Run the pair command.'); return; }
        if (pairingCode) {
          if (!pairingCodeRequested && !state.creds.registered) {
            pairingCodeRequested = true;
            void requestCode(current).catch(() => stop('Pairing code could not be requested. Check the private pairing-phone file and restart the pair-code command.'));
          }
        } else {
          void QRCode.toFile(qrPath, update.qr, { width: 480, margin: 2 }).then(async () => {
            await chmod(qrPath, 0o600); output(`Pair WhatsApp using the private QR image: ${qrPath}`);
          }).catch(() => stop('The private pairing image could not be saved.'));
        }
      }
      if (update.connection === 'open') {
        connected = true; reconnectDelay = 1000;
        void unlink(qrPath).catch(() => {});
        void unlink(codePath).catch(() => {});
        output('WhatsApp bridge connected. Only authorized queued messages will be sent.');
        void heartbeat();
      }
      if (update.connection === 'close') {
        connected = false; void heartbeat();
        const disconnectStatus = update.lastDisconnect?.error?.output?.statusCode;
        if (pairing) output(`WhatsApp pairing connection closed (status ${Number.isInteger(disconnectStatus) ? disconnectStatus : 'unknown'}).`);
        if (disconnectStatus === DisconnectReason.loggedOut) {
          void stop('WhatsApp session logged out. Pair again before sending.'); return;
        }
        if (pairingCode && pairingCodeRequested && !pairingEstablished) {
          void stop('Pairing code connection ended. Restart the pair-code command to request a fresh code.'); return;
        }
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(() => { void connect().catch(() => stop('WhatsApp reconnection failed. No sends were retried.')); }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
      }
    });
    current.ev.on('messages.update', updates => {
      for (const { key, update } of updates) {
        if (key.fromMe !== false && key.id && typeof update.status === 'number')
          void worker.record(key.id, update.status).catch(() => stop('Receipt journal could not be saved.'));
      }
    });
    // No message/history/contact handlers: inbound chats never enter the product.
  }
  let heartbeatTimer;
  async function stop(message) {
    if (stopping) return;
    stopping = true; connected = false; clearTimeout(reconnectTimer); clearInterval(heartbeatTimer);
    if (message) output(message);
    await heartbeat();
    try { await socket?.end(undefined); } catch {}
    await unlink(lockPath).catch(() => {});
  }
  process.once('SIGINT', () => { void stop('WhatsApp bridge stopped.'); });
  process.once('SIGTERM', () => { void stop('WhatsApp bridge stopped.'); });
  try {
    await heartbeat();
    await connect();
    heartbeatTimer = setInterval(() => { void heartbeat(); }, 5000);
    while (!stopping) {
      await worker.flushReceipts();
      if (connected && Date.now() - lastSent >= 1000) {
        let value;
        try {
          value = await api('/jobs/claim', {});
        } catch (error) {
          if (error.status === 401 || error.status === 403) { await stop('Local bridge authorization failed.'); break; }
        }
        if (value?.jobs?.length) {
          lastSent = Date.now();
          await worker.consume(value.jobs[0]);
        }
      }
      await delay(1000);
    }
  } finally { await stop(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    // Never print third-party exception objects: they can contain session material.
    process.stderr.write(error instanceof StartupError ? `${error.message}\n` : 'WhatsApp bridge could not start. Check local API, pairing, and private worker files.\n');
    process.exitCode = 1;
  });
}
