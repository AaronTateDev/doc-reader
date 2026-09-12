# Doc Reader web app: content-first workspace redesign

Date: 2026-09-12. Scope: the local web page served by `doc_reader/webapp.py`
(`INDEX_HTML`: vanilla HTML, CSS custom properties, and plain DOM JavaScript).
No framework, no build step, no new dependencies. Storage and API contracts are
unchanged.

## Current problems

- The text editor is confined to a 300px column; the Library gets the wide column.
- Import controls, voice settings, dictation diagnostics, and the text field all
  compete at the same visual weight in one stack.
- The Signal map sits above the Library as a large card even though it is
  consulted occasionally.
- Library cards repeat the beginning of the text in the title and again in the
  preview ("Dictation: adding any kind of..." then "adding any kind of...").
- Selection, live playback state, and the primary action are not clearly
  distinguished from navigation and status text.
- Status is written as long slash-separated sentences in three places.

## Chosen layout

```
+-----------------------------------------------------------------------------+
| Doc Reader   [Import document] [Import audio] [x] Timestamps  [New text]   |
|                                              (dot) Reading ...   [Details] |
+-------------+------------------------------------------------+-------------+
| Library     | Workspace                                       | Inspector   |
| All/Read/   |  Kind - Title - meta            [Copy] [Edit]   | (toggle)    |
| Dict/Clawdad|  +------------------------------------------+   | Dictation   |
| [search]    |  |  editor / reading text (18px, <=72ch)    |   | Signal map  |
| row         |  |                                          |   | Engines     |
| row (sel)   |  +------------------------------------------+   |             |
| row (live)  |  [Read text] [Pause] [Stop]  state  Voice Speed |             |
+-------------+------------------------------------------------+-------------+
```

- Library 280px on desktop; the workspace takes the rest. Rows are buttons
  that select an item and show its text in the workspace without starting
  playback. The playing/paused item carries a rail; the selected row a fill.
- Workspace footer keeps Read/Play, Pause/Resume, Stop, current state, voice,
  and speed visible while long text scrolls inside the surface.
- Inspector (right, toggled by "Details", remembered per browser) holds
  dictation controls and readiness, the Signal map, and engine status. Below
  1180px it overlays as a drawer; below 1024px the Library also becomes a
  drawer with a header toggle, Escape to close, and focus return.
- Draft text persists in `localStorage` and survives selecting items; "New
  text" returns to it. Saving edits keeps the existing `POST /api/items/{id}/text`
  semantics (no silent overwrite of another item).

## Token direction

Dark (default in dark mode): bg `#15171B`, structural `#1C1F25`, editor
`#242830`, text `#F3F4F6`, secondary `#B0B8C4`, accent `#62D0BC`, primary
button text `#10251F`, live/recording `#F0B45C`, error `#F28B82`. Light mode
mirrors it with `#F1F3F5` / `#E8EBEE` / `#FFFFFF`, accent `#137D6B`.
Contrast was measured (see verification): body and secondary text >= 4.5:1,
accent and strong borders >= 3:1 on their surfaces. System font stack;
interface text 14px, reading text 18px/1.6 constrained to 72ch.

## Feature mapping

| Capability | Where it lives now |
| --- | --- |
| Paste/type text and read it | Workspace editor + footer "Read text" (`POST /api/text`) |
| Import document / audio, timestamps | Header actions (`/api/upload`, `/api/audio/transcribe`) |
| Play / Pause / Resume / Stop | Footer (`/api/items/{id}/play`, `/api/pause`, `/api/stop`) |
| Voice, read speed | Footer (`/api/settings`) |
| Library filters, search, counts | Library sidebar (client-side, same rules) |
| Copy / edit saved text | Workspace header (`GET/POST /api/items/{id}/text`) |
| Dictation toggle, helper start/stop/reset, microphone | Inspector > Dictation (`/api/settings`, `/api/native/*`) |
| Recording / transcribing / helper state | Footer dictation chip + Inspector readiness rows |
| Signal map, Analyze, terms | Inspector > Signal map (`/api/library/analysis/run`) |
| Local/remote speech availability | Inspector > Engines |

Semantics preserved: "STT words" = words in dictation items, "TTS words" =
words in reading items, "Analyzed" = items with an analysis entry, "Open" =
readings not yet completed (`style_map.completion.open`). "Clawdad" = items
handed off from the external Clawdad app (`source == "clawdad"`). Read speed is
shown as WPM and the engine's own multiplier (`rate / 180`).

