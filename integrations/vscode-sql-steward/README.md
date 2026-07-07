# sql-steward for VS Code

Editing support for [sql-steward](https://github.com/Pawansingh3889/sql-steward)
semantic layer files: the YAML contract that decides what an AI agent may read
from your database.

## What it does

- **Validation as you type.** Structural errors — an unknown key, a metric with
  no aggregate, a check kind that does not exist — are underlined immediately,
  from a JSON schema that tracks the real layer format.
- **Autocomplete.** The dialects, aggregate functions, check kinds, and common
  PII categories complete from the schema, so you are picking from the allowed
  set rather than remembering it.
- **Hover docs.** Every field explains itself: what `block_pii` refuses, why a
  join not listed here is refused rather than invented, what a `search` block
  enables.
- **Full validation on demand.** The schema cannot check that a join actually
  connects two real entities or that a metric's dimensions resolve. The
  **sql-steward: Validate semantic layer** command (and a quiet check on save)
  runs the real `sql-steward validate`, which does.

## Requirements

- The [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml)
  (installed automatically as a dependency) drives validation, completion, and
  hover.
- For the validate command, the `sql-steward` CLI on your PATH, or its path set
  in `sqlSteward.executable` (e.g. a virtualenv: `.venv/Scripts/sql-steward`).
  `pip install sql-steward`.

## File matching

Applies to `semantic.yaml` / `semantic.yml` and any `semantic.<name>.yaml`, so
several layers can live side by side. Point the server at one with
`SQL_STEWARD_LAYER`.

## Why a semantic layer is worth editing carefully

It is the whole contract. The agent reads what is described here and nothing
else: the tables, the columns, the joins it may traverse, the aggregates it may
run. Anything not written down is refused rather than guessed. This extension
exists to make that file quick to write and hard to get subtly wrong.

## License

MIT
