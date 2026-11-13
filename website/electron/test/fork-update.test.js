const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { EventEmitter } = require("events");

const {
  pickTargetRelease,
  isAhead,
  check,
  apply,
  readForkRevision,
  RELEASES_URL,
  LATEST_RELEASE_URL,
  compareUrl,
  ASSET_ZIP,
  ASSET_SHA512,
} = require("../fork-update/fork-update");

// ── helpers ─────────────────────────────────────────────────────────────────

const LOCAL = "aaaabbbb"; // --short=8 stamp
const REMOTE = "ccccddd"; // tag sha (plain --short)

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "fork-update-test-"));
}

function writeStamp(bundle, revision) {
  const dir = path.join(
    bundle, "Contents", "Resources", "backend-dist",
    "kirocrew-backend-arm64", "lib", "python3.12", "site-packages", "kiro_crew",
  );
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "_build_info.py"), `FORK_REVISION = "${revision}"\n`);
}

function makeBundle(parent, revision) {
  const bundle = path.join(parent, "KiroCrew.app");
  writeStamp(bundle, revision);
  return bundle;
}

function release(over = {}) {
  return {
    tag_name: "v0.14.0+fork.g" + REMOTE,
    draft: false,
    prerelease: false,
    assets: [
      { name: ASSET_ZIP, browser_download_url: "https://dl.example/" + ASSET_ZIP },
      { name: ASSET_SHA512, browser_download_url: "https://dl.example/" + ASSET_SHA512 },
    ],
    ...over,
  };
}

/** A fetch stand-in: routes maps url -> body (object -> json, Buffer -> bytes) or an Error. */
function fakeFetch(routes) {
  const calls = [];
  const fn = async (url) => {
    calls.push(url);
    const hit = routes[url];
    if (hit === undefined) throw new Error(`unexpected fetch: ${url}`);
    if (hit instanceof Error) throw hit;
    return {
      ok: true,
      status: 200,
      json: async () => hit,
      arrayBuffer: async () => Buffer.from(hit),
      text: async () => String(hit),
    };
  };
  fn.calls = calls;
  return fn;
}

function notFoundFetch(routes) {
  const inner = fakeFetch(routes);
  return async (url, opts) => {
    try { return await inner(url, opts); }
    catch (e) {
      if (/compare/.test(url)) return { ok: false, status: 404, json: async () => ({}) };
      throw e;
    }
  };
}

/** A spawn stand-in: handler(cmd, args) -> {code, stderr}. Returns a fake child. */
function fakeSpawn(handler) {
  const calls = [];
  const fn = (cmd, args) => {
    calls.push([cmd, args]);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const result = handler(cmd, args) || { code: 0, stderr: "" };
    process.nextTick(() => {
      if (result.stderr) child.stderr.emit("data", result.stderr);
      child.emit("close", result.code);
    });
    return child;
  };
  fn.calls = calls;
  return fn;
}

// ── release selection ───────────────────────────────────────────────────────

test("pickTargetRelease: skips drafts, prereleases, bad tags and missing assets", () => {
  const good = release();
  const releases = [
    release({ tag_name: "v9.9.9+fork.gfffffff", draft: true }),
    release({ tag_name: "v9.9.9+fork.gfffffff", prerelease: true }),
    release({ tag_name: "v9.9.9" }),                    // no +fork.g suffix
    release({ tag_name: "v9.9.9+fork.gZZZZ" }),         // non-hex sha
    release({ tag_name: "v9.9.9+fork.gfffffff", assets: [
      { name: ASSET_ZIP, browser_download_url: "https://dl.example/zip" },
    ] }),                                             // sha512 asset missing
    release({ tag_name: "v9.9.9+fork.gfffffff", assets: [
      { name: ASSET_SHA512, browser_download_url: "https://dl.example/sha" },
    ] }),                                             // zip asset missing
    good,
  ];
  const picked = pickTargetRelease(releases);
  assert.strictEqual(picked.tag, good.tag_name);
  assert.strictEqual(picked.sha, REMOTE);
  assert.strictEqual(pickTargetRelease(releases.slice(0, -1)), null);
  assert.strictEqual(pickTargetRelease([]), null);
  assert.strictEqual(pickTargetRelease("not-a-list"), null);
});

