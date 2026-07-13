// Always-on WhatsApp bridge (Baileys). Links this pipeline as a WhatsApp
// companion device, captures history-sync batches and live messages, and
// appends them to a per-day JSONL store that scripts/fetch_whatsapp.py reads
// each night. It also publishes pairing status + a QR image and a discovered-
// chats list for the dashboard. It NEVER sends a message - read-only archiving.
//
// Files it writes under /work/whatsapp:
//   auth/                 Baileys multi-file auth state (the linked-device creds)
//   messages/<YYYY-MM-DD>.jsonl   one JSON message per line, bucketed by SGT date
//   chats.json            [{jid, name, is_group}] - discovered chats
//   status.json           {state, me, last_event_ts, history_sync_done}
//   qr.png                current pairing QR (present only while waiting_qr)

import makeWASocket, {
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  Browsers,
} from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode'
import fs from 'fs'
import path from 'path'

const TZ = process.env.TZ || 'Asia/Singapore'
const DATA_DIR = process.env.WHATSAPP_DATA_DIR || '/work/whatsapp'
const AUTH_DIR = path.join(DATA_DIR, 'auth')
const MSG_DIR = path.join(DATA_DIR, 'messages')
const CHATS_FILE = path.join(DATA_DIR, 'chats.json')
const STATUS_FILE = path.join(DATA_DIR, 'status.json')
const QR_FILE = path.join(DATA_DIR, 'qr.png')
const SYNC_FULL = process.env.WHATSAPP_SYNC_FULL_HISTORY !== '0'
const NTFY_TOPIC = (process.env.NTFY_TOPIC || '').trim()

const logger = pino({ level: process.env.WHATSAPP_LOG_LEVEL || 'warn' })

for (const d of [DATA_DIR, AUTH_DIR, MSG_DIR]) fs.mkdirSync(d, { recursive: true })

// --- small persistent-ish state ---
const chatNames = new Map()   // jid -> display name
const seenIds = new Set()     // "jid:id" appended this process (bounds restart dup-storms)
let me = null
let historySyncDone = false
let chatsDirty = false

const dateFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
})
function sgtDate(tsSeconds) {
  return dateFmt.format(new Date(tsSeconds * 1000))  // en-CA => YYYY-MM-DD
}

function writeStatus(state, extra = {}) {
  const status = {
    state,
    me,
    history_sync_done: historySyncDone,
    last_event_ts: Math.floor(Date.now() / 1000),
    ...extra,
  }
  try { fs.writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2)) } catch (e) { logger.error(e) }
}

function flushChats() {
  if (!chatsDirty) return
  chatsDirty = false
  const rows = []
  for (const [jid, name] of chatNames.entries()) {
    rows.push({ jid, name: name || '', is_group: jid.endsWith('@g.us') })
  }
  try { fs.writeFileSync(CHATS_FILE, JSON.stringify(rows, null, 2)) } catch (e) { logger.error(e) }
}
setInterval(flushChats, 5000)

function rememberChat(jid, name) {
  if (!jid) return
  const prev = chatNames.get(jid)
  if (name && name !== prev) { chatNames.set(jid, name); chatsDirty = true }
  else if (!chatNames.has(jid)) { chatNames.set(jid, prev || ''); chatsDirty = true }
}

async function notifyLoggedOut() {
  if (!NTFY_TOPIC) return
  try {
    await fetch(`https://ntfy.sh/${NTFY_TOPIC}`, {
      method: 'POST',
      headers: { Title: 'WhatsApp bridge', Priority: 'high', Tags: 'warning' },
      body: 'WhatsApp session logged out - re-pair from the dashboard Connections page.',
    })
  } catch (e) { logger.error(e) }
}

// --- message content extraction ---
function extractContent(message) {
  if (!message) return null
  // Unwrap ephemeral / view-once / edited wrappers.
  if (message.ephemeralMessage) return extractContent(message.ephemeralMessage.message)
  if (message.viewOnceMessage) return extractContent(message.viewOnceMessage.message)
  if (message.viewOnceMessageV2) return extractContent(message.viewOnceMessageV2.message)
  if (message.documentWithCaptionMessage) return extractContent(message.documentWithCaptionMessage.message)

  if (typeof message.conversation === 'string') return { type: 'text', text: message.conversation }
  if (message.extendedTextMessage?.text != null)
    return { type: 'text', text: message.extendedTextMessage.text }
  if (message.imageMessage) return { type: 'image', text: cap('[image]', message.imageMessage.caption) }
  if (message.videoMessage) return { type: 'video', text: cap('[video]', message.videoMessage.caption) }
  if (message.audioMessage)
    return { type: 'audio', text: message.audioMessage.ptt ? '[voice message]' : '[audio]' }
  if (message.documentMessage) {
    const n = message.documentMessage.fileName
    return { type: 'document', text: n ? `[document: ${n}]` : '[document]' }
  }
  if (message.stickerMessage) return { type: 'sticker', text: '[sticker]' }
  if (message.locationMessage) return { type: 'location', text: '[location]' }
  if (message.liveLocationMessage) return { type: 'location', text: '[live location]' }
  if (message.contactMessage || message.contactsArrayMessage) return { type: 'contact', text: '[contact]' }
  if (message.pollCreationMessage || message.pollCreationMessageV3)
    return { type: 'poll', text: cap('[poll]', (message.pollCreationMessage || message.pollCreationMessageV3)?.name) }
  // reactions, protocol updates, poll votes, etc. -> skip
  return null
}
function cap(tag, caption) { return caption ? `${tag} ${caption}` : tag }

