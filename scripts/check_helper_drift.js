/**
 * Drift guard for scripts/stream-archive-helpers.js
 * (nemarOrg/nemar-cli#1045).
 *
 * Run: node scripts/check_helper_drift.js
 *
 * The archive streamer inlines its own copy of these functions because the
 * runner checks out the *dataset* repo, not this one, so its heredoc has no
 * path to require() the checked-in file. That duplication is deliberate, but a
 * duplicate nobody checks is exactly how a tested file drifts away from the
 * code that actually runs -- and only the inlined copy ships to the ~785
 * dataset repos.
 *
 * So: assert the two copies are byte-identical after stripping the heredoc's
 * fixed 10-space indent. Edit one and this fails until you edit the other.
 */

const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

const ROOT = join(__dirname, "..");
const WORKFLOW = join(ROOT, ".github/workflows/run-generate-archive.yml");
const HELPERS = join(ROOT, "scripts/stream-archive-helpers.js");
const FUNCTIONS = ["declaredKeySize", "hollowObjectError", "isAbsentError"];

/** Pull `function <name>(...) { ... }` plus its leading comment block. */
function extractFromWorkflow(src, name) {
  const re = new RegExp(
    `\\n((?:          //[^\\n]*\\n)*          function ${name}\\(.*?\\n          \\})\\n`,
    "s",
  );
  const m = re.exec(src);
  assert.ok(m, `could not find ${name}() in run-generate-archive.yml`);
  // The heredoc body sits at a fixed 10-space indent.
  return m[1]
    .split("\n")
    .map((line) => (line.startsWith("          ") ? line.slice(10) : line))
    .join("\n")
    .trimEnd();
}

function extractFromHelpers(src, name) {
  const re = new RegExp(`\\n((?://[^\\n]*\\n)*function ${name}\\(.*?\\n\\})\\n`, "s");
  const m = re.exec(src);
  assert.ok(m, `could not find ${name}() in stream-archive-helpers.js`);
  return m[1].trimEnd();
}

const workflowSrc = readFileSync(WORKFLOW, "utf8");
const helpersSrc = readFileSync(HELPERS, "utf8");

let failed = 0;
for (const name of FUNCTIONS) {
  const inWorkflow = extractFromWorkflow(workflowSrc, name);
  const inHelpers = extractFromHelpers(helpersSrc, name);
  if (inWorkflow === inHelpers) {
    console.log(`  PASS  ${name} is identical in both copies`);
  } else {
    failed++;
    console.error(`  FAIL  ${name} has DRIFTED between the workflow and the helpers file`);
    console.error("\n--- .github/workflows/run-generate-archive.yml ---");
    console.error(inWorkflow);
    console.error("\n--- scripts/stream-archive-helpers.js ---");
    console.error(inHelpers);
    console.error("");
  }
}

if (failed) {
  console.error(
    `\n${failed} function(s) drifted. The workflow's inlined copy is what actually runs on\n` +
      "every dataset build; the helpers file is what the tests cover. They must match.\n" +
      "Copy the change across (mind the 10-space heredoc indent) and re-run.",
  );
  process.exit(1);
}
console.log(`\nall ${FUNCTIONS.length} helpers in sync`);
