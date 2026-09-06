# Imagegen Notes Implementation Notes

Implementation decisions for `hty7/llemon/core/notes_db.py`,
`hty7/llemon/mediagen/__init__.py` / `hty7/llemon/mediagen/imagegen/__init__.py`
(`_load_notes`, `get_tags`, `get_notes_slot`),
and the notes/tags handling in `lib/llemon_djview/imagegen.py`.
The user-facing media UI contract is in
`mediagen-image-user-interface-spec.md`; the notes data is loaded from the
shared `[*.llemon.mediagen]` config. In Grove's Django deployment,
`lib/llemon_djview` inherits the base LLemon config from `~/etc/llemon.conf`
and overlays Grove-local media UI values from `etc/llemon_djview.conf`.

`core.notes_db` is a general utility. It does not own a global database path;
the image-generation backend passes the path from `[*.llemon.mediagen].notes_dir`, and any other
package that uses the helper must pass its own notes directory.

---

## 1. `_load_notes()` as a Separate Function

Notes loading mirrors the existing `_load_quirks()` and `_load_parameters()`
pattern: a dedicated loader reads from the same `description_dirs`, merges
across files, and populates `_config` at `init()` time. Keeping it separate
makes each concern independently testable and avoids entangling the notes
merge rules with quirks logic.

`_load_notes()` only handles `tags`. The slot identifier is no longer read
from `notes.json`; it comes from `notes_selector` in `[*.llemon.mediagen]`
instead.

---

## 3. Schema Migration with `ALTER TABLE ADD COLUMN`

The `tags` column was added to an existing table rather than a new schema
version. `open_notes_db()` always attempts the `ALTER TABLE ADD COLUMN` and
silently catches `sqlite3.OperationalError` when the column already exists.
This is simpler than a schema version table for a single additive change, and
the idempotent probe has negligible cost.

The column stores a JSON object `{"tag": true/false}` rather than the original
array format. `get_note_tags()` accepts dict-format rows directly. Old
array-format rows are interpreted as `true` for each string in the array.

---

## 4. Unknown Tag Preservation in the Django View

When saving tags, the view fetches the currently stored dict, splits it into
`unknown` (keys not in the current vocabulary) and the submitted values for
known keys, then stores `{**unknown, **known_submitted}`. Tags written by a
deployment with a different `notes.json` vocabulary are opaque to this UI but
are not destroyed.

The save response returns the merged stored tag dict. The browser model-tag
cache uses that returned value so tags hidden by the current `notes.json` do
not appear to disappear during the current page session.

`--list-tags` reads tag names from the notes database, not from `notes.json`.
`notes.json` controls UI visibility/editability and reverse-filter semantics
only; tag existence is the set of names stored in the database until explicitly
removed with `--delete-tag`.

---

## 5. Auto-Save on Blur and Tag Click

The original UI had an explicit Save button. The button was removed in favour
of auto-save on textarea blur and on tag click. This eliminates the common
mistake of editing the note or toggling a tag and then navigating away without
saving. The `notesStatus` span still shows `Saving…` / `Saved.` / error
feedback so the user knows saves are happening.

## 6. Tristate Checkboxes

Each tag checkbox has three states: not tested (indeterminate), yes (checked),
no (unchecked). The cycle on click is: not tested → yes → no → not tested.

**Double-click problem.** Checkboxes inside `<label>` elements receive two
click events per user action: one from the direct click and one synthesized by
the label. The fix is `pointer-events: none` on the checkbox, which makes the
checkbox transparent to mouse events. All clicks land on the enclosing label
instead, firing exactly once.

**Implementation.** The click handler is bound to each `<label>` in
`#notes-tags`. It calls `e.preventDefault()` (preventing the label's default
toggle behaviour), reads the current state from `cb._tristate` (null = not
tested, true = yes, false = no), advances it, then calls `_setTristate()` to
apply `checked` and `indeterminate` properties and store the new `_tristate`
value.

`loadNote()` initialises every checkbox to `null` (indeterminate) before
applying the stored dict. `saveNote()` collects only explicitly-set tags
(where `_tristate !== null`) into a `{tag: bool}` dict for the POST body.

## 7. Creator Model Filter Tags

