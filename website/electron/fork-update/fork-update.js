"use strict";

/**
 * Self-update helper for the FORK's unsigned macOS desktop build.
 *
 * The normal feed lane (Squirrel.Mac) needs a stable signing identity; an
 * ad-hoc signature's cdhash changes every build, so the fork cannot use it.
 * Instead the build bakes an EXTERNALLY-MANAGED marker (packaging/fork/
 * mac-managed-marker.json) whose checkCommand / updateCommand invoke THIS
 * script through the app's own binary in Node mode:
 *
 *   ELECTRON_RUN_AS_NODE=1 "<bundle>/Contents/MacOS/KiroCrew" \
 *     "<bundle>/Contents/Resources/fork-update/fork-update.js" <check|apply>
 *
 * Managed-lane contract (runtime/update/managed-lane.js):
 *   check — exit 0 and print the update's version on stdout, or exit nonzero
 *           when nothing is available (or the check could not run).
 *   apply — exit 0 once the install is replaced. The lane stops the gateway
 *           first, then relaunches the marker's relaunchPath on success.
 *
 * The script lives OUTSIDE app.asar (extraResources) so it can be exec'd as a
 * plain file. It uses Node builtins only: the managed lane's constructed
 * environment carries no HOME and a narrowed system PATH, so nothing it
 * touches may rely on either — every spawned binary is named absolutely.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const GITHUB_REPO = "Fei2-Labs/kirocrew";
const RELEASES_URL =
  `https://api.github.com/repos/${GITHUB_REPO}/releases?per_page=30`;
const LATEST_RELEASE_URL =
  `https://api.github.com/repos/${GITHUB_REPO}/releases/latest`;
const compareUrl = (local, remote) =>
  `https://api.github.com/repos/${GITHUB_REPO}/compare/${local}...${remote}`;

// fork-release.yml tags every release `v<version>+fork.g<short sha>`; the
// captured sha is the remote revision the compare endpoint and the staged
// bundle's FORK_REVISION are checked against.
const TAG_RE = /^v[^+]+\+fork\.g([0-9a-f]+)$/;
const ASSET_ZIP = "KiroCrew-fork-mac-arm64.zip";
const ASSET_SHA512 = `${ASSET_ZIP}.sha512`;
const APP_NAME = "KiroCrew.app";
const USER_AGENT = "kirocrew-fork-desktop-updater";
const FETCH_TIMEOUT_MS = 15 * 1000;
const REVISION_RE = /^FORK_REVISION\s*=\s*"([0-9a-f]*)"/m;

// Exit codes. The managed lane reads ANY nonzero exit as "up to date"/"apply
// failed", so the split below is for the operator reading the log, not the lane.
const EXIT_UPDATED = 0;
const EXIT_NO_UPDATE = 1; // checked: nothing to take; applied: nothing was ahead
const EXIT_ERROR = 2;     // could not reach a verdict

const err = (msg) => process.stderr.write(`fork-update: ${msg}\n`);

/**
 * The .app bundle this script belongs to. The script sits at
 * <bundle>/Contents/Resources/fork-update/fork-update.js, so three levels up
 * is the bundle — derived from __dirname, never argv or env, so a copied or
 * symlinked invocation cannot point the swap at a different app.
 */
function bundleRoot(scriptDir = __dirname) {
  return path.resolve(scriptDir, "..", "..", "..");
}

/**
 * This build's fork revision, from the same stamp readForkRevision
 * (auto-update.js) reads: the backend tree's kiro_crew/_build_info.py.
 * Returns "" when no stamp is found — a non-fork build, which this lane
 * must never offer to replace.
 */
function readForkRevision(bundle, io = fs) {
  try {
    const backendDir = path.join(bundle, "Contents", "Resources", "backend-dist");
    for (const entry of io.readdirSync(backendDir)) {
      const libDir = path.join(backendDir, entry, "lib");
      let pythons = [];
      try { pythons = io.readdirSync(libDir); } catch { continue; }
      for (const py of pythons) {
        const stamp = path.join(libDir, py, "site-packages", "kiro_crew", "_build_info.py");
        let text = "";
        try { text = io.readFileSync(stamp, "utf8"); } catch { continue; }
        const m = REVISION_RE.exec(text);
        if (m && m[1]) return m[1];
      }
    }
  } catch { /* no backend tree */ }
  return "";
}

/** fetch() with a timeout and a fixed User-Agent (GitHub requires one). */
async function fetchJson(fetchImpl, url) {
  const res = await fetchImpl(url, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/vnd.github+json" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    redirect: "follow",
  });
  if (res.status === 404) return { notFound: true };
  if (!res.ok) throw new Error(`GET ${url} -> HTTP ${res.status}`);
  return { json: await res.json() };
}