## Risks

- PDF/DOCX items have no text endpoint; the workspace shows metadata and a
  notice instead of content. Playback still reads the document.
- The page re-renders from `/api/state` every 1.5 s; the workspace editor and
  selection must not be rebuilt on each poll (only the Library list is).
- Hotkey capture and paste cannot be exercised from the browser; verified via
  the transcription API and helper heartbeat only.

## Verification (2026-09-12, Windows, isolated preview on :8790 with a copy of the library)

Executed through the running page (Chromium automation) unless noted:

- Paste text, read it, pause, resume, stop; live row tag, footer state, and
  button labels follow `running`/`paused`/`active_id`.
- Select items; PDF/DOCX fall back to a notice (source inspection: no text
  endpoint for those types).
- Draft text survives selecting items and "New text", and survives reload.
- Filters (All / Readings / Dictations / Clawdad) with counts, search, empty
  states; selection, view, and inspector state persist across reload.
- Edit, save, revert, on a dictation; copy verified by source only (the
  Clipboard API refuses writes from an unfocused automation window).
- Import a `.txt` through the real file input (plays, line breaks kept);
  import a silent `.wav` through the real audio input ("Audio produced no
  text"); multipart audio transcription with timestamps via the API.
- Analyze: queued and completed with local rules; the upstream worker first
  waits on the unreachable remote model, unchanged behavior.
- Speech services unreachable (second preview on :8791): chip and engine rows
  show offline, playback reports a readable failure instead of a traceback.
- Viewports 1440x900, 1366x768, 1024x768, 390x844 (dark and light at 1440);
  150% zoom without horizontal overflow; library and Details drawers at phone
  width with Escape and focus return; visible focus ring; arrow keys move
  through the list.
- Contrast measured: text >= 4.5:1, accent and strong borders >= 3:1, both
  themes.
- `python -m unittest discover -s tests`: 21 tests pass.

Not verified here: real keyboard Enter/Space activation of list rows (the
automation harness cannot deliver activating key events, plain buttons
included); the hold-to-dictate hotkey and paste-into-app path (native helper,
outside the browser); document import of PDF/DOCX with real files.

## Addendum (2026-09-12): Kokoro voice picker

The footer Voice control was a native `<select>` that only chose the speech
engine; every Kokoro engine read with `af_heart`. It is now a trigger button
("Emma", or "Emma (Remote Kokoro)" when the engine is not the local one) that
opens a popover anchored above it (bottom-left origin, 150 ms ease-out,
`role="dialog"`):

- "American voices" and "British voices" listboxes: the 28 English Kokoro
  voices from `doc_reader/kokoro_voices.py`, female then male, each with a
  checkmark when current and a round play button that fetches
  `GET /api/voices/preview?voice=<id>` (one short sentence, synthesized once
  per voice per process, served as `audio/wav`).
- "Original engine options": a disclosure pinned to the bottom of the
  scrolling panel that expands the previous engine list (Local fallback,
  Local Kokoro, Remote Kokoro, strict, Chatterbox, OpenAI API). The current
  engine's label shows on the collapsed row.
- Choosing a voice posts `{kokoro_voice, speech_backend}`; if the current
  engine cannot use Kokoro voices, the engine switches to Local Kokoro.
  Choosing an engine posts `{speech_backend}` only, as before.
- Keyboard: focus lands on the current option; ArrowUp/ArrowDown move within
  visible options; Escape closes and returns focus to the trigger; a pointer
  press outside closes. Below 720 px the panel becomes a bottom sheet.

Backend: `kokoro_voice` is a validated web setting (catalog ids only);
`play()` appends `--http-tts-voice` for Kokoro-capable engines; prepared
Library audio uses the same voice; `tts` state carries `kokoro_voice`,
`kokoro_voice_label`, `kokoro_backends`, and `voices`.

Verified on the :8790 preview: picker opens/closes, sample playback for Bella,
choosing Emma persisted and the reader subprocess launched with
`--http-tts-voice bf_emma`, engine section expand/collapse, Escape and outside
click, arrow keys, 390x844 sheet, light theme, unknown voice ids rejected
(400). `python -m unittest discover -s tests`: 26 tests pass.
