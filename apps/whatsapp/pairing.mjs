import { randomUUID } from 'node:crypto';
import { readFile, rename, writeFile } from 'node:fs/promises';

/** One explicit pairing request per process; no account or code is logged. */
export function privatePairingCodeRequester({ phonePath, codePath, notify }) {
  let requested = false;
  return async socket => {
    if (requested) return false;
    requested = true;
    let phone;
    try { phone = (await readFile(phonePath, 'utf8')).trim(); }
    catch { throw new Error('Private pairing phone file is required'); }
    if (!/^\+[1-9][0-9]{6,14}$/.test(phone)) throw new Error('Private pairing phone must use E.164 format');
    const code = await socket.requestPairingCode(phone.slice(1));
    if (typeof code !== 'string' || !/^[A-Za-z0-9]{8}$/.test(code)) throw new Error('Provider returned an invalid pairing code');
    const temporary = `${codePath}.${randomUUID()}.tmp`;
    await writeFile(temporary, `${code}\n`, { mode: 0o600, flag: 'wx' });
    await rename(temporary, codePath);
    notify(`WhatsApp pairing code saved privately: ${codePath}`);
    return true;
  };
}
