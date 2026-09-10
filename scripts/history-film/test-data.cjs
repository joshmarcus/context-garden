const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { loadData } = require("./data.cjs");

test("preserved snapshot covers the approved cut and every changed file", () => {
  const { D, G, PH } = loadData(path.join(__dirname, "snapshot"));
  assert.equal(D.sourceHead, "6e4f3521a1e28e59c0f9283968af3de114da7e3a");
  assert.equal(D.tasks.length, 504);
  assert.equal(D.commits.length, 406);
  assert.equal(D.stats.matchedMerges, 384);
  assert.equal(G.stats.finalEdges, 1476);
  assert.equal(G.stats.parseErrors, 0);
  assert.equal(PH.closed.length, 4);
  assert.ok(
    D.tasks.some((t) =>
      t.events.some((e) => e[2] === "Changes requested" && e[3]),
    ),
  );
  for (const t of D.tasks) {
    assert.deepEqual(Object.keys(t).sort(), [
      "created",
      "events",
      "id",
      "merges",
      "phase",
      "title",
    ]);
    assert.ok(
      t.events.every((e) => e.length === 4 && typeof e[3] === "boolean"),
    );
  }
});

for (const [name, edit, rehash, expected] of [
  [
    "modified input",
    (d) => {
      d.sourceHead = "changed";
    },
    false,
    /checksum/,
  ],
  [
    "mixed captures",
    (d) => {
      d.sourceHead = "changed";
    },
    true,
    /different source heads/,
  ],
  [
    "missing blob sizes",
    (d) => {
      d.versions[0].sizes = [];
    },
    true,
    /Missing changed-file size/,
  ],
]) {
  test(`rejects ${name}`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "garden-film-"));
    try {
      fs.cpSync(path.join(__dirname, "snapshot"), dir, { recursive: true });
      const file = path.join(dir, "dependencies.json");
      const data = JSON.parse(fs.readFileSync(file));
      edit(data);
      fs.writeFileSync(file, JSON.stringify(data));
      if (rehash) {
        const manifestFile = path.join(dir, "manifest.json");
        const manifest = JSON.parse(fs.readFileSync(manifestFile));
        manifest.sha256["dependencies.json"] = crypto
          .createHash("sha256")
          .update(fs.readFileSync(file))
          .digest("hex");
        fs.writeFileSync(manifestFile, JSON.stringify(manifest));
      }
      assert.throws(() => loadData(dir), expected);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
}
