/**
 * Wa Gateway - Baileys lightweight gateway
 * Endpoint untuk ethol-notification.py:
 *   POST /send { number: "628xxx", text: "..." }
 *   GET  /status -> { connected: bool, qr: string|null }
 *   GET  /qr -> HTML dengan QR image (scan via HP)
 *   GET  /qr.png -> PNG image langsung
 *   GET  /qr.txt -> raw string (debug)
 */
const express = require('express');
const pino = require('pino');
const qrcodeTerm = require('qrcode-terminal');
const QRCode = require('qrcode');
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');

const PORT = process.env.PORT || 3000;
const AUTH_DIR = process.env.AUTH_DIR || './auth_info';
const API_KEY = process.env.API_KEY || '';

const logger = pino({ level: 'info' });
const app = express();
app.use(express.json());

let sock = null;
let qrString = null;
let isConnected = false;

app.use((req, res, next) => {
  if (!API_KEY) return next();
  if (req.path === '/health') return next();
  const key = req.headers['x-api-key'] || req.headers['apikey'] || req.query.key;
  if (key !== API_KEY) return res.status(401).json({ error: 'invalid api key' });
  next();
});

app.get('/health', (req, res) => res.json({ ok: true, connected: isConnected }));
app.get('/status', (req, res) => res.json({ connected: isConnected, hasQR: !!qrString }));

// HTML QR - buka di browser, langsung scan
app.get('/qr', async (req, res) => {
  if (isConnected) return res.send('<h2>✅ Sudah connected</h2><p>WhatsApp terhubung, tidak perlu scan lagi.</p>');
  if (!qrString) return res.status(404).send('<h2>QR belum siap</h2><p>Tunggu 3-5 detik lalu refresh halaman ini.</p>');
  try {
    const dataUrl = await QRCode.toDataURL(qrString, { width: 300, margin: 2 });
    res.type('html').send(`
      <html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Scan WA QR</title></head>
      <body style="font-family:sans-serif;text-align:center;padding:20px">
        <h2>Scan QR WhatsApp</h2>
        <p>WhatsApp > Perangkat Tertaut > Tautkan Perangkat > Scan</p>
        <img src="${dataUrl}" style="border:1px solid #ccc;padding:10px" />
        <p style="color:#888;font-size:12px">QR refresh otomatis tiap 30 detik. Jika expired, refresh halaman.</p>
        <p><a href="/qr.png" target="_blank">Buka PNG langsung</a> | <a href="/qr.txt" target="_blank">Raw string</a></p>
        <script>setTimeout(()=>location.reload(),30000)</script>
      </body></html>
    `);
  } catch (e) {
    res.status(500).send('Gagal generate QR: ' + e);
  }
});

app.get('/qr.png', async (req, res) => {
  if (isConnected) return res.status(400).send('Already connected');
  if (!qrString) return res.status(404).send('QR not yet generated');
  try {
    const buf = await QRCode.toBuffer(qrString, { width: 300, margin: 2 });
    res.type('png').send(buf);
  } catch (e) {
    res.status(500).send(String(e));
  }
});

app.get('/qr.txt', (req, res) => {
  if (isConnected) return res.send('Already connected');
  if (!qrString) return res.status(404).send('QR not yet generated');
  res.type('text').send(qrString);
});

app.post('/send', async (req, res) => {
  const { number, text } = req.body;
  if (!number || !text) return res.status(400).json({ error: 'number & text required' });
  if (!isConnected || !sock) return res.status(503).json({ error: 'whatsapp not connected, scan QR dulu di /qr atau lihat log' });
  let jid = number.replace(/\D/g, '');
  if (jid.startsWith('08')) jid = '62' + jid.slice(1);
  if (!jid.includes('@')) jid = jid + '@s.whatsapp.net';
  try {
    const result = await sock.sendMessage(jid, { text });
    logger.info({ jid, id: result?.key?.id }, 'pesan terkirim');
    res.json({ ok: true, id: result?.key?.id });
  } catch (e) {
    logger.error(e, 'gagal kirim');
    res.status(500).json({ error: String(e) });
  }
});

async function startWA() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  sock = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
    printQRInTerminal: false,
    browser: ['ethol-notifier', 'Chrome', '1.0'],
  });
  sock.ev.on('creds.update', saveCreds);
  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      qrString = qr;
      console.log('\n=== SCAN QR: buka http://localhost:' + PORT + '/qr di browser ===\n');
      qrcodeTerm.generate(qr, { small: true });
      console.log('\nAtau di home server: curl http://localhost:' + PORT + '/qr.png > qr.png\n');
    }
    if (connection === 'close') {
      const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
      isConnected = false;
      qrString = null;
      console.log('Connection closed, reconnect:', shouldReconnect, lastDisconnect?.error);
      if (shouldReconnect) setTimeout(startWA, 3000);
      else console.log('Logged out, hapus folder auth_info dan restart untuk scan ulang');
    } else if (connection === 'open') {
      isConnected = true;
      qrString = null;
      console.log('✅ WhatsApp connected!');
    }
  });
}

app.listen(PORT, () => {
  console.log(`wa-gateway listening on :${PORT}`);
  console.log(`
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 PANDUAN SETELAH docker compose up
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Buka QR di browser: http://localhost:${PORT}/qr
   (atau PNG: http://localhost:${PORT}/qr.png)
   Scan: WhatsApp > Perangkat Tertaut > Tautkan

2. Cek status: curl http://localhost:${PORT}/status
   → {"connected":true} = siap

3. Cek notifier: docker logs -f ethol_notifier
   → "Baseline X disimpan" = sukses

QR auto-refresh 30 detik, jika expired refresh halaman.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`);
  startWA();
});