test("pickTargetRelease: the real tag-name ordering picks the newest published_at", () => {
  // GitHub returned our same-version tags in descending tag-name order; the
  // first match was the installed build, not the newest release.
  const releases = [
    release({ tag_name: "v0.13.0+fork.ged8034aa8c", published_at: "2026-11-01T10:00:00Z" }),
    release({ tag_name: "v0.13.0+fork.g85603fae24", published_at: "2026-11-02T10:00:00Z",
      assets: [] }),                                    // no mac assets on this one
    release({ tag_name: "v0.13.0+fork.g6130eb9a81", published_at: "2026-11-03T10:00:00Z" }),
  ];
  const picked = pickTargetRelease(releases);
  assert.strictEqual(picked.tag, "v0.13.0+fork.g6130eb9a81");
  assert.strictEqual(picked.sha, "6130eb9a81");
});

test("pickTargetRelease: newest published_at wins regardless of array position", () => {
  const oldest = release({ tag_name: "v0.13.0+fork.gaaaaaaa1", published_at: "2026-11-01T00:00:00Z" });
  const newest = release({ tag_name: "v0.13.0+fork.gbbbbbbb2", published_at: "2026-11-05T00:00:00Z" });
  const middle = release({ tag_name: "v0.13.0+fork.gccccccc3", published_at: "2026-11-03T00:00:00Z" });
  assert.strictEqual(pickTargetRelease([newest, oldest, middle]).tag, newest.tag_name);
  assert.strictEqual(pickTargetRelease([oldest, middle, newest]).tag, newest.tag_name);
  assert.strictEqual(pickTargetRelease([middle, newest, oldest]).tag, newest.tag_name);
});

test("pickTargetRelease: a release lacking published_at sorts last", () => {
  const dated = release({ tag_name: "v0.13.0+fork.gaaaaaaa1", published_at: "2026-11-01T00:00:00Z" });
  const undated = release({ tag_name: "v0.13.0+fork.gbbbbbbb2" });
  assert.strictEqual(pickTargetRelease([undated, dated]).tag, dated.tag_name);
  assert.strictEqual(pickTargetRelease([undated]).tag, undated.tag_name);
});

// ── verdict mapping ─────────────────────────────────────────────────────────

test("isAhead: remote ahead -> update", async () => {
  const fetchImpl = fakeFetch({ [compareUrl(LOCAL, REMOTE)]: { status: "ahead" } });
  assert.strictEqual(await isAhead(LOCAL, REMOTE, { fetchImpl }), true);
});

test("isAhead: prefix-identical shas never reach the compare endpoint", async () => {
  const fetchImpl = fakeFetch({});
  // tag sha is a prefix of the --short=8 stamp (same commit)
  assert.strictEqual(await isAhead("ccccddd0", "ccccddd", { fetchImpl }), false);
  assert.strictEqual(await isAhead("ccccddd", "ccccddd0", { fetchImpl }), false);
  assert.strictEqual(await isAhead(LOCAL, LOCAL, { fetchImpl }), false);
  assert.deepStrictEqual(fetchImpl.calls, []);
});

test("isAhead: behind / diverged / identical statuses -> no update", async () => {
  for (const status of ["behind", "diverged", "identical"]) {
    const fetchImpl = fakeFetch({ [compareUrl(LOCAL, REMOTE)]: { status } });
    assert.strictEqual(await isAhead(LOCAL, REMOTE, { fetchImpl }), false, status);
  }
});