The creator page renders a second set of tag checkboxes next to the model
dropdown. These checkboxes filter the dropdown and do not edit tag state.
Ordinary selected tags are permissive: a model is hidden only when that tag is
explicitly `false`; `true` and absent both remain visible.

`block` is a special filter tag. It is checked on initial render and whenever
the filter controls are reset. While checked, it hides models with `block:
true`; models with `block: false` or no `block` state remain visible.

The Django view's model-tag-state payload is read from the notes database.
There is no `block` quirk path; setting or clearing a model's blocked state is
handled through the `block` tag.

---

## 8. Notes Slot from Config

The notes slot identifier is read from `notes_selector` in
`[{variant}.llemon.mediagen]` at `init()` time and stored in `_config`. The
value `"default"` (and the absent case) both map to the empty string so that
`_notes_key()` produces `provider:model` — identical to the pre-slot behaviour.
Any other value `S` produces `provider:model:S`.

The `.local` config overlay can override `notes_selector` to direct a given
deployment to a different notes set without modifying the base file.

---

## 9. Creator Type Selector

The image creator page supports three operation types via a **Type** dropdown
selector positioned inline next to the Provider dropdown. Selecting a type
controls form visibility and submission routing:

- **Normal**: text-to-image generation (default, existing flow)
- **Upscale**: upscale an uploaded image with scale/enhancement options
- **Edit**: edit an uploaded image with model and aspect-ratio selection

The Type selector is shown only when both `upscale_url` and/or `edit_image_url`
are registered in the URLconf.

### Type Selector Visibility

The Type selector HTML is conditionally rendered only when at least one of
`upscale_url` or `edit_image_url` is in the template context:

```django
{% if upscale_url or edit_image_url %}
<label for="image-type">Type</label>
<select id="image-type">
  <option value="normal">Normal</option>
  {% if upscale_url %}<option value="upscale">Upscale</option>{% endif %}
  {% if edit_image_url %}<option value="edit">Edit</option>{% endif %}
</select>
{% endif %}
```

This allows deployments without upscale/edit endpoints to not show the selector.

### Form Section Visibility by Type

The `switchType(type)` function manages visibility of form sections:

| Section | Normal | Upscale | Edit |
|---------|--------|---------|------|
| Model dropdown | show | hide | hide |
| Aspect ratio/Size/Format/Style row | show | hide | hide |
| Temperature | show | hide | hide |
| Provider options section | show | hide | hide |
| Source image selector | hide | show | show |
| Upscale options panel | hide | show | hide |
| Edit options panel | hide | hide | show |
| Model notes + tags | show | show | show |

Model notes and tag checkboxes remain visible in all types, allowing users to
review and edit per-model metadata across operation types.

The prompt textarea label is relabeled based on type: "Prompt" for normal and
upscale, "Instructions" for edit. The submit button label changes to match:
"Generate", "Upscale", "Edit".

### Source Image Selector

For upscale and edit modes, a source-image selector appears in the right column
at the bottom. It contains:

- A **Choose…** button that opens a modal picker
- A label showing the selected filename
- A **Clear** button (visible only when an image is selected)

Selected state is persisted in data attributes on the `#source-image-section`
DOM element: `data-selectedFname` (filename) and `data-selectedUrl` (full URL).
This decouples state from global variables.

The image picker modal displays a grid of 120×120 mini-thumbnails from the
gallery, rendered using `appendImageThumb()` (matching the video creator
pattern). Selected images show a blue border. Clicking an image or the Close
button dismisses the modal and updates the source-image label. Input files
must come from the gallery; to use a source dir image, copy it to the gallery
first via the Source Dirs browser.

### Form Submission Routing

The form's submit handler checks the Type selector value and routes to the
appropriate endpoint:

- **normal**: POST to `generateUrl` (existing flow)
- **upscale**: POST to `upscaleUrl` with JSON body
  `{provider, fname, scale, enhance, ...}`
- **edit**: POST to `editImageUrl` with JSON body
  `{provider, fname, model, aspect_ratio, prompt}` plus `image_size` when the
  provider's edit path accepts an explicit size (OpenRouter)

Both upscale and edit receive streaming NDJSON responses (same `readGenerateStream()`
pattern as normal generation) and display results using the existing
image-result rendering code.

Every action POST (`generate`, `upscale`, and `edit`) must contain the provider
currently selected in the form. The server returns HTTP 400 with
`provider is required` when it is absent or empty; action endpoints never
select the package default provider.

