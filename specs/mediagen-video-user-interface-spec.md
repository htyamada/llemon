# Video Generation User Interface Specification

## Implementation Status

**Implemented.**

### Note on Unified Gallery

The video and image galleries, archives, and category systems are unified. See [mediagen-unified-gallery-spec.md](mediagen-unified-gallery-spec.md) for the complete gallery and archive specification. This document focuses on video-generation-specific features (creator, video-to-video mode inference, reference images).

| File | Role |
|------|------|
| `lib/llemon_djview/videogen.py` | Django view set for video generation, gallery, archive, notes/tags |
| `lib/llemon_djview/templates/llemon_video/` | Templates for video index and Video Creator |
| `lib/llemon_djview/templates/llemon_image/` | Shared Media gallery/archive templates |
| `hty7/llemon/mediagen/__init__.py` + `hty7/llemon/mediagen/videogen/__init__.py` | Shared mediagen config loading plus video-generation config accessors for `media_dir`, `notes_dir`, tags, and notes slot |
| `hty7/llemon/core/notes_db.py` | Package-neutral SQLite notes/tag store |

## Overview

The video-generation Django UI is integrated with the unified media gallery system (see [mediagen-unified-gallery-spec.md](mediagen-unified-gallery-spec.md)). Both image and video galleries share the same storage:

| Area | Directory | Shared |
|------|-----------|--------|
| Gallery | `{LLEMON_GALLERY_DIR}` (configurable) or `{media_dir}/gallery` | Yes |
| Archive | `{LLEMON_ARCHIVE_DIR}` (configurable) or `{media_dir}/archive` | Yes |
| Categories | `gallery/db/gallery.db` | Yes |
| Notes DB | `notes_dir/notes.db` | Yes (with image generation) |

Video Creator uses image files from the gallery or archive as start images,
end images, and reference images for video models. Source dir images must be
copied to the gallery first via the Source Dirs browser.

## Routes

The deployed Django front ends include `llemon_djview.urls` at `/llemon/`.
That shared URL module instantiates `LLemonMediaViewSet('llemon_image',
'llemon', base_nav=..., nav=...)` and exposes Video Creator as the
`video_creator` page inside the combined Media app. `LLemonVideoGenViewSet`
remains available for direct reuse, but host projects should not carry local
view/URL wrappers for the deployed LLemon UI.

| Parameter | Purpose |
|-----------|---------|
| `base_nav` | Left-side navbar items (list of `{'name': str, 'url': str}` dicts) |
| `nav` | Right-side navbar items prepended before the section-specific links on every page; optional |

The section-specific right-side links are Image Creator, Video Creator,
Gallery and Archive. They are appended after any items supplied via
`nav`.

| URL name | Method | Purpose |
|----------|--------|---------|
| `media` | GET | Media app index |
| `video_creator` | GET | Video Creator form |
| `gallery` | GET | Shared Media gallery |
| `archive` | GET | Shared Media archive |
| `video_generate` | POST | Submit a video generation request |
| `video_model_note` | GET/POST JSON | Read/write model notes and tag state |
| `video_models_json` | GET JSON | Refresh provider model list |

Shared file, thumbnail, upload, delete, and archive/move operations use the
canonical Media URL names (`image_file`, `thumbnail`, `archive_image_file`,
`delete_image`, `move_to_archive`, etc.) because those
operations are type-aware.

## Creator

Provider-dependent browser data follows the shared operation-aware presentation
contract in
[`mediagen-creator-presentation-spec.md`](mediagen-creator-presentation-spec.md).
Initial render and provider-refresh JSON expose the same contract. Flat Django
context values remain only for server-rendering the initial controls; action
semantics are unchanged. Model changes use the shared stale-safe target
controller and reuse capabilities from the provider's already-fetched model
rows without repeating discovery.

The creator page follows the image-generation page structure: a left result preview and
form area, a narrow model tag filter column, and a right sidebar for model notes
and gallery/source-reference image selection. It selects provider, model,
duration, prompt, and Venice-specific fields. Model notes and tri-state tags
follow the image-generation interaction model: textarea blur saves notes, tag clicks
save immediately, and the special `block` reverse-tag is used by model
filtering.

