const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

function loadData(directory) {
  const read = (name) =>
    JSON.parse(fs.readFileSync(path.join(directory, name), "utf8"));
  const manifest = read("manifest.json");
  for (const name of [
    "film.json",
    "dependencies.json",
    "phases.json",
    "story.json",
  ]) {
    const sha = crypto
      .createHash("sha256")
      .update(fs.readFileSync(path.join(directory, name)))
      .digest("hex");
    if (manifest.sha256[name] !== sha)
      throw new Error(`Snapshot checksum mismatch: ${name}`);
  }
  const D = read("film.json"),
    G = read("dependencies.json"),
    PH = read("phases.json"),
    story = read("story.json");
  if (!Number.isFinite(D.start) || !Number.isFinite(D.end) || D.start >= D.end)
    throw new Error("Invalid history interval");
  if (D.sourceHead !== G.sourceHead)
    throw new Error("History and dependencies have different source heads");
  if (!D.files.length || D.commits.length !== G.versions.length)
    throw new Error("Incomplete history");
  const id = (n) => Number.isInteger(n) && n >= 0 && n < D.files.length;
  let previous = D.start;
  for (let i = 0; i < D.commits.length; i++) {
    const commit = D.commits[i],
      version = G.versions[i];
    if (commit.sha !== version.sha || commit.at < previous || commit.at > D.end)
      throw new Error("Invalid commit sequence");
    previous = commit.at;
    const sizes = new Map(version.sizes);
    if (
      commit.changes.some(
        ([n, op]) =>
          !id(n) || !["A", "M", "D", "T"].includes(op) || !sizes.has(n),
      )
    )
      throw new Error("Missing changed-file size");
    if (
      version.sizes.some(
        ([n, bytes]) => !id(n) || !Number.isSafeInteger(bytes) || bytes < 0,
      )
    )
      throw new Error("Invalid file size");
    if (
      version.updates.some(
        ([n, targets]) => !id(n) || targets.some(([to]) => !id(to)),
      )
    )
      throw new Error("Invalid dependency update");
  }
  if (G.edges.some(([a, b]) => !id(a) || !id(b)))
    throw new Error("Invalid layout edge");
  const tasks = new Set(D.tasks.map((t) => t.id));
  if (
    tasks.size !== D.tasks.length ||
    D.commits.some((c) => c.task && !tasks.has(c.task))
  )
    throw new Error("Invalid task links");
  for (const t of D.tasks) {
    let at = -Infinity;
    for (const e of t.events) {
      if (
        !Number.isFinite(e[0]) ||
        e[0] < at ||
        e[0] > D.end ||
        !Number.isInteger(e[1]) ||
        e[1] < 0 ||
        e[1] > 8
      )
        throw new Error("Invalid task timeline");
      at = e[0];
    }
  }
  if (!Array.isArray(story.knots) || story.knots.length < 4)
    throw new Error("Missing story timing");
  story.knots.forEach(([f, at], i) => {
    if (
      !Number.isFinite(f) ||
      !Number.isFinite(at) ||
      (i && (f <= story.knots[i - 1][0] || at < story.knots[i - 1][1]))
    )
      throw new Error("Invalid story timing");
  });
  if (
    story.knots[0][0] !== 0 ||
    story.knots.at(-1)[0] !== 60 ||
    story.knots[0][1] !== D.start ||
    story.knots.at(-1)[1] !== D.end
  )
    throw new Error("Story must cover the history");
  return { D, G, PH, story, manifest };
}

module.exports = { loadData };