/**
 * The newest release this lane may take. Every fork tag shares the same
 * version number, so GitHub's ordering is by tag name, not recency — the
 * winner is the qualifying release with the greatest `published_at`. A
 * release lacking `published_at` sorts last; ties keep the first seen.
 */
function pickTargetRelease(releases) {
  if (!Array.isArray(releases)) return null;
  let best = null;
  let bestPublished = -1;
  for (const release of releases) {
    if (release.draft || release.prerelease) continue;
    const m = TAG_RE.exec(release.tag_name || "");
    if (!m) continue;
    const assets = Array.isArray(release.assets) ? release.assets : [];
    const byName = new Map(assets.map((a) => [a.name, a.browser_download_url]));
    const zipUrl = byName.get(ASSET_ZIP);
    const shaUrl = byName.get(ASSET_SHA512);
    if (!zipUrl || !shaUrl) continue;
    const published = Date.parse(release.published_at || "");
    const publishedMs = Number.isNaN(published) ? -1 : published;
    if (!best || publishedMs > bestPublished) {
      best = { tag: release.tag_name, sha: m[1], zipUrl, shaUrl };
      bestPublished = publishedMs;
    }
  }
  return best;
}

/**
 * Is `remote` strictly ahead of `local`? Short shas prefix-match when they
 * name the same commit (the stamp uses --short=8, the tag plain --short), so
 * a prefix match either way means identical without a network call.
 * Otherwise the compare endpoint answers; anything that cannot prove
 * "ahead" — behind, diverged, 404, any fetch failure — is NO update.
 */
async function isAhead(local, remote, { fetchImpl }) {
  if (!local || !remote) return false;
  if (remote.startsWith(local) || local.startsWith(remote)) return false;
  try {
    const { notFound, json } = await fetchJson(fetchImpl, compareUrl(local, remote));
    if (notFound) return false;
    return json.status === "ahead";
  } catch {
    return false; // a compare that cannot prove "ahead" is no update
  }
}

/**
 * The update this install may take, or null: resolve the target release and
 * prove it ahead of the local revision. Throws on verdict-unreachable errors;
 * a missing/unsuitable release or a non-ahead compare returns null.
 */
async function findUpdate(local, { fetchImpl }) {
  const { json: releases } = await fetchJson(fetchImpl, RELEASES_URL);
  // The list is ordered by tag name, so once releases outgrow one page the
  // newest can fall off it; /releases/latest is ordered by creation time.
  let latest = null;
  try {
    latest = (await fetchJson(fetchImpl, LATEST_RELEASE_URL)).json || null;
  } catch {
    latest = null;
  }
  const candidates = Array.isArray(releases) ? releases : [];
  const target = pickTargetRelease(latest ? [latest, ...candidates] : releases);
  if (!target) return null;
  if (!(await isAhead(local, target.sha, { fetchImpl }))) return null;
  return target;
}

/** Run an absolute-path binary, resolving {code, stderr}. Never rejects. */
function runBinary(spawnImpl, cmd, args) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawnImpl(cmd, args);
    } catch (e) {
      resolve({ code: 1, stderr: String((e && e.message) || e) });
      return;
    }
    let stderr = "";
    let settled = false;
    const done = (code) => { if (!settled) { settled = true; resolve({ code, stderr }); } };
    if (child.stderr) child.stderr.on("data", (d) => { stderr += d.toString(); });
    if (child.stdout) child.stdout.resume ? child.stdout.resume() : child.stdout.on("data", () => {});
    child.on("error", (e) => { stderr += String(e); done(1); });
    child.on("close", (code) => done(typeof code === "number" ? code : 1));
  });
}

