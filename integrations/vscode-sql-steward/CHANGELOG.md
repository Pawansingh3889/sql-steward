# Changelog

## [0.1.0] - 2026-07-07

First release.

- JSON schema for the semantic layer: validation, autocomplete, and hover docs
  for `semantic.yaml` / `semantic.*.yaml` via redhat.vscode-yaml.
- `sql-steward: Validate semantic layer` command and a quiet on-save check that
  run the real CLI validator for the cross-reference checks a schema cannot do.
- `sqlSteward.executable` setting for a non-PATH CLI (e.g. a virtualenv).
