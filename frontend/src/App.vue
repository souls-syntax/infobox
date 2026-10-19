<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

type Draft = { id: number; content: string; status: string; source: string; updated_at: string }
type Email = { id: number; sender: string; sender_email: string; recipient: string; subject: string; body: string; body_html?: string | null; received_at: string; classification: string | null; classification_source: string | null; classification_status: string; draft: Draft | null }
type Status = { gmail: { connected: boolean; configured: boolean }; classifier: { available: boolean; model: string; reason: string | null }; reply_model: { available: boolean; model: string; running: boolean; reason: string | null } }
const emails = ref<Email[]>([])
const selectedId = ref<number | null>(null)
const status = ref<Status | null>(null)
const page = ref<'inbox' | 'settings'>('inbox')
const loading = ref(false)
const busy = ref('')
const notice = ref('')
const error = ref('')
const draftText = ref('')
const syncProgress = ref('')
const remoteImagesAllowed = ref(new Set<number>())
const selected = computed(() => emails.value.find(e => e.id === selectedId.value) ?? null)
const hasDraft = computed(() => Boolean(selected.value?.draft))

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`)
  return data as T
}
function fail(reason: unknown) { error.value = reason instanceof Error ? reason.message : 'Something went wrong.'; notice.value = '' }
function ok(text: string) { notice.value = text; error.value = '' }
async function refresh() {
  loading.value = true
  try { [emails.value, status.value] = await Promise.all([api<Email[]>('/emails'), api<Status>('/status')]) }
  catch (e) { fail(e) } finally { loading.value = false }
}
async function sync() {
  busy.value = 'sync'
  syncProgress.value = 'Connecting to Gmail…'
  try {
    const response = await fetch('/api/sync', { method: 'POST' })
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}))
      throw new Error(data.detail || `Sync failed (${response.status})`)
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let pending = ''
    let finished = false
    while (true) {
      const { value, done } = await reader.read()
      pending += decoder.decode(value, { stream: !done })
      const events = pending.split('\n\n')
      pending = events.pop() || ''
      for (const raw of events) {
        const line = raw.split('\n').find(part => part.startsWith('data: '))
        if (!line) continue
        const item = JSON.parse(line.slice(6))
        if (item.type === 'start') syncProgress.value = item.total ? `Found ${item.total} new message${item.total === 1 ? '' : 's'}…` : `Inbox is up to date (${item.already_cached} already cached).`
        if (item.type === 'progress' || item.type === 'item_error') syncProgress.value = `${item.processed}/${item.total} · ${item.message}`
        if (item.type === 'email') {
          const at = emails.value.findIndex(email => email.id === item.email.id)
          if (at < 0) emails.value = [item.email, ...emails.value].sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at))
          else emails.value[at] = item.email
          syncProgress.value = `${item.processed}/${item.total} · ${item.message}: ${item.email.subject}`
        }
        if (item.type === 'error') throw new Error(item.message)
        if (item.type === 'complete') {
          finished = true
          syncProgress.value = ''
          ok(`Sync complete · ${item.stored} saved from ${item.fetched} fetched.`)
          if (item.errors.length) error.value = item.errors.join(' ')
        }
      }
      if (done) break
    }
    if (!finished) throw new Error('Sync ended before Gmail confirmed completion. Retry sync to check for remaining messages.')
    await refresh()
  } catch (e) { fail(e) } finally { busy.value = ''; syncProgress.value = '' }
}
async function selectEmail(email: Email) {
  selectedId.value = email.id
  draftText.value = email.draft?.content ?? ''
  notice.value = ''; error.value = ''
  try {
    const detail = await api<Email>(`/emails/${email.id}`)
    const index = emails.value.findIndex(item => item.id === email.id)
    if (index >= 0) emails.value[index] = detail
    if (selectedId.value === email.id) draftText.value = detail.draft?.content ?? ''
  } catch (e) { if (selectedId.value === email.id) fail(e) }
}
function emailDocument(html: string, allowRemoteImages: boolean) {
  const imageSources = allowRemoteImages ? 'data: https:' : 'data:'
  const policy = `default-src 'none'; img-src ${imageSources}; style-src 'unsafe-inline'; font-src data:; media-src data:;`
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><meta name="viewport" content="width=device-width,initial-scale=1"><style>html{color-scheme:light}body{font:14px/1.6 system-ui,sans-serif;color:#343a31;margin:14px;overflow-wrap:anywhere}img,video{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body>${html}</body></html>`
}
async function reclassify() {
  if (!selected.value) return
  busy.value = 'classify'
  try { const result = await api<{ classification: string }>(`/emails/${selected.value.id}/reclassify`, { method: 'POST' }); await refresh(); ok(`Classified as ${result.classification}.`) }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function generate() {
  if (!selected.value) return
  busy.value = 'generate'
  try { const draft = await api<Draft>(`/emails/${selected.value.id}/draft/generate`, { method: 'POST' }); await refresh(); draftText.value = draft.content; ok('Draft generated by the local Qwen model and saved.') }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function save() {
  if (!selected.value) return
  busy.value = 'save'
  try { const draft = await api<Draft>(`/emails/${selected.value.id}/draft`, { method: 'PUT', body: JSON.stringify({ content: draftText.value }) }); await refresh(); draftText.value = draft.content; ok('Draft saved locally.') }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function approve() {
  if (!selected.value) return
  busy.value = 'approve'
  try { await save(); if (error.value) return; await api<Draft>(`/emails/${selected.value.id}/draft/approve`, { method: 'POST' }); await refresh(); ok('Draft approved. It has not been sent.') }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function discard() {
  if (!selected.value || !confirm('Discard this local draft?')) return
  busy.value = 'discard'
  try { await api(`/emails/${selected.value.id}/draft`, { method: 'DELETE' }); await refresh(); draftText.value = ''; ok('Draft discarded.') }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function send() {
  if (!selected.value || !confirm(`Send this reply to ${selected.value.sender_email}? This will send an email through Gmail.`)) return
  busy.value = 'send'
  try { await api(`/emails/${selected.value.id}/send`, { method: 'POST' }); ok('Gmail confirmed the message was sent.'); await refresh() }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function connect() {
  try { const result = await api<{ url: string }>('/auth/start'); window.location.assign(result.url) } catch (e) { fail(e) }
}
async function clearLocal() {
  if (!confirm('Delete all cached emails and drafts from this device? Gmail itself and its connection will remain unchanged.')) return
  busy.value = 'delete'
  try { await api('/local-data', { method: 'DELETE' }); selectedId.value = null; draftText.value = ''; await refresh(); ok('Local emails and drafts deleted. Gmail connection remains connected.') }
  catch (e) { fail(e) } finally { busy.value = '' }
}
async function copyDraft() { try { await navigator.clipboard.writeText(draftText.value); ok('Draft copied to clipboard.') } catch { fail('Clipboard access was unavailable.') } }
function date(value: string) { return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
onMounted(async () => { if (new URLSearchParams(location.search).has('connected')) { history.replaceState({}, '', '/'); ok('Gmail connected successfully.') }; await refresh() })
</script>

<template>
  <div class="app-shell">
    <aside class="rail">
      <div class="brand-mark">M</div><div class="rail-rule"></div>
      <button class="rail-button" :class="{ active: page === 'inbox' }" title="Inbox" @click="page = 'inbox'">▤</button>
      <button class="rail-button bottom" :class="{ active: page === 'settings' }" title="Settings" @click="page = 'settings'">⚙</button>
    </aside>
    <main class="main-area">
      <header class="topbar"><div><div class="eyebrow">LOCAL MAIL</div><div class="top-title">{{ page === 'inbox' ? 'Inbox' : 'Settings' }}</div></div><div class="top-actions"><span class="privacy"><i></i> Private by design</span><button v-if="page === 'inbox'" class="button primary" :disabled="busy === 'sync' || !status?.gmail.connected" @click="sync">{{ busy === 'sync' ? 'Syncing…' : '↻ Sync inbox' }}</button><button v-else class="button" @click="refresh">Refresh status</button></div></header>
      <div v-if="notice || error" class="toast" :class="{ bad: !!error }">{{ error || notice }}<button @click="error = ''; notice = ''">×</button></div>
      <section v-if="page === 'settings'" class="settings-page">
        <div class="page-intro"><div class="eyebrow">CONNECTIONS & MODELS</div><h1>Everything stays on this device.</h1><p>Gmail is used only to sync messages and send a reply after you confirm. Both AI steps run locally.</p></div>
        <article class="setting-card"><div class="card-icon">@</div><div class="setting-info"><h2>Gmail connection</h2><p>{{ status?.gmail.connected ? 'Your Google account is connected.' : status?.gmail.configured ? 'Connect your account to fetch inbox messages.' : 'OAuth client credentials are missing from local .env.' }}</p></div><div class="setting-action"><span class="state" :class="status?.gmail.connected ? 'good' : ''"><i></i>{{ status?.gmail.connected ? 'Connected' : 'Not connected' }}</span><button class="button primary" :disabled="!status?.gmail.configured" @click="connect">{{ status?.gmail.connected ? 'Reconnect' : 'Connect Gmail' }}</button></div></article>
        <article class="setting-card"><div class="card-icon violet">✳</div><div class="setting-info"><h2>Local classifier</h2><p>{{ status?.classifier.model || 'Checking…' }}</p><small>DeBERTa runs on this computer. Used only to categorize emails.</small><small v-if="status?.classifier.reason" class="err-text">{{ status.classifier.reason }}</small></div><div class="setting-action"><span class="state" :class="status?.classifier.available ? 'good' : 'warn'"><i></i>{{ status?.classifier.available ? 'Model found' : 'Unavailable' }}</span></div></article>
        <article class="setting-card"><div class="card-icon gold">✦</div><div class="setting-info"><h2>Local reply model</h2><p>{{ status?.reply_model.model || 'Checking…' }}</p><small>Qwen GGUF through llama-server. Used only to write reply drafts.</small><small v-if="status?.reply_model.reason" class="err-text">{{ status.reply_model.reason }}</small></div><div class="setting-action"><span class="state" :class="status?.reply_model.available ? 'good' : 'warn'"><i></i>{{ status?.reply_model.running ? 'Running' : status?.reply_model.available ? 'Ready to start' : 'Unavailable' }}</span></div></article>
        <article class="setting-card danger-card"><div class="card-icon red">⌫</div><div class="setting-info"><h2>Delete local email data</h2><p>Remove every cached message and draft from this device. Gmail and its connection are untouched.</p></div><button class="button danger" :disabled="busy === 'delete'" @click="clearLocal">{{ busy === 'delete' ? 'Deleting…' : 'Delete local data' }}</button></article>
        <div class="settings-foot">Model and Gmail status is checked locally. Credentials and message content are never shown here.</div>
      </section>
      <section v-else class="inbox-layout">
        <aside class="mail-list"><div class="list-heading"><div><h1>Inbox</h1><p>{{ emails.length }} cached {{ emails.length === 1 ? 'message' : 'messages' }}</p><p v-if="syncProgress" class="sync-progress" role="status">{{ syncProgress }}</p></div><span class="list-filter">ALL</span></div>
          <div v-if="loading" class="empty-list">Loading local inbox…</div><div v-else-if="!emails.length" class="empty-list"><div class="empty-art">✉</div><strong>Your inbox is ready.</strong><p>Connect Gmail in Settings, then sync to fetch messages to this device.</p><button class="text-button" @click="page = 'settings'">Open settings →</button></div>
          <button v-for="email in emails" :key="email.id" class="mail-row" :class="{ selected: selectedId === email.id }" @click="selectEmail(email)"><div class="avatar">{{ (email.sender || '?').slice(0, 1).toUpperCase() }}</div><div class="mail-copy"><div class="mail-sender">{{ email.sender || email.sender_email || 'Unknown sender' }}<span>{{ date(email.received_at) }}</span></div><div class="mail-subject">{{ email.subject }}</div><div class="mail-preview">{{ email.body.slice(0, 78).replace(/\s+/g, ' ') || 'No plain text body' }}</div><span v-if="email.classification" class="tag">{{ email.classification }}</span><span v-else class="tag muted">{{ email.classification_status === 'failed' ? 'Classification failed' : 'Unclassified' }}</span></div></button>
        </aside>
        <article v-if="selected" class="detail"><div class="detail-head"><span class="detail-kicker">MESSAGE</span><span class="detail-date">{{ new Date(selected.received_at).toLocaleString() }}</span></div><h1 class="subject">{{ selected.subject }}</h1><div class="sender-line"><div class="avatar large">{{ (selected.sender || '?').slice(0,1).toUpperCase() }}</div><div><strong>{{ selected.sender || selected.sender_email }}</strong><small>{{ selected.sender_email }}</small></div><span v-if="selected.classification" class="tag">{{ selected.classification }}</span></div><div v-if="selected.classification_source" class="model-note">Classified locally · {{ selected.classification_source }}</div><div v-if="selected.body_html" class="html-mail"><div class="remote-images-note"><span>Remote images are blocked until you choose to load them.</span><button v-if="!remoteImagesAllowed.has(selected.id)" class="text-button" @click="remoteImagesAllowed = new Set(remoteImagesAllowed).add(selected!.id)">Load remote images</button><button v-else class="text-button" @click="remoteImagesAllowed = new Set([...remoteImagesAllowed].filter(id => id !== selected!.id))">Block remote images</button></div><iframe class="email-frame" :srcdoc="emailDocument(selected.body_html, remoteImagesAllowed.has(selected.id))" sandbox="" referrerpolicy="no-referrer" title="Email content"></iframe></div><pre v-else class="message-body">{{ selected.body || '(No plain text content was found.)' }}</pre>
          <div class="reply"><div class="reply-heading"><div><div class="eyebrow">YOUR REPLY</div><h2>Draft a response</h2></div><span v-if="selected.draft" class="draft-state">{{ selected.draft.status }} · {{ selected.draft.source }}</span></div><div v-if="!hasDraft" class="generate-card"><div class="spark">✦</div><div><strong>Write a reply with local Qwen</strong><p>The message stays on this device during generation.</p></div><button class="button primary" :disabled="busy === 'generate' || !status?.reply_model.available" @click="generate">{{ busy === 'generate' ? 'Generating…' : 'Generate draft' }}</button></div><template v-else><textarea v-model="draftText" rows="7" aria-label="Editable reply draft" placeholder="Write your reply…"></textarea><div class="draft-tools"><span>Editable · saved on this device</span><button class="text-button" @click="copyDraft">Copy draft</button></div><div class="draft-actions"><button class="button" :disabled="busy === 'generate'" @click="generate">{{ busy === 'generate' ? 'Generating…' : '↻ Regenerate' }}</button><div class="action-right"><button class="button" :disabled="busy === 'save'" @click="save">Save</button><button class="button" :disabled="busy === 'discard'" @click="discard">Discard</button><button class="button approve" :disabled="busy === 'approve'" @click="approve">{{ busy === 'approve' ? 'Saving…' : 'Approve' }}</button><button class="button primary" :disabled="selected.draft?.status !== 'approved' || draftText !== selected.draft?.content || busy === 'send'" @click="send">{{ busy === 'send' ? 'Sending…' : 'Send…' }}</button></div></div><div class="send-note">Approval saves the draft. Any edits must be saved and approved again before sending.</div></template><button v-if="selected" class="text-button reclassify" :disabled="busy === 'classify' || !status?.classifier.available" @click="reclassify">{{ busy === 'classify' ? 'Classifying…' : '↻ Reclassify this message' }}</button></div>
        </article>
        <article v-else class="detail detail-empty"><div class="detail-empty-mark">↖</div><strong>Select a message to read</strong><p>Your local inbox is private to this device.</p></article>
      </section>
    </main>
  </div>
</template>
