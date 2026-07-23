# Frontend — Code & Lifecycle Explanation

This document explains how the React client works: authentication, onboarding, the
persistent voice shell, the always-on voice loop (client VAD for barge-in + server
STT for end-of-turn), the WebSocket protocol it speaks, the gapless audio playback
engine, reminder polling, and the Tasks UI.

The client is currently a **web test harness** for the backend (the production app
will be a separate mobile client). It is fully functional: sign in with Google,
complete a short onboarding, talk to the assistant, watch tool calls happen live,
and manage the resulting tasks in a dedicated page.

This revision replaces an earlier description built around a single flat chat
transcript and a three-layer SLM→escalate→LLM router — neither exists anymore. The
model layer is a single brain today (see `system_explanation.md` §3), and the
transcript is now split into an ambient one-turn caption plus a separate full-history
overlay (§4).

---

## 1. App shell, auth, and onboarding

```
main.jsx → Auth0Provider → App.jsx → Login | Onboarding | (VoiceChat + TasksPage + BottomNav)
```

### `main.jsx` — Auth0 provider
Wraps the app in `<Auth0Provider>` configured for **persistent sessions**:

```jsx
<Auth0Provider
  domain={VITE_AUTH0_DOMAIN}
  clientId={VITE_AUTH0_CLIENT_ID}
  authorizationParams={{
    redirect_uri: window.location.origin,
    scope: "openid profile email offline_access",
  }}
  cacheLocation="localstorage"
  useRefreshTokens
>
```

- **`offline_access` scope** → Auth0 issues a **refresh token**.
- **`cacheLocation="localstorage"`** → the session survives a full page reload (the
  default in-memory cache does not).
- **`useRefreshTokens`** → silent renewal uses the stored refresh token instead of a
  hidden-iframe check, which most browsers now block.

> The actual session **duration** is set in the Auth0 dashboard (Application →
> Refresh Token Rotation → Absolute / Inactivity Expiration). The code above only
> makes the client correctly *use* a long-lived refresh token.

### `Login.jsx`
A single "Sign in with Google" button that calls
`loginWithRedirect({ authorizationParams: { connection: "google-oauth2" } })` —
skipping Auth0's hosted page and going straight to Google.

### `App.jsx` — the gate sequence
`App.jsx` renders one of five things, in order, based on what's still loading or
missing:

1. **`isLoading`** → a blank `<div className="app" />` (avoids a login flash while
   the Auth0 SDK restores a session from `localStorage`).
2. **not authenticated** → `<Login />`.
3. **token/profile still loading** → blank screen. Getting the token calls
   `getAccessTokenSilently()` first (exercises refresh-token renewal so the token is
   guaranteed fresh) then reads the raw ID token via `getIdTokenClaims().__raw`.
   That raw ID token is the credential passed everywhere else — `?token=` on the WS,
   `Authorization: Bearer` on REST.
4. **`profile === "error"`** → a distinct "couldn't reach the server" retry screen —
   deliberately **not** the same branch as "not onboarded," so a backend outage can
   never look like a fresh account and wrongly re-trigger onboarding.
5. **`!profile.onboarding_complete`** → `<Onboarding />` (§2).
6. **`!launched`** → the branded "Start Conversation" screen (requests mic
   permission on the next click), showing the on-demand `GET /engagement/greeting`
   text if it loaded in time.
7. **the persistent shell** — `<VoiceChat>` always mounted, `<TasksPage>` overlaid
   translucently when the bottom nav is on "tasks," `<BottomNav>` fixed at the
   bottom.

Tasks are fetched and owned by `App.jsx`, **above** the talk/tasks view toggle —
switching views never refetches; the list only reloads when a tool result actually
changed something (`onTaskChange`, wired from `VoiceChat`) or the user hits the
Tasks page's refresh button.

