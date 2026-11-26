/**
 * Tests for scripts/stream-archive-helpers.js -- the decision functions of the
 * archive streamer in .github/workflows/run-generate-archive.yml.
 *
 * Run: node scripts/test_stream_archive_helpers.js
 *
 * No test framework and no mocks, matching the plain-`assert` style of the
 * sibling Python tests in this directory. Every case below is a real shape
 * observed in production, cited where it came from; these are regressions, not
 * hypotheticals.
 */

const assert = require("node:assert");
const {
  declaredKeySize,
  hollowObjectError,
  isAbsentError,
} = require("./stream-archive-helpers.js");

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  PASS  ${name}`);
  } catch (err) {
    console.error(`  FAIL  ${name}\n        ${err.message}`);
    process.exitCode = 1;
  }
}

console.log("declaredKeySize");

test("reads the byte size out of an MD5E key", () => {
  // Real key from on003574's availability report.
  assert.strictEqual(
    declaredKeySize("MD5E-s11878096--9c29d1a4c1f77718224924b2e8f74a6f.nii.gz"),
    11878096,
  );
});

test("reads the byte size out of a SHA256E key", () => {
  assert.strictEqual(declaredKeySize("SHA256E-s6488064--abc123.fif"), 6488064);
});

test("handles a key with no extension", () => {
  assert.strictEqual(declaredKeySize("MD5E-s42--deadbeef"), 42);
});

test("returns null for a URL key, which carries no size", () => {
  assert.strictEqual(declaredKeySize("URL--https://example.org/sub-01_eeg.edf"), null);
});

test("returns null for a WORM key -- it HAS a size, but not in this shape", () => {
  // WORM is WORM-s<size>-m<mtime>--<name>: the -m<mtime> sits between the size
  // and the --, so the regex deliberately does not match. Fail-open (null
  // disables the size check) rather than mis-parsing. A reviewer reading the
  // diff assumed WORM carried no size at all; it does. Guard the real reason.
  assert.strictEqual(declaredKeySize("WORM-s1024-m1234567890--sub-01_eeg.edf"), null);
});

test("returns null when there is no size segment at all", () => {
  assert.strictEqual(declaredKeySize("SHA256--abc123.edf"), null);
});

test("does not mistake a size-like run elsewhere in the name", () => {
  // The delimiter is -s<digits>--; a bare -s in the filename must not match.
  assert.strictEqual(declaredKeySize("MD5E--hash-s12345-file.edf"), null);
});

console.log("isAbsentError");

test("NoSuchKey by name is absent", () => {
  const e = new Error("NoSuchKey");
  e.name = "NoSuchKey";
  assert.strictEqual(isAbsentError(e), true);
});

test("NoSuchKey by Code is absent", () => {
  assert.strictEqual(isAbsentError({ Code: "NoSuchKey" }), true);
});

test("a 404 status is absent", () => {
  assert.strictEqual(isAbsentError({ $metadata: { httpStatusCode: 404 } }), true);
});

test("a size mismatch is absent via nemarAbsent", () => {
  assert.strictEqual(isAbsentError(hollowObjectError("MD5E-s10--h", 10, 0)), true);
});

test("403 is NOT absent -- it must fail the build and delete the zip", () => {
  // The whole absent/unreadable split rests on this. A 403 means the content
  // may well be there (expired creds, a policy race), so a rebuild can succeed;
  // classifying it absent would publish a truncated archive as if the data were
  // gone upstream.
  assert.strictEqual(isAbsentError({ $metadata: { httpStatusCode: 403 } }), false);
});

test("5xx and throttling are NOT absent", () => {
  assert.strictEqual(isAbsentError({ $metadata: { httpStatusCode: 500 } }), false);
  assert.strictEqual(isAbsentError({ $metadata: { httpStatusCode: 503 } }), false);
  assert.strictEqual(isAbsentError({ name: "SlowDown" }), false);
});

test("an error with no metadata is NOT absent", () => {
  // Filesystem read errors, archiver append errors, OOM: everything unknown
  // must fail loudly rather than be silently counted as a missing file.
  assert.strictEqual(isAbsentError(new Error("EACCES")), false);
});

test("null/undefined is not absent", () => {
  assert.strictEqual(isAbsentError(null), false);
  assert.strictEqual(isAbsentError(undefined), false);
});

console.log("hollowObjectError");

test("flags a 0-byte object at a valid key (the #967 empty-PUT signature)", () => {
  // on003574 shipped 11 of these inside a `ready` archive.
  const e = hollowObjectError("MD5E-s11878096--h.nii.gz", 11878096, 0);
  assert.strictEqual(e.nemarAbsent, true);
  assert.match(e.message, /0 bytes/);
  assert.match(e.message, /11878096/);
});

test("also flags an OVERSIZED object, not just a hollow one", () => {
  // on004624's stray upstream temp file: 6,520,832 bytes against a key
  // declaring 6,488,064. git-annex keys are content-addressed, so size is part
  // of the identity -- any mismatch means these are not the requested bytes.
  const e = hollowObjectError("MD5E-s6488064--h", 6488064, 6520832);
  assert.strictEqual(e.nemarAbsent, true);
  assert.strictEqual(isAbsentError(e), true);
});

test("is a real Error so it flows through the existing catch", () => {
  assert.ok(hollowObjectError("k", 1, 2) instanceof Error);
});

console.log(`\n${passed} passed${process.exitCode ? ", WITH FAILURES" : ""}`);
