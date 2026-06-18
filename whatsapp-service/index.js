/**
 * index.js
 * ========
 * Baileys WhatsApp microservice for the stock screener.
 *
 *  - On first run, prints a QR code in the terminal to link WhatsApp Web.
 *  - Persists the session in ./auth_store so restarts don't need re-scanning.
 *  - Exposes POST /send-message  { phone, message } -> { success, messageId }.
 *  - Serialises outgoing messages through a queue with a 2s gap to avoid bans.
 *  - Auto-reconnects with exponential backoff on connection drops.
 */

const path = require('path');
const express = require('express');
const pino = require('pino');
const qrcode = require('qrcode-terminal');
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
} = require('@whiskeysockets/baileys');

const PORT = process.env.PORT || 3001;
const AUTH_DIR = path.join(__dirname, 'auth_store');
const SEND_GAP_MS = 2000; // 2 seconds between consecutive messages

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

let sock = null;
let isConnected = false;
let reconnectAttempts = 0;

// --- Outgoing message queue (serialised, rate-limited) -------------------- //

const queue = [];
let draining = false;

function enqueueSend(phone, message) {
  return new Promise((resolve) => {
    queue.push({ phone, message, resolve });
    drainQueue();
  });
}

async function drainQueue() {
  if (draining) return;
  draining = true;
  while (queue.length > 0) {
    const job = queue[0];
    let result;
    try {
      result = await doSend(job.phone, job.message);
    } catch (err) {
      logger.error({ err: err.message }, 'send failed');
      result = { success: false, error: err.message };
    }
    job.resolve(result);
    queue.shift();
    if (queue.length > 0) {
      await sleep(SEND_GAP_MS); // rate-limit gap before the next message
    }
  }
  draining = false;
}

async function doSend(phone, message) {
  if (!isConnected || !sock) {
    return { success: false, error: 'whatsapp_not_connected' };
  }
  const jid = `${String(phone).replace(/\D/g, '')}@s.whatsapp.net`;
  const sent = await sock.sendMessage(jid, { text: message });
  const messageId = sent?.key?.id || null;
  logger.info({ phone, messageId, ts: new Date().toISOString() }, 'message sent');
  return { success: true, messageId };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// --- WhatsApp connection -------------------------------------------------- //

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false, // we render it ourselves below
    logger: pino({ level: 'silent' }),
    browser: ['StockScreener', 'Chrome', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
      qrcode.generate(qr, { small: true });
    }

    if (connection === 'open') {
      isConnected = true;
      reconnectAttempts = 0;
      logger.info('WhatsApp connection open');
      drainQueue(); // flush anything queued while disconnected
    } else if (connection === 'close') {
      isConnected = false;
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      logger.warn({ statusCode, loggedOut }, 'WhatsApp connection closed');

      if (loggedOut) {
        logger.error('Logged out. Delete auth_store/ and restart to re-link.');
        return;
      }
      // Exponential backoff reconnect.
      reconnectAttempts += 1;
      const backoff = Math.min(60000, 1000 * 2 ** reconnectAttempts);
      logger.info(`Reconnecting in ${backoff / 1000}s (attempt ${reconnectAttempts})`);
      setTimeout(startSocket, backoff);
    }
  });

  return sock;
}

// --- HTTP API ------------------------------------------------------------- //

const app = express();
app.use(express.json({ limit: '1mb' }));

app.get('/health', (req, res) => {
  res.json({ status: 'ok', connected: isConnected, queued: queue.length });
});

app.post('/send-message', async (req, res) => {
  const { phone, message } = req.body || {};
  if (!phone || !message) {
    return res.status(400).json({ success: false, error: 'phone and message are required' });
  }
  const result = await enqueueSend(phone, message);
  const status = result.success ? 200 : 503;
  return res.status(status).json(result);
});

async function main() {
  await startSocket();
  app.listen(PORT, () => {
    logger.info(`WhatsApp service HTTP API listening on http://localhost:${PORT}`);
    console.log(`\n✅ WhatsApp service running on port ${PORT}`);
    console.log('   POST /send-message  { phone, message }');
    console.log('   GET  /health\n');
  });
}

// Graceful shutdown.
process.on('SIGINT', () => {
  logger.info('SIGINT received, shutting down');
  process.exit(0);
});
process.on('SIGTERM', () => {
  logger.info('SIGTERM received, shutting down');
  process.exit(0);
});

main().catch((err) => {
  logger.error({ err: err.message }, 'fatal startup error');
  process.exit(1);
});