Every video-generation POST contains the provider currently selected in the
creator. The server requires a non-empty `provider` field and returns HTTP 400
with `provider is required` when it is absent; it never selects the package
default provider for an action request.

When a video generation request completes, the creator displays the returned
video immediately and renders the same metadata summary that is written to the
video sidecar. The summary includes provider, API, model display name,
duration, selected generation options, saved filename, and prompt when those
values are present. When prompt enhancement rewrote the prompt (see the
LLemon `mediagen-video-spec.md` for the selector mechanism), a
`Generated prompt` row follows the `Prompt` row; the `Prompt` value always
remains the original user prompt. When the creator is opened from a gallery Reload action,
the result preview is populated from query parameters derived from the sidecar
and uses the same metadata rendering path.

Neither `resolution` nor `aspect_ratio` is sent by default. The UI and view
only include them when the user explicitly fills the field. The `audio` field
is handled the same way: the default selector value is omitted, while explicit
Yes/No choices are sent. Some Venice video models reject requests that include
default-valued optional parameters, so omitting them is the safe default.

The image picker (start, end, reference, and generic image buttons) provides
two tabs: **Gallery** and **Source Dirs**. The Gallery tab lists image files from
the gallery. The Source Dirs tab is shown when source directories are
configured (via `input_files` in Grove's `etc/llemon_djview.conf` overlay)
and provides an in-picker
navigable browser backed by the `/media/source-dirs/json/` API. See
[mediagen-unified-gallery-spec.md](mediagen-unified-gallery-spec.md) §Source
Directories for configuration.

For requests that include media, djview must not send its private HTTP(S) media
URLs to the provider. Gallery/archive/source-dir media selected in the
UI is converted server-side to `data:<mime>;base64,...` before calling the
backend. This applies to all providers and to all data-bearing fields: start
images, end images, reference images, scene images, audio inputs, and video
inputs. Public external URLs may pass through unchanged. Converted data URLs are
request payloads only: the video sidecar stores the original selected URL/path
for media inputs so gallery Reload can replay the selection without persisting
any `data:` URL longer than 30 characters.

### Segmind image-to-video integration

LLemon presents the live-validated Segmind model `wan-2.2-i2v-fast` as
`mode: image-to-video` with `allows_start_image: true`. Video Creator uses
LLemon's normalized presentation to expose a start-image control for this
model and forwards it as `image_url`; Segmind does not support an end image,
so no end-image control is offered.

**As implemented in Grove (2026-09-04).** Segmind's start-image control is
the same `#start-image-row` Gallery/Source-Dirs picker used for Venice and
OpenRouter — picker-only, with no manual-URL fallback (`video.html`'s old
`#segmind-start-image-row`/`#segmind_start_image_url` field, its
`showSegmindStartImage` visibility wiring, its `resetForm()` clear, and its
`applyUrlPrefill()` special case are all gone; that provider now shares the
same non-Segmind code path as everyone else at each of those sites). The
submitted value is still routed through the same private-media conversion
helper as every other provider (`_data_reference_for_api`) before reaching
the backend: a stray private Grove media URL selected via the picker
converts to a `data:` URL, a public URL passes through unchanged.

LLemon originally accepted only a well-formed public `https://` URL for this
model and rejected `data:` URLs outright, which is why the control used to
be a manual field rather than a picker. LLemon now also accepts a `data:`
`image_url` for `wan-2.2-i2v-fast` specifically, through a live-validated
`provider_upload` transport (upload to Segmind, submit the returned hosted
reference), gated by `model_presentation()`'s `available_backend_transports`
containing `'provider_upload'` and by an `accept_data_handling_warnings`
parameter on `generate()`. Grove now sets that flag, per request, once the
conditions in "Implementation" below hold — this is the same
`accept_data_handling_warnings` consent Grove collects generically for
image editing (`specs/mediagen-image-user-interface-impl.md`, "Warning
consent, in two layers"), applied here to video's single-scope case.

#### Implementation: the hty7-dependent contract (2026-09-04)

This covers only the parts of the picker-swap work that depend on LLemon's
API surface (see
`~/src/hty7/python3/prj/llemon/specs/mediagen-video-segmind-spec.md`,
"Task 9 Step 3"). The generic picker-swap mechanics themselves
(`video.html`'s `#image-picker`/`pickerTarget` plumbing) are the same ones
Venice and OpenRouter already used; nothing video-specific was needed there.

1. **Availability gate, fail closed.** `segmindStartImagePickerState(modelId)`
   (`video.html`) reads `available_backend_transports` off that model's
   presentation record and checks it contains `'provider_upload'` — never
   hardcodes `wan-2.2-i2v-fast`. It re-evaluates on every model-selection
   change (mirroring the existing re-evaluation already done for
   `allows_start_image`), since availability is per-model and
   `UPLOAD_VERIFIED_I2V_MODELS` currently contains only one entry. The
   picker is enabled only on a positive, declared signal — a required
   transport present *and* listed as available; a model whose presentation
   lacks this metadata entirely (not merely an empty dict) is treated the
   same as one that declares the transport unavailable, never as "must be
   fine." Since there is no manual-URL fallback (point 5), every disabled
   case renders `#start-image-btn` itself `disabled`, with a note element
   explaining why, rather than an enabled control that would 400 on every
   selection.

2. **The warning text is LLemon's, not Grove's.** `#segmind-consent-row`
   (placed immediately after `#start-image-row`, styled like image
   editing's `#edit-consent-row`) displays `transport_warnings['provider_upload']`
   verbatim — never a Grove-paraphrased version. This is the same rule
   LLemon's `~/src/hty7/python3/prj/llemon/specs/mediagen-image-spec.md`'s
   "Data-handling warnings and caller acceptance" states for imagegen's
   consent UI; video's `#segmind-consent-row` and image editing's
   `#edit-consent-row` are independent UI elements (different templates)
   built to the same verbatim-text rule, not shared markup.

3. **When Grove sets `accept_data_handling_warnings=True`.** Exactly when
   all three hold: the request targets Segmind; the resolved `image_url` —
   after `_data_reference_for_api()` — is a `data:` URL (i.e., a
   private/picker-sourced image; a pasted public `https://` URL never needs
   this, see point 5); and the consent checkbox is checked at submit time —
   not merely rendered. `refreshSegmindConsent()` is hooked into the single
   shared place `startImageUrl` actually changes — the end of
   `updateStartImageDisplay()` (a no-op for every non-Segmind provider) —
   plus the provider/model switch and the checkbox's own `change` listener,
   so picking, clearing, and URL-prefilling the start image (`?image_url=`)
   all keep the row in sync. The checkbox resets to unchecked whenever a
   `(currentProvider, CREATOR_PRESENTATION.api, modelSel.value, warning)`
   key changes, so a prior check for a different model or provider can
   never silently carry over. The submit handler defensively refuses to
   submit if the row is visible and unchecked, and always sends
   `accept_data_handling_warnings: !!checkbox.checked` for Segmind.

4. **No new server-side error handling is needed.** `videogen.py`'s
   existing `generate()` call site already forwards any `result['error']`
   dict generically (`err.get('message')`, HTTP 502) — this already covers
   every new error type this transport can produce, with no additional
   code. Worth knowing what they mean, since they can appear verbatim in
   Grove's UI for this one model:
   - `warning_not_accepted` — should be unreachable given point 3's
     gating; if it appears anyway, the checkbox wasn't actually checked at
     submit time (a Grove bug, not a user input error).
   - `invalid_input` — the picked image failed the upload's own
     format/size preflight: only `png`/`jpg`/`webp` are accepted for
     upload (narrower than the Gallery's own accepted formats — a GIF
     will hit this specifically) and it must not exceed 64MB. Grove does
     not filter the Gallery/Source-Dirs picker by format for this control,
     so an otherwise-valid Gallery pick in another format surfaces this
     error rather than being pre-filtered.
   - `ambiguous_upload` / `authentication_error` (from the upload step,
     distinct from the same-named error a submit-phase failure can also
     produce) / `parse_error` (a malformed upload response) — Segmind-side
     upload failures, surfaced the same generic way as any other
     `generate()` failure Grove already shows today.

5. **The manual public-URL field's fate was a Grove UX decision, not an
   hty7 one — decided and implemented (2026-09-04): removed.** Nothing
   about the transport required removing it (a genuinely public,
   already-hosted `https://` URL would have worked unaffected by any of the
   above, since point 3's second condition never triggers for it), but
   Grove made Segmind's start-image control match Venice's own —
   picker-only, no manual-URL fallback — rather than carry two input paths
   with different consent rules. See point 1 for the consequence: since
   there is no fallback, an unavailable-transport model disables the
   picker control itself rather than leaving the user with no working
   input at all.

6. **Backend enforcement, for calibration.** `_generate_impl()` fails
   closed independent of anything Grove does: a `data:` `image_url` for a
   model outside `UPLOAD_VERIFIED_I2V_MODELS` is `invalid_request`
   regardless of any flag; for a verified model without
   `accept_data_handling_warnings=True` it is `warning_not_accepted`,
   before any network access. Grove's own gating (points 1 and 3) exists
   only to make sure a human made an informed, deliberate choice — never
   to substitute for LLemon's own check — mirroring the same
   frontend/backend relationship LLemon's
   `~/src/hty7/python3/prj/llemon/specs/mediagen-image-spec.md` documents
   for imagegen's consent UI.

#### Known caveat: aspect ratio not honored (2026-09-04)

A live-validation run of `wan-2.2-i2v-fast` returned square (624×624)
output instead of the requested 16:9 landscape shape, even though
`_generate_impl()` always sends `aspect_ratio` explicitly on the wire. This
looks like Segmind's model not reliably honoring `aspect_ratio` for
image-to-video mode specifically — provider behavior, not an hty7 or Grove
defect, and not chased down further.

Grove surfaces this as a static, informational, Grove-only notice keyed off
`(provider, model_id)` — not data sourced from hty7 (LLemon's
`model_presentation()` has no "known caveat"/quirk field for any provider).
`videogen.py`'s `_KNOWN_MODEL_CAVEATS` maps `('segmind', 'wan-2.2-i2v-fast')`
to the warning text; `_model_options()` merges it into
`capabilities['known_caveat']` (both the metadata-rich and plain-id-list
branches), omitting the key entirely for every other model so their JSON
payload is byte-for-byte unchanged. `video.html`'s `modelCapabilities(modelId)`
reads it and `#model-caveat-notice` (next to `#model-description`, same
warning color as the consent rows) renders it, refreshed from the same call
site that already updates the description text on every model-change path
(provider switch, model switch, `pageshow` restore, URL prefill) — no
per-path special casing needed. This notice is purely informational and
independent of the consent/picker machinery above: it shows as soon as the
model is selected, before any start image is picked.

For Venice models, the creator consumes the normalized `presentation` record
from LLemon's public video facade. LLemon centralizes catalog-schema precedence
and any identifier-based compatibility fallback; Grove neither imports the
Venice backend nor parses model identifiers. The normalized modes produce these
UI media inputs:

| Normalized mode | UI media inputs |
|-----------------|-----------------|
| Text to Video | none |
| Image to Video | permitted start/end inputs |
| Reference to Video | permitted ordered reference/scene inputs |
| Video to Video | no controls; video input is not supported |
| Transition | permitted start/end inputs |
| Other | inputs explicitly allowed by normalized presentation booleans |

Video-to-video mode hides the start, end, and reference buttons; video input
is not currently supported for those models.

Within Venice reference-to-video, the creator uses the normalized
`reference_image_request_family`; schema-derived values take precedence over
LLemon's centralized compatibility fallback:

| Family | Creator inputs |
|--------|----------------|
| `kling` | `Elements` plus optional `Scene Images` when allowed |
| `grok` | flat ordered `Reference` images |

For Kling O3 R2V, the `Elements` selection is displayed and labeled as
`@Element1`, `@Element2`, ... and the optional scene-image selection is
displayed and labeled as `@Image1`, `@Image2`, ... to match Venice's prompt
conventions. The POST body still uses package-normalized fields:
`reference_image_urls` for elements and `scene_image_urls` for scene images.
The Venice backend translates those to the provider request shape.

For Grok Imagine R2V, the creator shows only the flat ordered reference-image
selector and hides Venice audio controls because that model family does not
support audio generation.

For OpenRouter models, image-input buttons are shown only when the selected
model metadata reports the corresponding supported parameter. If
`supported_frame_images` is present, it decides whether the creator shows the
start-image button, the end-image button, or both. Otherwise `frame_images`
falls back to enabling both exact-frame controls. `input_references` enables
the reference-image button. The creator also provides a separate generic
multi-image selector for OpenRouter's `images` field; when no images are
selected, that field is omitted from the POST body.

The creator also reads OpenRouter's `/videos/models` metadata. If the selected
model reports supported aspect ratios or the passthrough option `aspectRatio`,
the aspect-ratio selector is shown. If the selected model supports audio output
or normalized `generate_audio`, the audio selector is shown. If the model allows
`negativePrompt`, the negative-prompt input is shown. If it allows
`enhancePrompt`, an enhance-prompt checkbox is shown. Any remaining
`allowed_passthrough_parameters` that the UI does not understand are displayed
as “Unknown provider options”.

OpenRouter fields are sent only when the user actually selects or enables them;
no image field is included in the POST body otherwise. Provider validation
remains authoritative for model-specific requirements.

Start and end selections are displayed as compact thumbnails labeled `Start` and
`End`, rather than as filenames. Reference selections are displayed as compact
thumbnails labeled `@Image1`, `@Image2`, etc.; those labels match the submitted
`reference_image_urls` order used by Venice prompt references.

## Gallery and Archive

Gallery and archive are part of the unified media gallery system (see [mediagen-unified-gallery-spec.md](mediagen-unified-gallery-spec.md) for complete specification).

Video-specific details:

- Thumbnails are created lazily with `ffmpeg` (frame at 1s, retrying at 0.1s and 0s on failure, scaled to 320px wide)
- When opening a video in the detail overlay, a video player is displayed instead of an image viewer
- Generation saves video files to gallery and writes a JSON sidecar beside the primary file
- The sidecar records provider, API, `model_id`, `model`, `model_display`, duration, prompt, creation time, options, saved file names, and best-effort request/job identifiers
- When prompt enhancement ran, the sidecar also records `generated_prompt` (the exact prompt sent to the video provider) and the `prompt_enhancement` provenance object from the backend result (enhancement provider, model, configured instruction, request ID, usage); `prompt` remains the original user prompt. Unenhanced generations omit both fields
- Data URLs in metadata are truncated to 30 characters in display; options must not contain any `data:` URL longer than 30 characters
- Generation responses return the sidecar object as `meta` plus a label/value `summary` for immediate creator display
- The detail overlay includes a Generate button (when creator URL is available) that opens the video creator with parameters from the sidecar

## Notes and Tags

Video generation uses the shared `core.notes_db` schema and reads its database
from `notes_dir`. Image and video generation read the same effective
`[*.llemon.mediagen]` settings after the inherited LLemon config and
Grove-local `etc/llemon_djview.conf` overlay are merged, so they use the same
`notes.db` unless an operator overrides one of the paths.

`notes.json` supports two tag lists. Both are merged across all
`description_dirs` / `extra_dirs` files in first-seen order:

| Field | Semantics |
|-------|-----------|
| `tags` | Standard tristate tags; selecting one in the creator filter shows only models that have it set to true |
| `reverse-tags` | Inverted-filter tags; selecting one *excludes* models that have it set to true; these tags are binary (true/false only, no indeterminate state) |

`notes.json` controls tag visibility/editability in the current UI and
reverse-filter semantics; it does not define tag existence in the notes
database. Removing a tag from `notes.json` hides it from the current UI but
does not delete stored tag state.

`reverse-tags` are processed before `tags` within each file; if a name appears
in both, the reverse definition wins. The combined vocabulary is returned by
`get_tags()`; `get_reverse_tags()` returns only the inverted-semantics subset.
The `block` tag included in the default `notes.json` is a reverse-tag.

Freetext note keys are slot-aware:

| Slot | Key |
|------|-----|
| default / absent | `provider:model` |
| `S` | `provider:model:S` |

Tag keys are always `provider:model`. Unknown stored tags are preserved when the
current `notes.json` vocabulary changes.
