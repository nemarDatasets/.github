/**
 * Pure helpers for the archive streamer in
 * `.github/workflows/run-generate-archive.yml` (nemarOrg/nemar-cli#1045).
 *
 * WHY THIS FILE EXISTS. The streamer runs as inline JavaScript inside a YAML
 * heredoc, which means it cannot be imported and therefore cannot be tested --
 * and it ships to all ~785 dataset repos at once, so a defect reaches every
 * dataset simultaneously and is caught only by a reviewer's eye or a live
 * production dispatch. Every bug in nemarOrg/nemar-cli#1038 came through that
 * hole. These are the decision functions from that script, extracted verbatim
 * so they can carry real tests.
 *
 * THIS IS A VERIFIED DUPLICATE, NOT A SINGLE SOURCE. The workflow still inlines
 * its own copy, because the runner checks out the *dataset* repo, not this one,
 * so the heredoc has no path to `require()` this file from. Making it a true
 * import would need a second checkout inside the archive job plus a new
 * walkDir exclusion, i.e. runtime risk to every dataset build in exchange for
 * tidiness. Instead `test-generate-archive.yml` asserts the two copies are
 * byte-identical, so they cannot drift: edit one and CI fails until you edit
 * the other.
 *
 * Keep these functions pure and dependency-free. That is what lets the same
 * text run unmodified in both places.
 */

// git-annex keys embed the content's byte size: MD5E-s11878096--<hash>.nii.gz,
// SHA256E-s6488064--<hash>.fif. Returns null when no size can be read,
// which disables the size check for that file (fail-open: an unparsed
// key must not make a present object look hollow).
//
// Two distinct reasons a key yields null, deliberately handled the
// same way. URL keys carry no size at all. WORM keys DO carry one, but
// as WORM-s<size>-m<mtime>--<name> — the -m<mtime> sits between the
// size and the --, so this regex does not match them either. Reading
// WORM sizes would need its own pattern; not worth it, since NEMAR
// content is MD5E/SHA256E and WORM appears only in hand-built repos.
function declaredKeySize(key) {
  var m = /-s(\d+)--/.exec(key);
  if (!m) return null;
  var n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

// The object at this key is not the content the key describes, so it
// must not go into the archive under that name.
//
// git-annex keys are content-addressed: the size is part of the
// identity, not metadata about it. Any mismatch means the bytes on
// hand are some other content, whatever the cause, so the check is
// deliberately a plain inequality rather than a 0-byte test.
//
// The motivating case is undersized: a failed import leaves a 0-byte
// object at a valid key (the nemarOrg/nemar-cli#967 empty-PUT
// signature — curl wrote an empty file on a 403 and it was uploaded
// anyway), GetObject SUCCEEDS, and archiver writes a 0-byte entry, so
// the zip ships `ready` carrying silently empty files. on003574's
// published archive has four 0-byte T1w scans exactly this way.
//
// But oversize happens too, and the first live run proved it: on004624
// carries a stray upstream temp file whose object is 6,520,832 bytes
// against a key declaring 6,488,064. Same verdict, same handling — the
// content is not what was asked for and no rebuild will change that.
function hollowObjectError(key, expected, actual) {
  var e = new Error("object is " + actual + " bytes; key declares " + expected);
  e.nemarAbsent = true;
  return e;
}

// S3 answered authoritatively that the object is not there, or we
// proved the object present is not the content its key declares.
// Anything else means we never got a usable answer.
function isAbsentError(err) {
  if (!err) return false;
  var status = (err.$metadata && err.$metadata.httpStatusCode) || 0;
  return err.nemarAbsent === true || err.name === "NoSuchKey" || err.Code === "NoSuchKey" || status === 404;
}

module.exports = { declaredKeySize, hollowObjectError, isAbsentError };