test("isAhead: 404 and network errors -> no update (fail closed)", async () => {
  const fetch404 = notFoundFetch({});
  assert.strictEqual(await isAhead(LOCAL, REMOTE, { fetchImpl: fetch404 }), false);
  const fetchDown = fakeFetch({ [compareUrl(LOCAL, REMOTE)]: new Error("ENOTFOUND") });
  assert.strictEqual(await isAhead(LOCAL, REMOTE, { fetchImpl: fetchDown }), false);
});

test("check: prints the release tag and exits 0 when ahead", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const target = release();
  const fetchImpl = fakeFetch({
    [RELEASES_URL]: [target],
    [compareUrl(LOCAL, REMOTE)]: { status: "ahead" },
  });
  let printed = "";
  const orig = process.stdout.write;
  process.stdout.write = (s) => { printed += s; return true; };
  try {
    assert.strictEqual(await check(bundle, { fetch: fetchImpl }), 0);
  } finally {
    process.stdout.write = orig;
  }
  assert.strictEqual(printed, target.tag_name + "\n");
});

test("check: a newest release only /releases/latest returns is still found", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const offPage = release({ published_at: "2026-11-09T00:00:00Z" });
  const onPage = release({ tag_name: `v0.13.0+fork.g${LOCAL}`, published_at: "2026-11-01T00:00:00Z" });
  const fetchImpl = fakeFetch({
    [RELEASES_URL]: [onPage],
    [LATEST_RELEASE_URL]: offPage,
    [compareUrl(LOCAL, REMOTE)]: { status: "ahead" },
  });
  const orig = process.stdout.write;
  let printed = "";
  process.stdout.write = (s) => { printed += s; return true; };
  try {
    assert.strictEqual(await check(bundle, { fetch: fetchImpl }), 0);
  } finally {
    process.stdout.write = orig;
  }
  assert.strictEqual(printed, offPage.tag_name + "\n");
});

test("check: exits 1 when nothing is ahead, 2 on check failure", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const upToDate = fakeFetch({
    [RELEASES_URL]: [release()],
    [compareUrl(LOCAL, REMOTE)]: { status: "identical" },
  });
  assert.strictEqual(await check(bundle, { fetch: upToDate }), 1);
  const noRelease = fakeFetch({ [RELEASES_URL]: [] });
  assert.strictEqual(await check(bundle, { fetch: noRelease }), 1);
  const down = fakeFetch({ [RELEASES_URL]: new Error("ECONNREFUSED") });
  assert.strictEqual(await check(bundle, { fetch: down }), 2);
  // no stamp at all -> not a fork build -> 1
  const unstamp = path.join(parent, "Bare.app");
  fs.mkdirSync(unstamp, { recursive: true });
  assert.strictEqual(await check(unstamp, { fetch: fakeFetch({}) }), 1);
});

// ── apply ───────────────────────────────────────────────────────────────────

function applyFetch(zipBytes, { status = "ahead", stagedRev = "ccccddd0" } = {}) {
  const sha = crypto.createHash("sha512").update(zipBytes).digest("hex");
  const routes = {
    [RELEASES_URL]: [release()],
    [compareUrl(LOCAL, REMOTE)]: { status },
    ["https://dl.example/" + ASSET_ZIP]: zipBytes,
    ["https://dl.example/" + ASSET_SHA512]: sha + "  " + ASSET_ZIP + "\n",
  };
  return fakeFetch(routes);
}

/** ditto stand-in: copies a prepared extracted tree into the dest dir. */
function dittoCopying(preparedDir) {
  return fakeSpawn((cmd, args) => {
    if (cmd === "/usr/bin/ditto") {
      const dest = args[3];
      fs.cpSync(preparedDir, dest, { recursive: true });
      return { code: 0, stderr: "" };
    }
    return { code: 0, stderr: "" };
  });
}

function leftoverDirs(parent) {
  return fs.readdirSync(parent).filter((d) => /fork-update-|\.old-/.test(d));
}