### `Onboarding.jsx`
First-run capture: name (prefilled from the Google profile), an optional location
(typed, or "Detect" via `reverseGeocode()` — browser geolocation → a keyless
reverse-geocoding API → `"City, Country"`), and a daily check-in hour picked from
four presets (Morning/Afternoon/Evening/Night). Submits to
`POST /profile/onboarding` with the browser's IANA timezone
(`Intl.DateTimeFormat().resolvedOptions().timeZone`) attached automatically — the
user never has to state their timezone explicitly.

---

## 2. The persistent shell — why VoiceChat never unmounts

Once onboarded, `App.jsx` mounts `VoiceChat` once and leaves it mounted for the rest
of the session, regardless of which bottom-nav tab is active:

```jsx
<VoiceChat accessToken={idToken} active={view === "talk"} onEnd={...} onTaskChange={refreshTasks} />
{view === "tasks" && <TasksPage tasks={tasks} onRefresh={refreshTasks} onSetStatus={setTaskStatus} />}
```

`active` (not a mount/unmount) controls whether the voice session is "live": when
`false`, `VoiceChat` dims the orb (`.inactive` CSS class — never `display:none`),
mutes the mic, and interrupts any in-flight reply — but the WebSocket, Deepgram, and
Sarvam connections all stay open. Switching back to "Talk" resumes instantly instead
of reconnecting from scratch. `TasksPage` renders as a translucent overlay
(`backdrop-filter: blur`) on top of the dimmed orb rather than replacing it, so the
app never feels like it's navigating between unrelated screens.

---

## 3. Continuous audio streaming & VAD

Unlike push-to-talk, the client streams continuously:
1. **AudioWorklet / VAD** (`@ricky0123/vad-web`, Silero ONNX, model `v5`) processes
   the mic at 16 kHz Float32 and fires `onFrameProcessed` per frame (~32 ms).
2. Each frame is converted to PCM-16 `Int16Array` and sent over the WebSocket
   **while the status is `listening` or `recording`** — the client never cuts you
   off; Deepgram decides end-of-turn server-side via semantic endpointing
   (`vadManager.js` tunes `positiveSpeechThreshold`/`redemptionMs` purely for
   barge-in detection, not turn-taking).
