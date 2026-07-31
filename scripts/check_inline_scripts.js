/**
 * Syntax gate for JavaScript embedded in workflow heredocs
 * (nemarOrg/nemar-cli#1045).
 *
 * Run: node scripts/check_inline_scripts.js
 *
 * These workflows are shared: every one of the ~785 dataset repos dispatches
 * them, so a syntax error in an inlined script is not one broken build, it is
 * every dataset's build, discovered only when a dispatch happens to run. YAML
 * validity says nothing about the JavaScript inside a heredoc -- to YAML it is
 * an opaque string -- so nothing in this repo catches it today.
 *
 * This finds every `cat > <file>.js << 'MARKER' ... MARKER` block across the
 * workflows and runs `node --check` on the extracted body. Discovery is by
 * pattern, not a hardcoded list, so a NEW inlined script is covered the moment
 * it is added rather than whenever someone remembers to register it.
 */

const { execFileSync } = require("node:child_process");
const { readdirSync, readFileSync, writeFileSync, mkdtempSync } = require("node:fs");
const { join } = require("node:path");
const { tmpdir } = require("node:os");

const WORKFLOW_DIR = join(__dirname, "../.github/workflows");
const scratch = mkdtempSync(join(tmpdir(), "inline-js-"));

// cat > <something>.js << 'MARKER' ... newline + indent + MARKER
const BLOCK = /cat\s*>\s*(\S+\.js)\s*<<\s*'([A-Za-z_][A-Za-z0-9_]*)'\n([\s\S]*?)\n\s*\2\s*$/gm;

let checked = 0;
let failed = 0;

for (const file of readdirSync(WORKFLOW_DIR).filter((f) => /\.ya?ml$/.test(f))) {
  const src = readFileSync(join(WORKFLOW_DIR, file), "utf8");
  BLOCK.lastIndex = 0;
  let m = BLOCK.exec(src);
  while (m !== null) {
    const [, target, marker, rawBody] = m;
    // Strip the heredoc's common leading indent. Blank lines carry none, so
    // measure only non-blank ones or every block would come out mis-dedented.
    const lines = rawBody.split("\n");
    const indent = Math.min(
      ...lines.filter((l) => l.trim()).map((l) => l.match(/^ */)[0].length),
    );
    const body = lines.map((l) => l.slice(indent)).join("\n");

    const tmpFile = join(scratch, `${file}.${marker}.js`);
    writeFileSync(tmpFile, body);
    checked++;
    try {
      execFileSync(process.execPath, ["--check", tmpFile], { stdio: "pipe" });
      console.log(`  PASS  ${file} :: ${marker} -> ${target} (${lines.length} lines)`);
    } catch (err) {
      failed++;
      console.error(`  FAIL  ${file} :: ${marker} -> ${target}`);
      console.error(String(err.stderr || err.message).trimEnd());
    }
    m = BLOCK.exec(src);
  }
}

if (checked === 0) {
  // Fail rather than pass vacuously: the pattern silently matching nothing
  // would make this gate look green while checking nothing at all.
  console.error(
    "No inlined JS heredocs found. That is almost certainly a broken pattern in\n" +
      "this script rather than a repo with none -- run-generate-archive.yml has one.",
  );
  process.exit(1);
}

console.log(`\n${checked} inlined script(s) checked, ${failed} failed`);
process.exit(failed ? 1 : 0);