test("apply: successful swap leaves the new bundle in place, no old/staging dirs", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const zipBytes = Buffer.from("fake-zip-payload");
  const prepared = tmpdir();
  writeStamp(path.join(prepared, "KiroCrew.app"), "ccccddd0");
  const spawnImpl = dittoCopying(prepared);
  const fetchImpl = applyFetch(zipBytes);

  assert.strictEqual(await apply(bundle, { fetch: fetchImpl, spawn: spawnImpl }), 0);
  assert.strictEqual(readForkRevision(bundle), "ccccddd0");
  assert.deepStrictEqual(leftoverDirs(parent), []);
  // ditto ran on the zip; xattr ran on the staged app
  const cmds = spawnImpl.calls.map((c) => c[0]);
  assert.deepStrictEqual(cmds, ["/usr/bin/ditto", "/usr/bin/xattr"]);
});

test("apply: sha512 mismatch fails and removes staging, bundle untouched", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const fetchImpl = fakeFetch({
    [RELEASES_URL]: [release()],
    [compareUrl(LOCAL, REMOTE)]: { status: "ahead" },
    ["https://dl.example/" + ASSET_ZIP]: Buffer.from("zip"),
    ["https://dl.example/" + ASSET_SHA512]: "0".repeat(128) + "  " + ASSET_ZIP,
  });
  const spawnImpl = fakeSpawn(() => ({ code: 0, stderr: "" }));
  assert.strictEqual(await apply(bundle, { fetch: fetchImpl, spawn: spawnImpl }), 2);
  assert.strictEqual(readForkRevision(bundle), LOCAL);
  assert.deepStrictEqual(leftoverDirs(parent), []);
  assert.deepStrictEqual(spawnImpl.calls, []); // never reached ditto
});

test("apply: staged FORK_REVISION mismatch fails, original bundle untouched", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const prepared = tmpdir();
  writeStamp(path.join(prepared, "KiroCrew.app"), "deadbeef"); // not the release sha
  const spawnImpl = dittoCopying(prepared);
  const fetchImpl = applyFetch(Buffer.from("zip"));
  assert.strictEqual(await apply(bundle, { fetch: fetchImpl, spawn: spawnImpl }), 2);
  assert.strictEqual(readForkRevision(bundle), LOCAL);
  assert.deepStrictEqual(leftoverDirs(parent), []);
});

test("apply: second rename failure restores the original bundle", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const prepared = tmpdir();
  writeStamp(path.join(prepared, "KiroCrew.app"), "ccccddd0");
  const spawnImpl = dittoCopying(prepared);
  const fetchImpl = applyFetch(Buffer.from("zip"));

  let renames = 0;
  const failingFs = Object.create(fs);
  failingFs.renameSync = (src, dest) => {
    renames += 1;
    if (renames === 2) throw new Error("rename: cross-device link");
    return fs.renameSync(src, dest);
  };

  assert.strictEqual(
    await apply(bundle, { fetch: fetchImpl, spawn: spawnImpl, fs: failingFs }),
    2,
  );
  // Rollback: the bundle is back with its original stamp.
  assert.strictEqual(readForkRevision(bundle), LOCAL);
  assert.deepStrictEqual(leftoverDirs(parent), []);
  assert.strictEqual(renames, 3); // away, failed-into-place, restore
});

test("apply: not ahead -> exits 1 without touching anything", async () => {
  const parent = tmpdir();
  const bundle = makeBundle(parent, LOCAL);
  const fetchImpl = applyFetch(Buffer.from("zip"), { status: "behind" });
  const spawnImpl = fakeSpawn(() => ({ code: 0, stderr: "" }));
  assert.strictEqual(await apply(bundle, { fetch: fetchImpl, spawn: spawnImpl }), 1);
  assert.strictEqual(readForkRevision(bundle), LOCAL);
  assert.deepStrictEqual(leftoverDirs(parent), []);
});