3. A **~500 ms ring buffer** (`RING_BUFFER_FRAMES = 15`) of recent frames is kept so
   that on a barge-in (when we weren't streaming during playback) the words that
   triggered it can be flushed to Deepgram and not lost.
4. **AEC** — the player's output is bound to a hidden `<audio>` element
   (`getOutputStream()` → `srcObject`) so the assistant's own voice played back
   through the speakers doesn't feed into the mic and get transcribed as user
   speech.
5. **Barge-in** — while the AI is `speaking`, `onSpeechStart` firing means: stop the
   player, send `{type: "interrupt"}`, flush the ring buffer to Deepgram (so the
   words that triggered the barge-in aren't lost), and switch to `recording`.
   `onSpeechEnd` deliberately does **nothing** to streaming — Deepgram must "hear"
   the trailing silence itself to fire `utterance_end`.

---

## 4. `VoiceChat.jsx` — the hub

Owns the WebSocket, the VAD instance, the audio player, and renders the orb plus
four bottom-stack surfaces: due-reminder card, `MetadataCard`, `LiveCaption`, and
(on demand) `TranscriptPage`.

### The WebSocket message table
`handleMessage` switches on `data.type`:

| Type | UI effect |
|---|---|
| `processing` | status → `processing` ("thinking") |
| `stt.interim` | live ghost text in `LiveCaption`; status → `recording` |
| `stt.final` | update ghost text (segment finalized, turn may continue) |
| `stt.result` | commit the user's line into `messages` (for the transcript overlay); clear this turn's tool/metadata chips |
| `stt.reconnecting` | transient "Reconnecting…" notice |
| `tool.start` | add a chip to `ToolActivity`; if this is the first tool call this turn and it's `research`/`create_task`/`update_task`, a short spoken filler line is queued (server-driven — see `system_explanation.md` §6) |
| `tool.result` | add a result chip; if it's `create_task`/`update_task` and `ok`, call `onTaskChange()` (tells `App.jsx` to refetch `/tasks`) |
| `metadata` | append to `metadataCards`, rendered by `MetadataCard` — never spoken |
| `llm.token` | append to the live response caption |
| `llm.done` | stash the full response text (committed on `tts.done`) |
| `tts.start` | status → `speaking`; reset the player for a fresh turn |
| `tts.done` | flush remaining buffer; commit the AI line into `messages`; clear the live caption; once playback finishes, resume listening |
| `notice` | transient server-side notice line (auto-clears) |
| `error` | log; commit the partial response (flagged); resume listening |
| `interrupted` | stop the player, clear buffers (don't resume — the user is talking) |
| `history_cleared` | clear `messages`/tool chips/metadata cards |

**Client → server messages:** raw binary PCM frames, plus JSON `speech.start`,
`interrupt`, `clear_history`, `location`, `ping`.

> **One `tts.start` … `tts.done` pair brackets the entire turn**, even across
> multiple sentences — the backend's TTS loop tracks per-sentence completion
> internally, so the frontend needs no per-sentence logic; it just plays the
> continuous stream between start and done.

### `LiveCaption.jsx` vs `TranscriptPage.jsx` — ambient vs full history
These replaced a single always-visible chat transcript that showed the entire
conversation persistently. Two separate components now:

- **`LiveCaption`** renders **exactly one line**: either the user's live interim
  transcript, or the AI's answer captioned as it streams — never both, never
  history. It receives a single `live` prop (`{role, text} | null`) computed
  inline in `VoiceChat`'s render (`interimTranscript` takes priority over
  `responseCaption`).
- **`TranscriptPage`** is the full `messages` array as a translucent overlay
  (same visual pattern as `TasksPage`), opened via a dedicated button in the top
  row and closed with its own header controls (Clear / Close). This is where "what
  did I actually say five turns ago" lives — deliberately out of the ambient view
  so the default screen stays uncluttered during a live conversation.

### `MetadataCard.jsx` — the "show it, don't say it" channel
Renders the structured data the backend deterministically extracts from tool
results (`system_explanation.md` §9's `_extract_presentable_metadata`): research
findings + source links, or a queried task's stored research/due date. Two things
worth knowing:
- **`stripMarkdown()`** (`utils/markdown.js`) is applied to research summary text
  before display — the research model's prompt already forbids markdown, but this
  is a defensive client-side cleanup (`**bold**`, `[text](url)`, stray emphasis
  markers) so a slip on the model side never shows literal asterisks/brackets to
  the user.
- The card's CSS (`.meta-row`) uses `white-space: pre-line` so line breaks in the
  findings text are preserved instead of collapsing into one run-on paragraph.

### `ToolActivity.jsx`
An ephemeral, per-turn list of chips (`"searching the web…"` → `"✓ searching the
web"`) built from `tool.start`/`tool.result` events. Cleared at the start of every
new user turn (`stt.result`).

---

## 5. Reminders — polling, the due-now card, and the dev test button

The web client never receives push notifications (no service worker, no FCM
device-token registration) — it polls instead. `VoiceChat.jsx` extracts the check
into a single `checkReminders` callback:

```js
const checkReminders = useCallback(async () => {
  const d = await rest.getReminders();       // GET /reminders/due — this CONSUMES
  if (!d.count) return;
  setDueReminders((prev) => [...prev, ...d.tasks]);
  if (Notification.permission === "granted") {
    for (const t of d.tasks) new Notification("Reminder", { body: t.title });
  }
}, [rest]);
```

This same callback is called once on mount, then every 60 seconds via
`setInterval`, **and** wired to a dashed, dev-only bell button in the top row
(rendered only when `import.meta.env.DEV` — Vite strips it from production builds
entirely, verified by grepping the built bundle). The point of the dev button isn't
a separate "simulate a notification" mock — it's a manual trigger for the *exact
same* real code path the poller uses, so a due reminder can be tested on demand
(after asking the assistant for a near-future reminder) instead of waiting up to 60
seconds, without inventing a fake reminder client-side that would only test the UI
reaction and not the real due-detection query.

Due reminders render as a dismissible "Due now" card above `MetadataCard` in the
bottom stack; dismissing just clears local state; the backend already marked them
delivered the moment they were fetched (`GET /reminders/due` is a consuming
endpoint — see `system_explanation.md` §12).

---

## 6. The gapless playback engine (`utils/audioPlayer.js`)

`AudioPlayer` streams voice segments over the network without pops or gaps via
three mechanisms:

### A. Decode-free playback
Takes raw binary, casts to `Int16Array`, scales to Float32 — no MP3/Opus decode
overhead. Chunks are contiguous slices of one continuous PCM stream, so there's
deliberately no per-chunk fade in/out — fading every ~67ms chunk would create an
audible 15Hz volume warble.

### B. Pre-buffering (jitter absorption)
Holds playback until at least `MIN_BUFFER_CHUNKS = 4` chunks (~300–400ms) are
queued, absorbing initial network/synthesis jitter before the first sound plays.

### C. Sample-accurate look-ahead scheduling
Each `AudioBufferSourceNode` is scheduled on the `AudioContext` timeline using a
floating `nextStartTime` marker synced to `audioContext.currentTime`, with a 50ms
look-ahead so the audio clock never falls behind — guaranteeing seamless
concatenation between chunks.

### D. AEC bridge
All audio is routed through a `MediaStreamAudioDestinationNode` (not
`audioContext.destination` directly) and played via a hidden `<audio>` element —
Chrome's echo cancellation tracks `<audio>` elements but not raw
`AudioBufferSourceNode`s, so this indirection is required for the mic to properly
ignore the assistant's own voice. A silent oscillator keeps the `MediaStream` alive
continuously so the OS audio hardware never goes to sleep and clips the first
half-second of a new turn's audio.

**Turn boundaries:** `tts.start` calls `stop()` (clear queue, reset timeline) for a
clean turn; `tts.done` calls `flushAndPlay()`, and the caller polls `isActive()`
every 200ms, only resuming listening once all scheduled audio has actually finished
playing (unless a barge-in has already moved the status elsewhere).

---

## 7. The Tasks UI (`TasksPage.jsx` / `TaskRow.jsx`)

A translucent overlay (same visual pattern as `TranscriptPage`) listing tasks as a
grid of cards, grouped into an open section and a collapsed "Done" section.
Sub-tasks are nested **inside their parent's card** rather than shown as a flat
list — `TasksPage` computes `childrenByParent` from `parent_id` client-side (a
parent that's itself been filtered out, e.g. cancelled, falls back to showing its
child standalone).

Each `TaskRow` shows status, title, due date, any stored research links (clickable,
opened in a new tab), a nested sub-task list with its own mini "mark done" buttons,
and status actions (`Done` / `Cancel` when open, `Reopen` when closed) that call
`PATCH /tasks/{id}` via `onSetStatus`. `App.jsx` owns the actual `tasks` array and
refetches after every successful status change — `TasksPage` itself is purely
presentational and never fetches on its own.

---

## 8. Frontend configuration (`client/.env`)

| Var | Purpose |
|---|---|
| `VITE_WS_URL` | Backend voice WebSocket (e.g. `ws://localhost:8000/ws/voice`) |
| `VITE_API_URL` | Backend REST base (e.g. `http://localhost:8000`) |
| `VITE_AUTH0_DOMAIN` | Auth0 tenant domain |
| `VITE_AUTH0_CLIENT_ID` | Auth0 application (SPA) client id |

> Auth0 dashboard must list the dev origin (e.g. `http://localhost:5173`) under
> **Allowed Callback URLs**, **Allowed Logout URLs**, and **Allowed Web Origins**,
> or the redirect login fails with "Callback URL mismatch."
