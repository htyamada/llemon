# Repository Guidelines

## Project Structure & Module Organization

This repository contains the `llime` Django project, shared local libraries,
and the imhandler command project. Application code for the web project lives
under `llime/`: project settings are in `llime/config/`, Django apps are in
directories such as `base/` and `to_do_list/`. Project specs are consolidated
in root `specs/`, including To Do, mediaview, imhandler, and LLemon frontend/media specs.
Reusable shared code lives under `lib/`; currently `lib/mediaview/` is a
standalone Django app, `lib/llemon_djview/` contains the shared LLemon Django
views and templates, and `lib/imhandler/` contains the local imhandler
library and Django view app. The imhandler CLI lives at `bin/imh`, and its
config lives under `imhandler/`.
The active consumers of these shared Django apps are `llime` and
`../qat/knip`.

Templates are stored per app in `templates/`, and static assets are under each
app’s `static/` directory. Tests, where present, use each app’s `tests.py`.

## Build, Test, and Development Commands

- `cd llime && ./manage.py check` validates Django settings, app loading, URL
  configuration, and template discovery.
- `cd llime && ./start-server` starts the local Django development server.
- `python3 -m py_compile lib/mediaview/*.py lib/llemon_djview/*.py lib/imhandler/*.py lib/imhandler/cli/*.py lib/imhandler/djview/*.py bin/imh` checks the
  shared Python packages for syntax errors.
- `python3 -m unittest discover -s tests -t .` runs the repository-level unit
  tests. The explicit `-t .` (top-level directory = repo root) is required,
  not cosmetic: without it, `-s`/`-t` default to the same directory and
  `unittest` never imports `tests/__init__.py` as a package at all — test
  modules load as bare top-level names instead of `tests.test_*`, and
  `tests/__init__.py`'s own top-level code (the check that fails fast on a
  stale installed `hty7` — see `tests/_hty7_install_check.py`) silently
  never runs.
- `cd llime && ./manage.py test` runs Django tests for apps that define them.

## Coding Style & Naming Conventions

Use Python 3 with 4-space indentation. Keep Django views and helpers small,
module-local, and named descriptively; private helpers should use a leading
underscore, for example `_resolve()` or `_associated_sidecars()`. Prefer
`pathlib.Path` for filesystem work. Templates should use plain Django template
syntax and plain browser JavaScript unless an existing app already uses another
pattern.

## Testing Guidelines

Run `./manage.py check` after settings, URL, template, or app-loading changes.
Run focused Django tests with `./manage.py test app_name` when changing app
behavior. For shared libraries, also run the `py_compile` command above and manually
verify browse, thumbnail, metadata, move, delete, and sidecar handling when those
flows are touched.

## Commit & Pull Request Guidelines

Git history uses short, direct summaries such as `Moved llime from hty7 to
grove`. Keep commits focused and use concise past-tense or imperative messages.
Pull requests should state the user-visible change, list verification commands
run, note config or deployment impacts, and include screenshots for UI changes.

## Security & Configuration Tips

Do not commit secrets or machine-local config. Runtime settings come from files
under `~/etc/`, while generated logs and caches live under `~/var/`. Shared apps
may be imported by host projects through `sys.path`, so keep public module names
stable and document path changes in both host settings and `lib/*` docs.

### Operational Trust Model

Grove is a single-person system: the person configuring, developing, and
operating an application is also the person creating the archives and content
it accesses. There are no separate or untrusted users. Treat validation,
bounded parsing, path handling, and similar safeguards as reliability features
against mistakes and malformed personal data, not as security boundaries or a
multi-user threat model.

Some archived content (e.g. downloaded EPUBs/PDFs/CBZs) is produced by third
parties rather than the operator, so it should be treated as not fully
trusted -- but the bar is "possibly malformed or subtly broken," not
"possibly weaponized." Keep bounded parsing, size/pixel caps, and path
containment for that reason, proportionate and simple; do not add further
hardening whose only justification is resisting a deliberate, actively
hostile attacker.