### Generation Metadata and Prompt Enhancement

`_generate_result()` reads `generated_prompt` and `prompt_enhancement` from
the backend result (present when a mediagen prompt-enhancement selector
matched the request; see the LLemon `mediagen-image-spec.md`). Both fields
are passed to the metadata writers and, when a generated prompt is present,
the client-side canonical EXIF/sidecar writer
(`write_image_generation_exif_with_sidecar_fallback`) is used even for a
backend that did not request server-side embedding, so the original and
generated prompts are both represented in the embedded `generationParams`.
The `prompt` value everywhere remains the original user prompt. The summary
returned to the creator gains a `Generated prompt` line via
`image_generation_summary_lines()`, and the JSON response includes a
`generated_prompt` key when one exists. Unenhanced generations omit all of
these additions. Enhancement failures are terminal: the backend returns a
`prompt_enhance_`-prefixed structured error before any image provider
request, and the view reports it like any other generation error (the Django
path performs no outer retries).

### Upscale Options

When Type is upscale, a panel shows:

- **Scale** dropdown: 2×, 3×, 4×, 1× (enhance only)
- **Enhance** checkbox: when checked, reveals prompt/creativity/replication fields

The enhance sub-options are omitted from the POST body when enhance is unchecked.

### Edit Options

When Type is edit, a panel shows:

- **Model** dropdown: complete edit-model rows from the operation presentation.
  The rows come only from live discovery, through LLemon's public
  `list_edit_models_with_metadata()` facade (see
  `specs/mediagen-image-spec.md` "Edit-model listing" in the LLemon repo).
  Grove preserves every facade row and adds only its display label. There is no
  static fallback. The nullable backend default remains distinct from the
  selected row; presentation selects a valid request, then the backend default,
  then the first eligible row. A provider that does not
  support editing at all renders normally with editing absent — see "Backend
  Context Additions" below. A provider that does support editing but whose
  discovery fails or comes back empty is a provider fault: the view does not
  render a degraded creator for it (see "Error responses" below).
- **Aspect ratio** dropdown: exactly the provider's `edit_aspect_ratios`.
  There is no empty "(source)" choice. Venice includes `auto`
  (displayed as "auto (source)"), which preserves the source ratio;
  OpenRouter has no source-preserving ratio, so a concrete ratio is always
  selected and submitted. `default_edit_aspect_ratio` selects `auto` when
  available, otherwise the provider's default generation ratio.
- **Size** dropdown: shown only when `edit_image_sizes` is non-empty
  (OpenRouter). For Venice single-image edits the control is replaced by a
  note explaining that output size is determined by the source image.

The submitted body always contains `provider`, the explicitly selected edit
`model`, and `aspect_ratio`; `image_size` is included only while the size
control is visible. With no discovered edit model, the client submits nothing.

Server-side, `_do_edit_image()` re-validates everything: the edit model must
be explicitly present and in the discovered list, the aspect ratio must be one of
`edit_aspect_ratios` (with `auto` supplied as the default when the provider
offers it, and a 400 requiring an explicit fixed ratio when it does not),
and `image_size` is rejected with a provider-appropriate message when the
provider does not accept one, or validated against `edit_image_sizes` and
defaulted when it does. `_edit_result()` forwards `image_size` to
`backend.edit()` only when set and records it in the operation sidecar.
`supports_edit(provider, api)` is checked first and returns HTTP 400 when the
provider does not support editing at all; once that passes, edit-model
discovery either succeeds with at least one model or raises — see "Error
responses" below for the resulting HTTP 502, which replaces the previous
empty-list HTTP 400.

### Multi-image editing