async function downloadTo(fetchImpl, url, dest, io) {
  const res = await fetchImpl(url, {
    headers: { "User-Agent": USER_AGENT },
    signal: AbortSignal.timeout(30 * 60 * 1000),
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`GET ${url} -> HTTP ${res.status}`);
  io.writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
}

/**
 * `check`: print the tag of an available update and exit 0; exit 1 when none
 * is available; exit 2 when the check itself could not run.
 */
async function check(bundle, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch;
  const io = deps.fs || fs;
  const local = readForkRevision(bundle, io);
  if (!local) {
    err("no FORK_REVISION in the bundled backend — not a fork build");
    return EXIT_NO_UPDATE;
  }
  try {
    const target = await findUpdate(local, { fetchImpl });
    if (!target) return EXIT_NO_UPDATE;
    process.stdout.write(`${target.tag}\n`);
    return EXIT_UPDATED;
  } catch (e) {
    err(`check failed: ${(e && e.message) || e}`);
    return EXIT_ERROR;
  }
}

/**
 * `apply`: re-resolve the target (it must still be ahead), download and
 * verify the zip, extract it with ditto, verify the staged app's revision,
 * de-quarantine it, then swap it into place beside the old bundle. Same-volume
 * sibling staging so the final rename is atomic. Exit 0 on success; the old
 * bundle and staging are removed best-effort.
 */
async function apply(bundle, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch;
  const spawnImpl = deps.spawn || spawn;
  const io = deps.fs || fs;

  const local = readForkRevision(bundle, io);
  if (!local) {
    err("no FORK_REVISION in the bundled backend — not a fork build");
    return EXIT_NO_UPDATE;
  }

  const parent = path.dirname(bundle);
  const stamp = `${Date.now()}-${process.pid}`;
  const staging = path.join(parent, `.${path.basename(bundle)}.fork-update-${stamp}`);
  const oldPath = path.join(parent, `.${path.basename(bundle)}.old-${stamp}`);
  const cleanupStaging = () => {
    try { io.rmSync(staging, { recursive: true, force: true }); } catch { /* best effort */ }
  };

  try {
    const target = await findUpdate(local, { fetchImpl });
    if (!target) {
      err("no newer fork release — nothing to apply");
      return EXIT_NO_UPDATE;
    }

    io.mkdirSync(staging, { mode: 0o700 });
    try {
      const zipPath = path.join(staging, ASSET_ZIP);
      const shaPath = path.join(staging, ASSET_SHA512);
      await downloadTo(fetchImpl, target.zipUrl, zipPath, io);
      await downloadTo(fetchImpl, target.shaUrl, shaPath, io);

      const expected = /^([0-9a-f]{128})/.exec(io.readFileSync(shaPath, "utf8").trim());
      if (!expected) throw new Error(`${ASSET_SHA512} carries no sha512 digest`);
      const actual = crypto.createHash("sha512").update(io.readFileSync(zipPath)).digest("hex");
      if (actual !== expected[1]) {
        throw new Error(`sha512 mismatch: expected ${expected[1]}, got ${actual}`);
      }

      // ditto, not unzip: it preserves the macOS metadata a .app needs.
      const ditto = await runBinary(spawnImpl, "/usr/bin/ditto", ["-x", "-k", zipPath, staging]);
      if (ditto.code !== 0) throw new Error(`ditto extract failed (exit ${ditto.code}): ${ditto.stderr.trim()}`);

      const stagedApp = path.join(staging, APP_NAME);
      if (!io.existsSync(stagedApp)) throw new Error(`extracted archive has no ${APP_NAME}`);

      // The staged app must be the build the tag names: its stamp is --short=8
      // of the release commit, the tag's sha plain --short — same commit
      // prefix-matches either direction.
      const stagedRev = readForkRevision(stagedApp, io);
      if (!stagedRev || !(stagedRev.startsWith(target.sha) || target.sha.startsWith(stagedRev))) {
        throw new Error(`staged app revision ${stagedRev || "<none>"} does not match release ${target.sha}`);
      }

      const xattr = await runBinary(spawnImpl, "/usr/bin/xattr", ["-dr", "com.apple.quarantine", stagedApp]);
      if (xattr.code !== 0 && !/No such xattr/i.test(xattr.stderr)) {
        throw new Error(`xattr de-quarantine failed (exit ${xattr.code}): ${xattr.stderr.trim()}`);
      }

      // The swap: both renames are same-volume (staging/old are siblings of
      // the bundle). If putting the staged app in place fails, restore the
      // original so the install is never left without an app.
      io.renameSync(bundle, oldPath);
      try {
        io.renameSync(stagedApp, bundle);
      } catch (e) {
        try { io.renameSync(oldPath, bundle); } catch (e2) {
          err(`CRITICAL: could not restore ${bundle}: ${(e2 && e2.message) || e2}`);
        }
        throw e;
      }

      try { io.rmSync(oldPath, { recursive: true, force: true }); } catch { /* best effort */ }
      return EXIT_UPDATED;
    } finally {
      cleanupStaging();
    }
  } catch (e) {
    err(`apply failed: ${(e && e.message) || e}`);
    return EXIT_ERROR;
  }
}

async function main(argv, deps = {}) {
  const mode = argv[2];
  const bundle = bundleRoot(__dirname);
  if (mode === "check") return check(bundle, deps);
  if (mode === "apply") return apply(bundle, deps);
  err(`usage: fork-update.js <check|apply> (got: ${argv.slice(2).join(" ") || "<nothing>"})`);
  return EXIT_ERROR;
}

if (require.main === module) {
  main(process.argv).then((code) => { process.exitCode = code; });
}

module.exports = {
  bundleRoot,
  readForkRevision,
  pickTargetRelease,
  isAhead,
  findUpdate,
  check,
  apply,
  main,
  TAG_RE,
  ASSET_ZIP,
  ASSET_SHA512,
  APP_NAME,
  RELEASES_URL,
  LATEST_RELEASE_URL,
  compareUrl,
};
