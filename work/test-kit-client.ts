// Kit-client round-trip test against the forge (not part of the entry).
import {
  GitClient, ZERO_OID, CONFORMANCE_IDENTITY,
  buildCommit, buildTag, buildTree, hashObject,
} from "/home/hatch/workspace/makefun/github-core-clone-01-kit/github-core-clone-01-kit/sdk/conformance/git-client.ts";

const BASE = process.env.FORGE_URL!;
const client = new GitClient({ baseUrl: BASE, token: "testtoken123" });
const enc = new TextEncoder();
const OWNER = "adaiversen";
const REPO = "kit-test";

let failures = 0;
function check(name: string, ok: boolean, extra = "") {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? " — " + extra : ""}`);
  if (!ok) failures++;
}

function blobObj(data: string) {
  return { type: "blob" as const, data: enc.encode(data) };
}
function treeObj(entries: Array<{ mode: string; name: string; oid: string }>) {
  const data = buildTree(entries);
  return { type: "tree" as const, data };
}
function commitObj(tree: string, parents: string[], message: string, ts = 1700000000) {
  const data = buildCommit({
    tree, parents, message,
    author: CONFORMANCE_IDENTITY, committer: CONFORMANCE_IDENTITY,
    authorTime: ts, committerTime: ts,
  });
  return { type: "commit" as const, data };
}

const blob1 = blobObj("hello forge\n");
const blob1Oid = hashObject("blob", blob1.data);
const tree1 = treeObj([{ mode: "100644", name: "a.txt", oid: blob1Oid }]);
const tree1Oid = hashObject("tree", tree1.data);
const commit1 = commitObj(tree1Oid, [], "first commit\n");
const commit1Oid = hashObject("commit", commit1.data);

await client.createRepo(OWNER, REPO);
check("createRepo", true);

// empty clone
const empty = await client.clone(OWNER, REPO);
check("clone empty repo: 0 objects", empty.objects.size === 0, `${empty.objects.size} objects`);

// push first commit
const push1 = await client.push(OWNER, REPO, [
  { oldOid: ZERO_OID, newOid: commit1Oid, ref: "refs/heads/main" },
], { objects: [blob1, tree1, commit1] });
check("push first commit ok", !!push1.report?.unpackOk && push1.report.refs.every((r) => r.ok),
  JSON.stringify(push1.report));

// clone and verify object-for-object
const cloned = await client.clone(OWNER, REPO);
check("clone: 3 objects", cloned.objects.size === 3, `${cloned.objects.size}`);
check("clone: blob bytes match",
  cloned.objects.get(blob1Oid)?.data?.toString() === "hello forge\n");
check("clone: commit bytes match",
  Buffer.from(cloned.objects.get(commit1Oid)!.data).equals(Buffer.from(commit1.data)));
check("clone: checksum ok", cloned.checksumOk);
check("clone: head is main", cloned.head === "refs/heads/main", String(cloned.head));

// second commit
const blob2 = blobObj("hello forge\nsecond line\n");
const blob2Oid = hashObject("blob", blob2.data);
const tree2 = treeObj([{ mode: "100644", name: "a.txt", oid: blob2Oid }]);
const tree2Oid = hashObject("tree", tree2.data);
const commit2 = commitObj(tree2Oid, [commit1Oid], "second commit\n", 1700000001);
const commit2Oid = hashObject("commit", commit2.data);
const push2 = await client.push(OWNER, REPO, [
  { oldOid: commit1Oid, newOid: commit2Oid, ref: "refs/heads/main" },
], { objects: [blob2, tree2, commit2] });
check("push second commit ok", !!push2.report?.unpackOk && push2.report.refs.every((r) => r.ok),
  JSON.stringify(push2.report));

// incremental fetch: have commit1, want commit2 -> only the 3 new objects
const inc = await client.fetch(OWNER, REPO, { wants: [commit2Oid], haves: [commit1Oid] });
check("incremental fetch: 3 new objects", inc.objects.size === 3,
  `${inc.objects.size} objects: ${[...inc.objects.keys()].join(",")}`);
check("incremental fetch: no old tip", !inc.objects.has(commit1Oid));

// fetch with tip held -> 0 objects
const held = await client.fetch(OWNER, REPO, { wants: [commit2Oid], haves: [commit2Oid] });
check("fetch with tip held: 0 objects", held.objects.size === 0, `${held.objects.size}`);

// shallow depth-1 clone: tip + tree + blob only
const shallow1 = await client.clone(OWNER, REPO, { depth: 1 });
check("shallow depth-1: 3 objects", shallow1.objects.size === 3, `${shallow1.objects.size}`);
check("shallow depth-1: shallow names tip",
  shallow1.shallow.includes(commit2Oid), JSON.stringify(shallow1.shallow));
check("shallow depth-1: parent absent", !shallow1.objects.has(commit1Oid));

// deepen 2: parent + tree arrive, grandparent absent
const deep = await client.fetch(OWNER, REPO, {
  wants: [commit2Oid], shallow: [commit2Oid], depth: 2,
});
check("deepen 2: parent present", deep.objects.has(commit1Oid));
check("deepen 2: unshallow names old tip", deep.unshallow.includes(commit2Oid),
  JSON.stringify(deep.unshallow));

// annotated tag
const tag = buildTag({
  object: commit2Oid, type: "commit", tag: "v1",
  tagger: CONFORMANCE_IDENTITY, taggerTime: 1700000002, message: "version 1\n",
});
const tagOid = hashObject("tag", tag);
const pushTag = await client.push(OWNER, REPO, [
  { oldOid: ZERO_OID, newOid: tagOid, ref: "refs/tags/v1" },
], { objects: [{ type: "tag", data: tag }] });
check("push annotated tag ok", !!pushTag.report?.unpackOk && pushTag.report.refs.every((r) => r.ok),
  JSON.stringify(pushTag.report));
const ls = await client.lsRefs(OWNER, REPO, { peel: true, symrefs: true });
const tagRef = ls.refs.find((r) => r.name === "refs/tags/v1");
check("ls-refs: tag peels to commit2", tagRef?.peeled === commit2Oid,
  JSON.stringify(tagRef));
const headRef = ls.refs.find((r) => r.name === "HEAD");
check("ls-refs: HEAD symref-target main", headRef?.symrefTarget === "refs/heads/main",
  JSON.stringify(headRef));

// non-fast-forward push -> ng with reason
const rogue = commitObj(tree1Oid, [], "rogue root\n", 1700000003);
const rogueOid = hashObject("commit", rogue.data);
const pushRogue = await client.push(OWNER, REPO, [
  { oldOid: commit2Oid, newOid: rogueOid, ref: "refs/heads/main" },
], { objects: [rogue] });
check("non-fast-forward refused",
  (pushRogue.report?.refs.length ?? 0) === 1 && !pushRogue.report!.refs[0].ok
    && /non-fast-forward/.test(pushRogue.report!.refs[0].reason ?? ""),
  JSON.stringify(pushRogue.report));
// ref must not have moved
const ls2 = await client.lsRefs(OWNER, REPO, {});
const mainRef = ls2.refs.find((r) => r.name === "refs/heads/main");
check("non-fast-forward: ref unmoved", mainRef?.oid === commit2Oid,
  JSON.stringify(mainRef));

// branch create + delete
const pushBranch = await client.push(OWNER, REPO, [
  { oldOid: ZERO_OID, newOid: commit2Oid, ref: "refs/heads/dev" },
], { objects: [] });
check("create branch dev ok", !!pushBranch.report?.unpackOk && pushBranch.report.refs.every((r) => r.ok),
  JSON.stringify(pushBranch.report));
const pushDel = await client.push(OWNER, REPO, [
  { oldOid: commit2Oid, newOid: ZERO_OID, ref: "refs/heads/dev" },
], { objects: [] });
check("delete branch dev ok", !!pushDel.report?.unpackOk && pushDel.report.refs.every((r) => r.ok),
  JSON.stringify(pushDel.report));

// v0 protocol client
const v0 = new GitClient({ baseUrl: BASE, token: "testtoken123", gitProtocol: 0 });
const v0clone = await v0.clone(OWNER, REPO);
check("v0 clone: objects present", v0clone.objects.size >= 3, `${v0clone.objects.size}`);
check("v0 clone: protocol 0", v0clone.protocol === 0, String(v0clone.protocol));

// missing repo -> 404 both services
let missing404 = false;
try {
  await client.clone(OWNER, "does-not-exist");
} catch (e: any) {
  missing404 = /404/.test(String(e?.message ?? e));
}
check("missing repo clone -> 404", missing404);

// malformed POST must not crash the server
const bad = await fetch(`${BASE}/${OWNER}/${REPO}.git/git-upload-pack`, {
  method: "POST",
  headers: { Authorization: "Bearer testtoken123", "Content-Type": "application/x-git-upload-pack-request" },
  body: "this is not pkt-lines",
});
check("malformed upload-pack -> 400, server alive", bad.status === 400, String(bad.status));
const health = await fetch(`${BASE}/healthz`);
check("server alive after malformed request", health.ok);

console.log(failures === 0 ? "ALL KIT-CLIENT TESTS PASS" : `${failures} FAILURES`);
process.exit(failures === 0 ? 0 : 1);
