/**
 * sql-steward VS Code extension.
 *
 * The JSON schema (contributed in package.json) gives editing intelligence for
 * free through redhat.vscode-yaml: structural validation, autocomplete, hover
 * docs. This activation adds the one thing a schema cannot express — the
 * semantic validation the CLI does (join reachability, references that resolve,
 * metrics whose dimensions actually exist). It shells out to `sql-steward
 * validate` and surfaces the result.
 */
const vscode = require('vscode');
const { execFile } = require('child_process');
const path = require('path');

const OUTPUT = vscode.window.createOutputChannel('sql-steward');

function isSemanticLayer(document) {
  if (document.languageId !== 'yaml') return false;
  return /(^|[\\/])semantic(\.[^\\/]*)?\.ya?ml$/i.test(document.fileName);
}

function runValidate(document) {
  const config = vscode.workspace.getConfiguration('sqlSteward');
  const exe = config.get('executable', 'sql-steward');
  const filePath = document.fileName;
  const cwd = path.dirname(filePath);

  OUTPUT.appendLine(`validating ${filePath}`);
  execFile(exe, ['validate', filePath], { cwd }, (error, stdout, stderr) => {
    const out = (stdout || '').trim();
    const err = (stderr || '').trim();
    if (out) OUTPUT.appendLine(out);
    if (err) OUTPUT.appendLine(err);

    if (error && error.code === 'ENOENT') {
      vscode.window
        .showErrorMessage(
          `sql-steward not found (tried "${exe}"). Set sqlSteward.executable to its path.`,
          'Open settings',
        )
        .then(pick => {
          if (pick === 'Open settings') {
            vscode.commands.executeCommand(
              'workbench.action.openSettings',
              'sqlSteward.executable',
            );
          }
        });
      return;
    }

    if (error) {
      OUTPUT.show(true);
      vscode.window.showErrorMessage(
        `sql-steward: semantic layer is invalid — ${err || out || 'see the sql-steward output'}`,
      );
      return;
    }

    vscode.window.showInformationMessage('sql-steward: semantic layer is valid.');
  });
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('sqlSteward.validate', () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage('sql-steward: open a semantic layer file first.');
        return;
      }
      runValidate(editor.document);
    }),
  );

  // Validate on save, quietly: only a failure interrupts.
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(document => {
      if (isSemanticLayer(document)) runValidate(document);
    }),
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