function recordFor(m) {
  const content = extractContent(m.message)
  if (!content) return null
  const jid = m.key?.remoteJid
  if (!jid || jid === 'status@broadcast') return null
  const ts = Number(m.messageTimestamp) || 0
  if (!ts) return null
  const isGroup = jid.endsWith('@g.us')
  const fromMe = !!m.key?.fromMe
  const senderJid = fromMe ? (me?.id || 'me') : (m.key?.participant || jid)
  const senderName = fromMe ? 'Me' : (m.pushName || chatNames.get(senderJid) || senderJid.split('@')[0])
  if (!isGroup && m.pushName) rememberChat(jid, m.pushName)
  return {
    id: m.key?.id,
    chat_jid: jid,
    chat_name: chatNames.get(jid) || (isGroup ? jid : senderName),
    is_group: isGroup,
    sender_jid: senderJid,
    sender_name: senderName,
    from_me: fromMe,
    ts,
    type: content.type,
    text: content.text,
  }
}

function appendMessages(msgs) {
  // Group records by SGT date, then append each day's block once.
  const byDate = new Map()
  for (const m of msgs || []) {
    const rec = recordFor(m)
    if (!rec) continue
    const key = `${rec.chat_jid}:${rec.id}`
    if (seenIds.has(key)) continue
    seenIds.add(key)
    const day = sgtDate(rec.ts)
    if (!byDate.has(day)) byDate.set(day, [])
    byDate.get(day).push(rec)
  }
  for (const [day, recs] of byDate.entries()) {
    const line = recs.map((r) => JSON.stringify(r)).join('\n') + '\n'
    try { fs.appendFileSync(path.join(MSG_DIR, `${day}.jsonl`), line) }
    catch (e) { logger.error(e) }
  }
  if (byDate.size) logger.info(`appended ${msgs.length} message(s) across ${byDate.size} day-file(s)`)
}

// --- connection / reconnect ---
let reconnectDelay = 1000
const MAX_RECONNECT_DELAY = 60000

// A start() rejection with no .catch would crash the process (exit 1) and put
// the container in a docker restart loop; every (re)start goes through here so
// failures always land in a logged retry instead.
function scheduleStart(delayMs) {
  setTimeout(() => {
    start().catch((e) => {
      logger.error(e)
      writeStatus('error', { error: String(e?.message || e) })
      scheduleStart(5000)
    })
  }, delayMs)
}

// Baileys occasionally rejects promises nobody awaits (ws sends after close,
// history-sync decode errors); log them instead of letting Node exit 1.
process.on('unhandledRejection', (err) => {
  logger.error(err, 'unhandled rejection (bridge kept alive)')
})

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  const { version } = await fetchLatestBaileysVersion()
  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    syncFullHistory: SYNC_FULL,
    markOnlineOnConnect: false,
    logger,
    browser: Browsers.appropriate('Obsidian Pipeline'),
  })

  sock.ev.on('creds.update', saveCreds)

  sock.ev.on('connection.update', async (u) => {
    const { connection, lastDisconnect, qr } = u
    if (qr) {
      try { await qrcode.toFile(QR_FILE, qr, { width: 320, margin: 1 }) } catch (e) { logger.error(e) }
      writeStatus('waiting_qr')
      logger.warn('QR ready - scan it from the dashboard Connections page.')
    }
    if (connection === 'open') {
      reconnectDelay = 1000
      me = sock.user ? { id: sock.user.id, name: sock.user.name || sock.user.verifiedName || '' } : me
      try { if (fs.existsSync(QR_FILE)) fs.unlinkSync(QR_FILE) } catch (e) {}
      writeStatus('connected')
      logger.warn(`connected as ${me?.name || me?.id}`)
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        writeStatus('logged_out')
        logger.error('logged out - re-pair required.')
        await notifyLoggedOut()
        return  // do not auto-reconnect; user must re-pair
      }
      writeStatus('connecting', { reconnect_in_ms: reconnectDelay })
      logger.warn(`connection closed (code ${code}); reconnecting in ${reconnectDelay}ms`)
      scheduleStart(reconnectDelay)
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY)
    }
  })

  // Name maps.
  const learnChats = (chats) => { for (const c of chats || []) rememberChat(c.id, c.name || c.subject || '') }
  sock.ev.on('chats.upsert', learnChats)
  sock.ev.on('chats.update', learnChats)
  sock.ev.on('contacts.upsert', (cs) => { for (const c of cs || []) rememberChat(c.id, c.name || c.notify || c.verifiedName || '') })
  sock.ev.on('contacts.update', (cs) => { for (const c of cs || []) rememberChat(c.id, c.name || c.notify || c.verifiedName || '') })

  // History sync batches (this is how the one-time backfill arrives).
  sock.ev.on('messaging-history.set', ({ chats, contacts, messages, isLatest, progress }) => {
    learnChats(chats)
    for (const c of contacts || []) rememberChat(c.id, c.name || c.notify || c.verifiedName || '')
    appendMessages(messages)
    if (progress != null) writeStatus(me ? 'connected' : 'connecting', { history_progress: progress })
    if (isLatest) { historySyncDone = true; logger.warn('history sync latest batch received') }
  })

  // Live messages.
  sock.ev.on('messages.upsert', ({ messages, type }) => {
    if (type === 'notify' || type === 'append') appendMessages(messages)
  })
}

writeStatus('connecting')
flushChats()
scheduleStart(0)