The edit flow dispatches through LLemon's provider-neutral multi-image
facade (`edit_images()`/`normalize_edit_inputs()`, `edit_inputs`/
`operations.edit_images` in presentation — see the LLemon repo's
`specs/mediagen-image-spec.md`, "Provider-neutral multi-image editing"),
not the single-image `edit()`/`edit_input`. The two agree for every model
at today's effective maximum of one image
(`specs/mediagen-image-spec.md`, "Agreement with `edit_input` is scoped,
not universal"), so a model reachable only through a named schema is
selectable here exactly like an ordered one.

**Request shape.** `_do_edit_image()` accepts an ordered
`images: [{filename, role?}]` array. A lone top-level `filename` string
is still accepted as input-only compatibility for one release and is
treated as a single-element, role-free array. Parsing is split into two
phases so a bad request is rejected before any file is touched:

- `_parse_request_images()` extracts filenames/roles only, building a
  placeholder canonical image list (`{'source': 'data:'}`, no filesystem
  access) sufficient for `normalize_edit_inputs()` to classify each entry
  and validate shape/count/role against the selected model's
  `edit_inputs` schema. A non-string `role` is rejected explicitly (never
  silently dropped, which would let a garbage role submit as unroled); an
  empty-string `role` is passed through so `normalize_edit_inputs()`'s
  own role checks reject it once, rather than duplicating that rule here.
- `_resolve_request_image_sources()` runs only after
  `normalize_edit_inputs()` has accepted the request, and only then reads
  and base64-encodes each gallery file into a real `data:` URL, in
  order. This ordering means an over-count or malformed request never
  pays to load images it will reject anyway, and a later bad filename
  never produces a misleading "file not found" ahead of a count/role
  error that should fire first.

**Source-kind usability and precedence.** `_source_kind_usability(scope,
source_kind)` classifies one scope (the top-level `edit_inputs` for an
ordered schema, or one role dict for a named schema) against a source
kind, checking `required_backend_transports` *before*
`accepted_source_kinds` — the same precedence
`normalize_edit_inputs()`/`edit_images_availability()` use. Checking
acceptance first would make a disjointly-declared
transport fact (e.g. `accepted_source_kinds=['https_url']` alongside a
required `data_url` upload transport, Segmind's array/named-role shape)
unreachable and silently defeat every upload-backed model.

`_operation_state(row, 'edit_images', source_kind=...)` — and its
client-side mirror `editOptionState()` in `image.html` — uses this
per-scope classifier directly for an ordered schema, but evaluates it
**per role** for a named schema rather than against the top-level
`accepted_source_kinds`/`required_backend_transports`, which are only
the cross-role intersection
(`specs/mediagen-image-spec.md`, "Capability schema") and can be empty
even when every role independently accepts the source kind through its
own transport. Eligibility follows the same required-vs-optional split
`edit_images_availability()` uses: every required role must be usable,
or — with no required roles — at least one optional role must be.

**Warning consent, in two layers.** Grove collects
`accept_data_handling_warnings` consent for multi-image edit (see
"Consent UI" below), but the two layers must not be
confused: `_operation_state()`'s schema-level aggregate below is an
*eligibility* fact (can this model be selected at all, and could it ever
need consent), not the actual per-request gate.

`_operation_state()`'s `(eligible, enabled, reason)` split does the same
job here that it does for `detail == 'summary'`: a warned candidate is
always *eligible* (selectable -- the caller has to be able to pick the
model to see the warning and consent to it) but *enabled* only when the
caller passes `accept_data_handling_warnings=True` itself. For a schema
with required roles, one warned *usable* role means some request through
this model will need consent (AND semantics: any warned usable candidate
lowers `enabled`, absent the flag). For an all-optional schema, a warned
usable role does not lower `enabled` as long as some other usable role
needs no consent (OR semantics: only when *every* usable candidate is
warned). The single-scope (ordered/top-level) case applies the same
warned-transport check without the AND/OR split, since there is only one
scope to satisfy. This aggregate is schema-wide and assignment-blind --
useful for annotating the model dropdown, and for `_do_edit_image()`'s
own preflight (called with `accept_data_handling_warnings=True` forced,
to isolate genuine unusability from the consent question), but it must
never itself be the thing that gates a real submission.

The actual per-request gate is `_resolved_edit_warning_reason()`
(Python) and its JS mirror `resolvedEditWarning()`: given the images
*actually* assigned to roles for this specific request, they look only
at every required role plus whichever optional roles the caller actually
used, and return the verbatim warning text if any of those resolves to a
warned transport. An optional role nobody assigned an image to cannot
make a request need consent, even if that role would be warned in
isolation -- the schema-level aggregate's OR/AND semantics above answer
"could this model ever need it", not "does this one". `_do_edit_image()`
calls this immediately after `normalize_edit_inputs()` succeeds and 400s
before touching the filesystem if it returns a reason and the caller
didn't pass a literal JSON `true` for `accept_data_handling_warnings`
(any other value, including a truthy non-boolean, is rejected rather
than coerced).

**Preflight-pending sources.** `_edit_input_transport_pending()` mirrors
`llemon-image`'s identical check: a canonical image whose source will be
replaced by an uploaded-asset URL before the request is built must not
have its current raw shape validated against the wire schema it will
never actually carry. `_do_edit_image()` only calls the single-image
`preflight_request()` path when there is exactly one image and it is not
transport-pending; a multi-image request, or a pending single one, relies
on `normalize_edit_inputs()` plus the backend's own `validate_request()`
at dispatch instead.

**Selection UI.** `#edit-images-section` (distinct from the single-image
`#source-image-section` Upscale still uses) holds an ordered thumbnail
list built by `renderEditImagesList()`. The shared `#image-picker` modal
supports a toggle-multi-select mode (`window.__editImagesBridge`) for
this section, capped by `currentEditImagesMaxCount()` rather than the
schema's raw `effective_max_count` directly: for a named schema the
usable-role count (per `roleDataUrlUnusableReason()`, which flags a role
that can't accept `data_url` or needs warning consent Grove doesn't
collect) can be lower than the declared count, and the cap follows the
lower of the two so a caller can never add an image with no valid role
left to assign it. Each thumbnail shows a role `<select>` (populated from
`edit_inputs.roles`, unusable options disabled with their reason via
`roleDataUrlUnusableReason()`) for a named schema, or a plain position
tag (`.edit-thumb-position`, `#1`/`#2`/…) for an ordered one. Move-
earlier/move-later controls (`moveEditImage()`, rendered via
`makeThumbIconButton()` as real `<button type="button">` elements with
`aria-label` and native `disabled` at each boundary — not click-only
`<span>`s) reorder the selection in place. Switching to a model whose
shape/count/roles differ from the current selection clears it, using
`_editInputsSignatureFor()` — which folds in each role's
`roleDataUrlUnusableReason()` outcome and `min_count`, not just shape/
count/role names, so a provider or model switch that changes a role's
usability without renaming anything still forces a reset.

**Client-side submit validation.** `handleEditSubmit()` builds the
`images: [{filename, role?}]` request body from `editImageFnames`/
`editImageUrls`/`editImageRoles` and rejects locally, before any
network request, when: no image is selected; fewer images are selected
than `edit_inputs.min_count` (checked for both shapes — a named schema
also gets this indirectly from required-role completeness, but an
ordered schema has no roles to catch it any other way); a role is
assigned to more than one image; or a required role has no image
assigned.

**Consent UI.** A persistent `#edit-consent-row` (message + checkbox) is
the last row inside `#edit-opts`, distinct from the transient
`#model-info-notices` area above — it is shown only while the current
model+assignment actually needs consent, displaying the backend's warning
text verbatim (never Grove-paraphrased). `refreshEditWarningState()`
computes this from current global state (never a stale argument) and is
called from every place that can change it: `_applyEditMetadata()` (initial
load, provider switch), the `edit-model-sel` change handler, the end of
`renderEditImagesList()` (image add/remove/move/clear), the per-thumbnail
role `<select>`, and the checkbox itself. The checkbox resets to unchecked
whenever the reset key — `(provider, api, model id, messages)` — changes;
provider is part of the key because model ids are only unique within a
provider/API, so two providers reusing the same id and warning text must
not let consent carry over between them. `handleEditSubmit()` always sends
`accept_data_handling_warnings: !!checkbox.checked` and defensively refuses
to submit if the row is visible and unchecked (mirroring this function's
other pre-submit re-checks), so Grove cannot offer a way to submit while
unchecked. `_do_edit_image()` parses `accept_data_handling_warnings`
strictly server-side — only a literal JSON `true` counts; any other
non-boolean value 400s explicitly rather than being coerced.

**Testing.** `tests/js/` is a small Node project (`package.json`/
`package-lock.json` committed, `node_modules/` gitignored) whose
`edit_images_dom_test.js` drives a `jsdom`-rendered copy of this page
through named steps — picker multi-select, role assignment, reordering,
both client-side validation rejections, a named schema with per-role
transport/warning facts, an all-optional schema with one warning-free
usable role, and a full submit/response cycle including the async
`fetch`/`.json()` continuation and its `catch` path — since this
repository's Python suite only runs `node --check` on extracted
`<script>` blocks (syntax only) and cannot catch a cross-block or
temporal-dead-zone `ReferenceError`, nor anything that only manifests
once an event handler actually runs. `tests/test_llemon_image_edit_dom.py`
renders a fixture page and runs the script as a subprocess, skipping
(not failing) when Node or `jsdom` isn't installed, so a checkout that
never ran `npm install` in `tests/js/` loses only this file's coverage.
Any future change to the multi-image edit UI should extend this harness
rather than relying on render-only Python assertions or `node --check`
alone — both have repeatedly missed real runtime defects in this exact
code (temporal-dead-zone and cross-`<script>`-block `ReferenceError`s,
a stale-variable-name bug in error-path code, a `windowErrors` check
that only ran once instead of after every step, and an unflushed
microtask queue that let the harness exit before async submit handling
ran at all).

### Backend Context Additions

The `image_creator()` view adds to the template context:

- `upscale_url`: URL path or `None`
- `edit_image_url`: URL path or `None`
- `picker_images`: list of dicts with `fname` and `thumb_url` from gallery
- `model_options`: complete generation metadata rows plus Grove's `display`
  label
- `selected_model`: effective generation row selected for form presentation
- `default_model`: nullable generation backend default
- `notices`: complete JSON-safe model-information notices, rendered from the
  separate `creator-notices-data` script block rather than cached presentation
  state
- `supports_edit`: effective edit availability; false only when the backend
  itself has no editing support. A backend that does support editing but
  whose discovery fails never reaches template rendering — see "Error
  responses" below.
- `edit_models`: live-discovered edit model identifiers; never a static
  fallback; a flat compatibility alias derived from complete presentation rows
- `edit_model_options`: independently copied complete edit rows plus Grove's
  `display` label
- `default_edit_model`: nullable backend default represented as `''` only in
  flat compatibility context; never synthesized from discovery order
- `selected_edit_model`: effective row selected for form presentation
- `edit_aspect_ratios`: the selected complete row's edit ratios
- `default_edit_aspect_ratio`: the row's normalized default when non-null;
  otherwise `auto`, the current provider fallback when offered, or the first
  row choice, in that order
- `edit_image_sizes`: the selected complete row's permitted edit sizes (empty
  when size is automatic)
- `default_edit_image_size`: default selected edit size (`''` when automatic)

These edit keys are produced by the module-level `_edit_metadata()` helper.
The authoritative selected edit controls are derived from that complete row
by `_edit_row_controls()`. A non-null normalized row default wins; otherwise
Grove retains its `auto`, provider fallback, and first-choice display policy.
It no longer maintains its own cache: `list_edit_models_with_metadata()`
caches live edit-model discovery in LLemon itself (300 seconds, keyed by
provider/api/URL), so page renders and edit requests still do not each pay a
catalog fetch, without Grove duplicating that cache. The same keys are
included in the `models_json` response so a provider switch updates the edit
controls without a page reload. Unit/render tests replace `_edit_metadata()`
or `list_edit_models_with_metadata()` with deterministic doubles and never
contact a live provider.

The creator renders notices in `#model-info-notices`, separate from the red
`#error-msg` area. Informational notices use normal status coloring; a warning
adds warning coloring and exactly one `Warning: ` prefix. Later validation or
request errors therefore retain their normal error styling.

Grove's current edit source is always a `data_url`. A complete edit row is
enabled only when normalized edit availability is true, `data_url` is accepted,
and any transport required for `data_url` appears in the available transports.
Other rows remain visible and disabled with a brief normalized explanation.
The edit action endpoint repeats the same check before backend construction.
HTTP(S) caller sources and provider uploads remain unavailable until their
separate storage/transport work is implemented.

Generation model targets carry availability, ratio and size choices/defaults,
quality choices/default, temperature/system visibility, and extra-field
descriptors. Model-scoped providers use the complete normalized presentation
as the source of those controls. API-wide providers retain their established
provider-wide ratio/size choices and defaults when a per-model presentation
omits them; an empty OpenRouter model-record enum therefore cannot replace the
API-wide dropdown with an open input. Every target key is present, including
empty lists and null defaults, so a new model can clear prior controls.

The browser retains provider fallback defaults separately from active selected-
generation defaults. Accepted model targets replace only the active layer.
Generation Reset uses the active layer; edit-model fallback calculation uses
the current provider layer. Provider switching replaces both layers and the
current provider metadata object before edit model changes are handled.

Ratio and size render as selects when the effective choice list is nonempty and
as optional free-text controls when it is empty. The open size placeholder
documents `WIDTHxHEIGHT`; open controls start and reset blank so omission lets
the model apply its own default. In select mode, a normalized default that is
null or absent from the choices resolves to the first choice; Reset restores
that effective value rather than the unresolved normalized default.
Submission, query restoration, and Reset use
one visible-control helper. Query restoration waits for the accepted model
target before setting values, so cache hits and asynchronous fetches behave
identically. Controls hidden by an accepted target are cleared and only visible
controls are serialized.

An HTTP-200 target with an empty `controls` mapping is the model-detail failure
sentinel, not a partial overlay. The browser clears prior model controls,
disables generation with the generic model-control error, and rejects target
application. The shared refresh controller caches only after successful
application, so the sentinel is not retained and a later selection retries the
lookup. Partial nonempty targets overlay by key presence; meaningful empty
lists and null defaults replace provider fallbacks.

Model changes rebuild dynamic extra-field DOM when descriptors change. Values
are retained only for fields whose complete normalized descriptor is unchanged;
removed, hidden, or changed fields are cleared. This preserves provider-wide
Venice values across ordinary model changes without leaking a value into a
different model-specific control schema.

The extracted, DOM-independent browser state helpers have executable Node
coverage for target keying and stale-response behavior, presence-based control
overlay, unresolved-target retry semantics, enumerated/open choice resolution,
extra-field descriptor equality, concrete select/open DOM application, visible-
control serialization, and target-ready sequencing used by Reset and query
restoration. Creator render and view tests additionally verify that the template
uses those shared helpers. These are offline acceptance tests. Live provider
execution is outside this UI specification's implementation acceptance and is
performed independently when authorized.

Generation availability is applied to presentation and ordinary browser
submission only. The action resolves its explicit/registered model without a
presentation lookup, strips ratio/size strings, treats absent/empty/whitespace
values as model-default requests, membership-checks only nonempty choice lists,
validates model-scoped dynamic fields, and calls provider-neutral
`preflight_request()` before backend construction. Parameter errors return HTTP
400. Model-information or unexpected preflight failures are logged and return
HTTP 502 with `could not validate request against model information`. When both
the request and normalized default omit ratio or size, preflight sees it as not
supplied while backend dispatch and metadata receive canonical `''`, never
`None`. This adds no generation `model_presentation()` authorization lookup.

Edit action validation selects the submitted complete row, repeats availability
and data-URL transport compatibility, derives controls through
`_edit_row_controls()`, and preflights the actual data URL before backend
construction. A supplied edit ratio is checked against nonempty row choices.
For omission, a non-null normalized ratio default wins, then `auto` when
offered; fixed-choice rows otherwise require an explicit ratio. A displayed
provider/first-choice fallback does not become an action omission default.
Size retains the separate existing automatic/source-determined policy.

### Error responses

`image_creator()`, `models_json()`, and `_do_edit_image()`/
`_edit_archive_image()` each call `_edit_metadata(provider, api)` (directly,
or through `_creator_data()`) inside its own `try`/`except`. When that call
raises — a discovery failure, or an empty listing from a provider that does
declare `supports_edit`, both of which `list_edit_models_with_metadata()`
raises rather than returning a degraded result — the view logs the exception
and returns `JsonResponse({'error': f'could not list edit models: {e}'},
status=502)` instead of rendering or returning partial creator data. This
mirrors the existing `could not list image generation models`/`could not
list models` 502 pattern already used for the generate-listing failure path
in the same two view methods. There is no HTTP 400 for an empty edit-model
listing any more: that case is now indistinguishable, at the HTTP layer,
from any other discovery failure.

The `_gallery_picker_items()` private method scans the gallery directory for
image files (extensions: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) and returns
a sorted list of dicts with `fname` and `thumb_url`. Does not call
`_ensure_thumbnail()` on page load for performance; uses pre-existing thumbnails.

### Gallery Cleanup

The upscale and edit buttons that previously appeared on the gallery's image
detail panel have been removed, eliminating redundancy. All upscale/edit
operations now flow exclusively through the creator's Type selector.
