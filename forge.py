# forge.py — a GitHub-compatible git forge in one Python process.
#
# One process serves all four surfaces the contest scores, from one store:
#   git smart HTTP  /{owner}/{repo}.git  (upload-pack v2, receive-pack v0)
#   REST v3         /api/v3
#   GraphQL v4      /api/graphql
#   MCP             /mcp  (streamable HTTP, 2026-07-28 with a legacy fallback)
#
# The store is in-memory and deterministic: ids are sequential counters, commit
# timestamps come from MAKEFUN_TRIAL_NOW (never the wall clock), request ids
# are a counter, and the viewer login is derived from the competition's fixed
# scenario seed. Nothing here reads the clock or unseeded randomness.

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import threading
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from gitcore import (
    CONFORMANCE_IDENTITY, DEFAULT_TIMESTAMP, ZERO_OID, GitError, PktReader,
    apply_delta, build_commit, build_tag, build_tree, collect_reachable,
    decode_all_packets, encode_pkt_lines, global_id, hash_object,
    is_oid, parse_commit, parse_global_id, parse_tag, parse_tree,
    pkt, read_pack, scenario_users, verify_pack_checksum, write_pack,
)

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

FORGE_NAME = "forge"
FORGE_VERSION = "1.0.0"
AGENT_NAME = "forge/1.0"

USERS = scenario_users()
VIEWER = USERS[0]            # adaiversen — the login the bearer token names
TEAMMATE = USERS[1]
STRANGER = USERS[2]

EXPECTED_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")


def _trial_now() -> int:
    """MAKEFUN_TRIAL_NOW: the instance's opening time, the only now allowed."""
    raw = os.environ.get("MAKEFUN_TRIAL_NOW", "")
    if raw:
        try:
            value = float(raw)
            if value > 1e11:      # milliseconds
                return int(value // 1000)
            if value > 1e9:
                return int(value)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            pass
    return DEFAULT_TIMESTAMP


TRIAL_NOW = _trial_now()


def iso_now(seq: int = 0) -> str:
    """A deterministic ISO timestamp: trial opening + a sequence offset."""
    moment = datetime.fromtimestamp(TRIAL_NOW + seq, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode("".join(text.split()).encode("ascii"))


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class Repo:
    __slots__ = ("id", "owner", "name", "private", "description", "default_branch",
                 "refs", "objects", "issues", "issue_next", "labels", "pulls",
                 "pull_next", "comment_next", "seq", "created_seq")

    def __init__(self, store, owner, name, private, description):
        self.id = store.next_id("repo")
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = "main"
        self.refs = {}            # full refname -> commit oid
        self.objects = {}         # oid -> (otype, bytes)
        self.issues = {}          # number -> issue dict
        self.issue_next = 1
        self.labels = {}          # name -> color
        self.pulls = {}           # number -> pull dict
        self.pull_next = 1
        self.comment_next = 1
        self.seq = store.next_seq()
        self.created_seq = self.seq

    # -- git object store -------------------------------------------------
    def get_object(self, oid):
        return self.objects.get(oid)

    def put_object(self, otype, data):
        oid = hash_object(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def ref_tip(self, refname):
        return self.refs.get(refname)

    # -- git tree helpers --------------------------------------------------
    def flatten_tree(self, tree_oid):
        """{path: (mode, oid)} for every blob under a tree, recursively."""
        flat = {}

        def walk(tree, prefix):
            found = self.get_object(tree)
            if found is None or found[0] != "tree":
                return
            for mode, name, oid in parse_tree(found[1]):
                path = prefix + name
                if mode == "40000":
                    walk(oid, path + "/")
                elif mode != "160000":
                    flat[path] = (mode, oid)

        walk(tree_oid, "")
        return flat

    def build_tree_flat(self, flat):
        """Build (and store) the tree objects for a {path: (mode, oid)} map."""
        def build(paths):
            groups = {}
            for path, (mode, oid) in paths.items():
                head, _, tail = path.partition("/")
                groups.setdefault(head, []).append((tail, mode, oid))
            entries = []
            for head, items in groups.items():
                if len(items) == 1 and items[0][0] == "":
                    _tail, mode, oid = items[0]
                    entries.append((mode, head, oid))
                else:
                    sub = {tail: (mode, oid) for tail, mode, oid in items}
                    entries.append(("40000", head, build(sub)))
            tree = build_tree(entries)
            return self.put_object("tree", tree)

        if not flat:
            return self.put_object("tree", build_tree([]))
        return build(dict(flat))

    def commit_view(self, oid):
        found = self.get_object(oid)
        if found is None or found[0] != "commit":
            return None
        return parse_commit(found[1])

    def resolve_ref(self, ref):
        """A branch name, full refname or oid -> commit oid (or None)."""
        if ref is None:
            ref = self.default_branch
        if is_oid(ref):
            return ref if self.get_object(ref) else None
        if ref.startswith("refs/"):
            return self.refs.get(ref)
        tip = self.refs.get("refs/heads/" + ref)
        if tip is not None:
            return tip
        return self.refs.get("refs/tags/" + ref)

    def history(self, tip, limit=None):
        """Commit oids newest-first, following first parents."""
        out = []
        seen = set()
        current = tip
        while current and current not in seen:
            seen.add(current)
            view = self.commit_view(current)
            if view is None:
                break
            out.append(current)
            if limit is not None and len(out) >= limit:
                break
            current = view["parents"][0] if view["parents"] else None
        return out

    def is_ancestor(self, ancestor, descendant):
        """True when ancestor is reachable from descendant (any parent)."""
        if ancestor == descendant:
            return True
        seen = set()
        queue = [descendant]
        while queue:
            oid = queue.pop()
            if oid in seen:
                continue
            seen.add(oid)
            view = self.commit_view(oid)
            if view is None:
                continue
            for parent in view["parents"]:
                if parent == ancestor:
                    return True
                queue.append(parent)
        return False

    def merge_base(self, a, b):
        """A common ancestor of a and b (newest-first walk from both)."""
        if a is None or b is None:
            return None
        seen_a = {}
        queue = [(a, 0)]
        while queue:
            oid, depth = queue.pop(0)
            if oid in seen_a:
                continue
            seen_a[oid] = depth
            view = self.commit_view(oid)
            if view:
                queue.extend((p, depth + 1) for p in view["parents"])
        queue = [b]
        seen_b = set()
        while queue:
            oid = queue.pop(0)
            if oid in seen_b:
                continue
            seen_b.add(oid)
            if oid in seen_a:
                return oid
            view = self.commit_view(oid)
            if view:
                queue.extend(view["parents"])
        return None

    def diff_trees(self, base_tree, head_tree):
        """Changed files between two trees: [{filename, status, additions,
        deletions, changes}] with real line counts."""
        base = self.flatten_tree(base_tree) if base_tree else {}
        head = self.flatten_tree(head_tree) if head_tree else {}
        out = []
        for path in sorted(set(base) | set(head)):
            in_base = path in base
            in_head = path in head
            if in_base and not in_head:
                out.append({"filename": path, "status": "removed",
                            "additions": 0, "deletions": _line_count(repo_blob(self, base[path][1])),
                            "changes": _line_count(repo_blob(self, base[path][1]))})
            elif in_head and not in_base:
                content = repo_blob(self, head[path][1])
                out.append({"filename": path, "status": "added",
                            "additions": _line_count(content), "deletions": 0,
                            "changes": _line_count(content)})
            elif base[path][1] != head[path][1]:
                old = repo_blob(self, base[path][1])
                new = repo_blob(self, head[path][1])
                adds, dels = _diff_counts(old, new)
                out.append({"filename": path, "status": "modified",
                            "additions": adds, "deletions": dels,
                            "changes": adds + dels})
        return out


def repo_blob(repo, oid):
    found = repo.get_object(oid)
    return found[1] if found and found[0] == "blob" else b""


def _line_count(data: bytes) -> int:
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _diff_counts(old: bytes, new: bytes):
    old_lines = old.decode("utf-8", "replace").splitlines()
    new_lines = new.decode("utf-8", "replace").splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    adds = dels = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            adds += j2 - j1
        if tag in ("delete", "replace"):
            dels += i2 - i1
    return adds, dels


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}           # (owner, name) -> Repo
        self._seq = 0
        self._ids = {"repo": 0, "issue": 0, "comment": 0, "pull": 0, "label": 0}
        self._req = 0
        self._commit_seq = 0

    def next_seq(self):
        self._seq += 1
        return self._seq

    def next_id(self, kind):
        self._ids[kind] += 1
        return self._ids[kind]

    def next_request_id(self):
        self._req += 1
        return "%016x" % self._req

    def next_commit_time(self):
        self._commit_seq += 1
        return TRIAL_NOW + self._commit_seq

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def create_repo(self, owner, name, private, description):
        repo = Repo(self, owner, name, private, description)
        self.repos[(owner, name)] = repo
        return repo

    def delete_repo(self, owner, name):
        self.repos.pop((owner, name), None)

    def viewer_repos(self):
        return sorted((r for (o, _n), r in self.repos.items() if o == VIEWER["login"]),
                      key=lambda r: r.seq)

    # -- shared object builders -------------------------------------------
    def make_commit(self, repo, tree_oid, parents, message):
        author = "%s <%s>" % (VIEWER["login"], VIEWER["email"])
        stamp = self.next_commit_time()
        data = build_commit(tree_oid, parents, message, author, author, stamp)
        return repo.put_object("commit", data)

    def contents_commit(self, repo, branch, path, content, message, sha=None, delete=False):
        """Write/delete one file on a branch through a real git commit.
        Returns (commit_oid, blob_oid|None, created_bool)."""
        tip = repo.ref_tip("refs/heads/" + branch)
        if tip is None:
            if repo.refs:
                base_tip = repo.ref_tip("refs/heads/" + repo.default_branch)
                if base_tip is None:
                    raise ValueError("branch %s does not exist" % branch)
                tip = base_tip
                parents = [tip]
                flat = repo.flatten_tree(repo.commit_view(tip)["tree"])
            else:
                parents = []
                flat = {}
        else:
            parents = [tip]
            flat = repo.flatten_tree(repo.commit_view(tip)["tree"])
        created = path not in flat
        if delete:
            if created:
                raise KeyError(path)
            del flat[path]
            blob_oid = None
        else:
            if not created and sha is not None and sha.lower() != flat[path][1].lower():
                raise ValueError("stale sha")
            if not created and sha is None:
                raise ValueError("sha is required to update a file")
            blob_oid = repo.put_object("blob", content)
            flat[path] = ("100644", blob_oid)
        tree_oid = repo.build_tree_flat(flat)
        commit_oid = self.make_commit(repo, tree_oid, parents, message)
        repo.refs["refs/heads/" + branch] = commit_oid
        return commit_oid, blob_oid, created

    def merge_pull(self, repo, pull):
        """Squash-merge a pull request with a three-way merge.
        Returns (merge_commit_oid|None, conflict_bool)."""
        head_tip = repo.ref_tip("refs/heads/" + pull["head"])
        base_tip = repo.ref_tip("refs/heads/" + pull["base"])
        if head_tip is None or base_tip is None:
            return None, True
        base_commit = repo.merge_base(head_tip, base_tip)
        if base_commit is None:
            return None, True
        base_view = repo.commit_view(base_commit)
        head_view = repo.commit_view(head_tip)
        cur_view = repo.commit_view(base_tip)
        base_flat = repo.flatten_tree(base_view["tree"])
        head_flat = repo.flatten_tree(head_view["tree"])
        cur_flat = repo.flatten_tree(cur_view["tree"])
        merged = dict(cur_flat)
        for path in sorted(set(base_flat) | set(head_flat) | set(cur_flat)):
            b = base_flat.get(path)
            h = head_flat.get(path)
            c = cur_flat.get(path)
            head_changed = h != b
            cur_changed = c != b
            if head_changed and cur_changed and h != c:
                return None, True
            if head_changed:
                if h is None:
                    merged.pop(path, None)
                else:
                    merged[path] = h
        tree_oid = repo.build_tree_flat(merged)
        message = "Merge pull request #%d from %s" % (pull["number"], pull["head"])
        commit_oid = self.make_commit(repo, tree_oid, [base_tip], message)
        repo.refs["refs/heads/" + pull["base"]] = commit_oid
        return commit_oid, False

    def pr_mergeable(self, repo, pull):
        """MERGEABLE / CONFLICTING / UNKNOWN for a pull request."""
        head_tip = repo.ref_tip("refs/heads/" + pull["head"])
        base_tip = repo.ref_tip("refs/heads/" + pull["base"])
        if head_tip is None or base_tip is None:
            return "UNKNOWN"
        if pull["merged"]:
            return "MERGEABLE"
        base_commit = repo.merge_base(head_tip, base_tip)
        if base_commit is None:
            return "UNKNOWN"
        base_flat = repo.flatten_tree(repo.commit_view(base_commit)["tree"])
        head_flat = repo.flatten_tree(repo.commit_view(head_tip)["tree"])
        cur_flat = repo.flatten_tree(repo.commit_view(base_tip)["tree"])
        for path in sorted(set(base_flat) | set(head_flat) | set(cur_flat)):
            b = base_flat.get(path)
            h = head_flat.get(path)
            c = cur_flat.get(path)
            if h != b and c != b and h != c:
                return "CONFLICTING"
        return "MERGEABLE"


STORE = Store()

# ---------------------------------------------------------------------------
# JSON views (shared by REST, GraphQL and MCP)
# ---------------------------------------------------------------------------

def user_view(user):
    return {"login": user["login"], "id": user["id"], "node_id": global_id("User", user["id"]),
            "type": "User", "avatar_url": "https://example.invalid/avatar/%d" % user["id"],
            "html_url": "https://github.com/%s" % user["login"]}


def repo_view(repo):
    return {"id": repo.id, "node_id": global_id("Repository", repo.id),
            "name": repo.name, "full_name": "%s/%s" % (repo.owner, repo.name),
            "owner": user_view(VIEWER if repo.owner == VIEWER["login"] else
                               _user_by_login(repo.owner)),
            "private": repo.private, "description": repo.description,
            "default_branch": repo.default_branch, "has_issues": True,
            "has_projects": True, "has_wiki": True, "fork": False,
            "created_at": iso_now(repo.created_seq),
            "updated_at": iso_now(repo.seq),
            "pushed_at": iso_now(repo.seq),
            "html_url": "https://github.com/%s/%s" % (repo.owner, repo.name),
            "url": "/api/v3/repos/%s/%s" % (repo.owner, repo.name),
            "clone_url": "https://github.com/%s/%s.git" % (repo.owner, repo.name),
            "git_url": "git://github.com/%s/%s.git" % (repo.owner, repo.name)}


def _user_by_login(login):
    for user in USERS:
        if user["login"] == login:
            return user
    return VIEWER


def label_view(label):
    return {"id": label["id"], "node_id": global_id("Label", label["id"]),
            "name": label["name"], "color": label["color"],
            "description": label.get("description", ""), "default": False}


def issue_view(repo, issue):
    return {"id": issue["id"], "node_id": global_id("Issue", issue["id"]),
            "number": issue["number"], "title": issue["title"],
            "body": issue.get("body", ""), "state": issue["state"],
            "labels": [label_view(repo.labels[name])
                       for name in issue["labels"] if name in repo.labels],
            "user": user_view(VIEWER), "assignee": None, "assignees": [],
            "created_at": issue["created_at"], "updated_at": issue["updated_at"],
            "comments": len(issue["comments"]),
            "html_url": "https://github.com/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"]),
            "url": "/api/v3/repos/%s/%s/issues/%d" % (repo.owner, repo.name, issue["number"]),
            "comments_url": "/api/v3/repos/%s/%s/issues/%d/comments" % (repo.owner, repo.name, issue["number"])}


def comment_view(repo, issue, comment):
    return {"id": comment["id"], "node_id": global_id("IssueComment", comment["id"]),
            "body": comment["body"], "user": user_view(VIEWER),
            "created_at": comment["created_at"], "updated_at": comment["updated_at"],
            "html_url": "https://github.com/%s/%s/issues/%d#issuecomment-%d" % (
                repo.owner, repo.name, issue["number"], comment["id"])}


def pull_view(repo, pull):
    head_tip = repo.ref_tip("refs/heads/" + pull["head"]) or ""
    base_tip = repo.ref_tip("refs/heads/" + pull["base"]) or ""
    return {"id": pull["id"], "node_id": global_id("PullRequest", pull["id"]),
            "number": pull["number"], "title": pull["title"],
            "body": pull.get("body", ""), "state": pull["state"],
            "merged": pull["merged"],
            "merged_at": pull["merged_at"],
            "merge_commit_sha": pull["merge_commit_sha"],
            "head": {"ref": pull["head"], "sha": head_tip,
                     "label": "%s:%s" % (repo.owner, pull["head"]),
                     "user": user_view(VIEWER)},
            "base": {"ref": pull["base"], "sha": base_tip,
                     "label": "%s:%s" % (repo.owner, pull["base"]),
                     "user": user_view(VIEWER)},
            "user": user_view(VIEWER),
            "created_at": pull["created_at"], "updated_at": pull["updated_at"],
            "html_url": "https://github.com/%s/%s/pull/%d" % (repo.owner, repo.name, pull["number"]),
            "url": "/api/v3/repos/%s/%s/pulls/%d" % (repo.owner, repo.name, pull["number"])}


def commit_view(repo, oid):
    view = repo.commit_view(oid)
    author = {"name": VIEWER["login"], "email": VIEWER["email"],
              "date": iso_now(0)}
    if view is not None:
        author = view["author"]
    return {"sha": oid, "node_id": global_id("Commit", oid),
            "commit": {"message": view["message"] if view else "",
                       "author": author, "committer": author,
                       "tree": {"sha": view["tree"] if view else ""},
                       "verification": {"verified": False, "reason": "unsigned"}},
            "author": user_view(VIEWER), "committer": user_view(VIEWER),
            "parents": [{"sha": p} for p in (view["parents"] if view else [])]}


def ref_view(repo, refname, oid):
    found = repo.get_object(oid) if repo is not None else None
    otype = found[0] if found else "commit"
    return {"ref": refname, "node_id": global_id("Ref", refname),
            "object": {"sha": oid, "type": otype, "url": ""},
            "url": ""}


def contents_file_view(repo, path, blob_oid, content):
    return {"type": "file", "encoding": "base64", "size": len(content),
            "name": path.rsplit("/", 1)[-1], "path": path, "sha": blob_oid,
            "content": b64encode(content),
            "url": "/api/v3/repos/%s/%s/contents/%s" % (repo.owner, repo.name, path),
            "html_url": "https://github.com/%s/%s/blob/main/%s" % (repo.owner, repo.name, path),
            "git_url": "", "download_url": ""}


def contents_dir_view(repo, path, name, oid, is_dir, size=0):
    return {"type": "dir" if is_dir else "file", "size": size,
            "name": name, "path": path, "sha": oid,
            "url": "/api/v3/repos/%s/%s/contents/%s" % (repo.owner, repo.name, path),
            "html_url": "https://github.com/%s/%s/tree/main/%s" % (repo.owner, repo.name, path) if is_dir
                        else "https://github.com/%s/%s/blob/main/%s" % (repo.owner, repo.name, path),
            "git_url": "", "download_url": ""}


# ---------------------------------------------------------------------------
# shared internal operations (REST / GraphQL / MCP all call these)
# ---------------------------------------------------------------------------

def op_create_repo(store, owner, name, private=False, description=""):
    if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("Invalid repository name")
    existing = store.get_repo(owner, name)
    if existing is not None:
        raise KeyError("already exists")
    return store.create_repo(owner, name, bool(private), description or "")


def op_create_issue(store, repo, title, body="", labels=None):
    number = repo.issue_next
    repo.issue_next += 1
    seq = store.next_seq()
    issue = {"id": store.next_id("issue"), "number": number, "title": title,
             "body": body or "", "state": "open",
             "labels": [label for label in (labels or [])
                        if isinstance(label, str) and label in repo.labels],
             "comments": [], "created_at": iso_now(seq), "updated_at": iso_now(seq),
             "seq": seq}
    repo.issues[number] = issue
    return issue


def op_create_label(store, repo, name, color):
    if name in repo.labels:
        return repo.labels[name]
    label = {"name": name, "color": color, "id": store.next_id("label")}
    repo.labels[name] = label
    return label


def op_add_comment(store, repo, issue, body):
    comment = {"id": store.next_id("comment"), "body": body,
               "created_at": iso_now(store.next_seq()),
               "updated_at": iso_now(store.next_seq())}
    issue["comments"].append(comment)
    return comment


def op_create_pull(store, repo, title, head, base, body=""):
    if repo.ref_tip("refs/heads/" + head) is None:
        raise KeyError("head branch not found")
    if repo.ref_tip("refs/heads/" + base) is None:
        raise KeyError("base branch not found")
    number = repo.pull_next
    repo.pull_next += 1
    seq = store.next_seq()
    pull = {"id": store.next_id("pull"), "number": number, "title": title,
            "body": body or "", "head": head, "base": base, "state": "open",
            "merged": False, "merged_at": None, "merge_commit_sha": None,
            "created_at": iso_now(seq), "updated_at": iso_now(seq)}
    repo.pulls[number] = pull
    return pull


def op_merge_pull(store, repo, pull, method="squash"):
    if pull["merged"]:
        return pull
    if pull["state"] != "open":
        raise ValueError("pull request is not open")
    if store.pr_mergeable(repo, pull) == "CONFLICTING":
        raise RuntimeError("conflict")
    commit_oid, conflict = store.merge_pull(repo, pull)
    if conflict or commit_oid is None:
        raise RuntimeError("conflict")
    pull["merged"] = True
    pull["state"] = "closed"
    pull["merged_at"] = iso_now(store.next_seq())
    pull["merge_commit_sha"] = commit_oid
    pull["updated_at"] = pull["merged_at"]
    return pull


def paginate(items, query, default_per_page=30):
    try:
        per_page = int(query.get("per_page", [default_per_page])[0])
    except ValueError:
        per_page = default_per_page
    per_page = max(1, min(100, per_page))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    return items[start:start + per_page], per_page, page, len(items)


def link_header(host, path, query, per_page, page, total):
    parts = []
    last = max(1, (total + per_page - 1) // per_page)
    if page < last:
        parts.append('<%s%s?%s>; rel="next"' % (
            host, path, _qs(query, per_page, page + 1)))
    if last > 1:
        parts.append('<%s%s?%s>; rel="last"' % (
            host, path, _qs(query, per_page, last)))
    return ", ".join(parts) if parts else None


def _qs(query, per_page, page):
    params = dict(query)
    params["per_page"] = [str(per_page)]
    params["page"] = [str(page)]
    return "&".join("%s=%s" % (k, v[0]) for k, v in params.items())

# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Forge/1.0.0"

    def log_message(self, *args):
        pass

    # -- verbs ------------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    # -- plumbing ---------------------------------------------------------
    def _send(self, status, body: bytes, content_type, extra=None):
        headers = {"Content-Type": content_type,
                   "Content-Length": str(len(body)),
                   "X-GitHub-Request-Id": STORE.next_request_id()}
        if extra:
            headers.update(extra)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, obj, extra=None):
        if obj is None:
            self._send(status, b"", "application/json", extra)
            return
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._send(status, payload, "application/json", extra)

    def _send_error(self, status, message):
        self._send_json(status, {"message": message})

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        return self.rfile.read(size)

    def _parse_json(self, raw):
        if not raw:
            return None, None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, "Invalid JSON in request body"
        return value, None

    def _authenticated(self):
        if not EXPECTED_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return header == "Bearer " + EXPECTED_TOKEN

    def _dispatch(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never let one request kill the process
            import traceback
            traceback.print_exc()
            try:
                self._send_error(500, "Internal Server Error")
            except Exception:
                pass

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        method = self.command
        raw = self._read_body()

        if path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        if path.startswith("/api/v3"):
            self._handle_rest(method, path[len("/api/v3"):], query, raw)
            return

        if path == "/api/graphql":
            if method != "POST":
                self._send_error(405, "Method Not Allowed")
                return
            self._handle_graphql(raw)
            return

        if path == "/mcp":
            if method != "POST":
                self._send_error(405, "Method Not Allowed")
                return
            self._handle_mcp(raw)
            return

        match = re.fullmatch(r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)\.git(/.*)?", path)
        if match:
            self._handle_git(method, match.group(1), match.group(2),
                             match.group(3) or "", query, raw)
            return

        self._send_error(404, "Not Found")

    # -- REST -------------------------------------------------------------
    def _rest_headers(self):
        return {"X-OAuth-Scopes": "repo, read:org"}

    def _handle_rest(self, method, subpath, query, raw):
        if not self._authenticated() and subpath not in ("", "/", "/meta"):
            self._send_json(401, {"message": "Requires authentication"}, self._rest_headers())
            return
        body, err = self._parse_json(raw)
        if err:
            self._send_json(400, {"message": err}, self._rest_headers())
            return
        segments = [unquote(s) for s in subpath.strip("/").split("/") if s != ""]

        if segments == []:
            if method == "GET":
                self._send_json(200, {
                    "current_user_url": "/api/v3/user",
                    "repository_url": "/api/v3/repos/{owner}/{repo}",
                    "user_url": "/api/v3/users/{user}",
                    "meta": "/api/v3/meta",
                }, self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return

        if segments == ["meta"]:
            if method == "GET":
                self._send_json(200, {
                    "installed_version": FORGE_VERSION,
                    "verifiable_password_authentication": False,
                    "github_services_sha": "0000000000000000000000000000000000000000",
                    "hooks": ["pre-receive"],
                    "git": ["2.40.0"],
                    "pages": ["1.0.0"],
                    "importer": ["1.0.0"],
                    "packages": ["1.0.0"],
                }, self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return

        if segments[0] == "user":
            self._handle_user(method, segments, query, body)
            return

        if segments[0] == "repos" and len(segments) >= 3:
            self._handle_repo(method, segments[1], segments[2], segments[3:],
                              query, body)
            return

        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _need_repo(self, owner, name):
        repo = STORE.get_repo(owner, name)
        if repo is None:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return None
        return repo

    def _handle_user(self, method, segments, query, body):
        if segments == ["user"]:
            if method == "GET":
                self._send_json(200, user_view(VIEWER), self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if segments == ["user", "repos"]:
            if method == "POST":
                if not isinstance(body, dict):
                    self._send_json(400, {"message": "request body must be a JSON object"},
                                    self._rest_headers())
                    return
                name = body.get("name")
                try:
                    repo = op_create_repo(STORE, VIEWER["login"], name,
                                          body.get("private", False),
                                          body.get("description", ""))
                except ValueError as exc:
                    self._send_json(400, {"message": str(exc)}, self._rest_headers())
                    return
                except KeyError:
                    self._send_json(422, {"message": "name already exists on this account"},
                                    self._rest_headers())
                    return
                self._send_json(201, repo_view(repo), self._rest_headers())
                return
            if method == "GET":
                repos = STORE.viewer_repos()
                page_items, per_page, page, total = paginate(
                    [repo_view(r) for r in repos], query, 30)
                extra = dict(self._rest_headers())
                host = self.headers.get("Host", "localhost")
                link = link_header("http://" + host, "/api/v3/user/repos",
                                   query, per_page, page, total)
                if link:
                    extra["Link"] = link
                self._send_json(200, page_items, extra)
                return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_repo(self, method, owner, name, rest, query, body):
        repo = self._need_repo(owner, name)
        if repo is None:
            return
        if rest == []:
            if method == "GET":
                self._send_json(200, repo_view(repo), self._rest_headers())
            elif method == "PATCH":
                if isinstance(body, dict):
                    if "description" in body:
                        repo.description = body["description"] or ""
                    if "private" in body:
                        repo.private = bool(body["private"])
                    if "default_branch" in body:
                        repo.default_branch = body["default_branch"]
                self._send_json(200, repo_view(repo), self._rest_headers())
            elif method == "DELETE":
                STORE.delete_repo(owner, name)
                self._send(204, b"", "application/json", self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return

        head = rest[0]
        if head == "branches":
            self._handle_branches(method, repo, rest[1:], query, body)
        elif head == "commits":
            self._handle_commits(method, repo, query)
        elif head == "git" and len(rest) >= 2 and rest[1] == "refs":
            self._handle_git_refs(method, repo, rest[2:], body)
        elif head == "contents":
            self._handle_contents(method, repo, "/".join(rest[1:]), query, body)
        elif head == "labels":
            self._handle_labels(method, repo, body)
        elif head == "issues":
            self._handle_issues(method, repo, rest[1:], query, body)
        elif head == "pulls":
            self._handle_pulls(method, repo, rest[1:], query, body)
        else:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_branches(self, method, repo, rest, query, body):
        if method != "GET":
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if rest == []:
            branches = []
            for refname in sorted(repo.refs):
                if refname.startswith("refs/heads/"):
                    branches.append({
                        "name": refname[len("refs/heads/"):],
                        "commit": {"sha": repo.refs[refname],
                                   "url": "/api/v3/repos/%s/%s/commits/%s" % (
                                       repo.owner, repo.name, repo.refs[refname])},
                        "protected": False})
            self._send_json(200, branches, self._rest_headers())
            return
        branch = "/".join(rest)
        tip = repo.ref_tip("refs/heads/" + branch)
        if tip is None:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        self._send_json(200, {
            "name": branch,
            "commit": {"sha": tip, "url": "/api/v3/repos/%s/%s/commits/%s" % (
                repo.owner, repo.name, tip)},
            "protected": False}, self._rest_headers())

    def _handle_commits(self, method, repo, query):
        if method != "GET":
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        ref = query.get("sha", [repo.default_branch])[0]
        tip = repo.resolve_ref(ref)
        if tip is None:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        history = repo.history(tip)
        page_items, _pp, _pg, _total = paginate(history, query, 30)
        self._send_json(200, [commit_view(repo, oid) for oid in page_items],
                        self._rest_headers())

    def _handle_git_refs(self, method, repo, rest, body):
        if method == "POST" and rest == []:
            if not isinstance(body, dict) or not body.get("ref") or not body.get("sha"):
                self._send_json(422, {"message": "Validation Failed"}, self._rest_headers())
                return
            refname = body["ref"]
            sha = body["sha"]
            if not refname.startswith("refs/") or repo.ref_tip(refname) is not None:
                self._send_json(422, {"message": "Reference already exists"},
                                self._rest_headers())
                return
            if repo.get_object(sha) is None:
                self._send_json(422, {"message": "Object does not exist"},
                                self._rest_headers())
                return
            repo.refs[refname] = sha
            self._send_json(201, ref_view(repo, refname, sha), self._rest_headers())
            return
        if method == "GET":
            if rest == []:
                self._send_json(200, [ref_view(repo, r, o)
                                      for r, o in sorted(repo.refs.items())],
                                self._rest_headers())
                return
            refname = "refs/" + "/".join(rest)
            tip = repo.ref_tip(refname)
            if tip is None:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
                return
            self._send_json(200, ref_view(repo, refname, tip), self._rest_headers())
            return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_contents(self, method, repo, path, query, body):
        branch = query.get("ref", [repo.default_branch])[0]
        if method == "GET":
            tip = repo.resolve_ref(branch)
            if tip is None:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
                return
            tree_oid = repo.commit_view(tip)["tree"]
            flat = repo.flatten_tree(tree_oid)
            if path == "":
                entries = {}
                for full, (mode, oid) in flat.items():
                    head, _, tail = full.partition("/")
                    if tail == "":
                        entries[head] = (mode, oid, False, _blob_size(repo, oid))
                    else:
                        entries.setdefault(head, ("40000", None, True, 0))
                out = []
                for name in sorted(entries):
                    mode, oid, is_dir, size = entries[name]
                    out.append(contents_dir_view(repo, name, name, oid or "", is_dir, size))
                self._send_json(200, out, self._rest_headers())
                return
            if path in flat:
                mode, oid = flat[path]
                self._send_json(200, contents_file_view(repo, path, oid, repo_blob(repo, oid)),
                                self._rest_headers())
                return
            prefix = path + "/"
            children = {p: v for p, v in flat.items() if p.startswith(prefix)}
            if children:
                entries = {}
                for full, (mode, oid) in children.items():
                    rest_path = full[len(prefix):]
                    head, _, tail = rest_path.partition("/")
                    if tail == "":
                        entries[head] = (mode, oid, False, _blob_size(repo, oid))
                    else:
                        entries.setdefault(head, ("40000", None, True, 0))
                out = []
                for name in sorted(entries):
                    mode, oid, is_dir, size = entries[name]
                    out.append(contents_dir_view(repo, path + "/" + name, name,
                                                 oid or "", is_dir, size))
                self._send_json(200, out, self._rest_headers())
                return
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if method in ("PUT", "DELETE"):
            if not isinstance(body, dict):
                self._send_json(400, {"message": "request body must be a JSON object"},
                                self._rest_headers())
                return
            branch = body.get("branch", branch)
            message = body.get("message", "Update %s" % path)
            if method == "PUT":
                try:
                    content = b64decode(body.get("content", ""))
                except Exception:
                    self._send_json(400, {"message": "content is not valid base64"},
                                    self._rest_headers())
                    return
                try:
                    commit_oid, blob_oid, created = STORE.contents_commit(
                        repo, branch, path, content, message, body.get("sha"))
                except KeyError:
                    self._send_json(404, {"message": "Not Found"}, self._rest_headers())
                    return
                except ValueError as exc:
                    self._send_json(409, {"message": str(exc)}, self._rest_headers())
                    return
                self._send_json(201 if created else 200, {
                    "content": contents_file_view(repo, path, blob_oid, content),
                    "commit": {"sha": commit_oid, "message": message}},
                    self._rest_headers())
                return
            try:
                commit_oid, _blob, _created = STORE.contents_commit(
                    repo, branch, path, b"", message, body.get("sha"), delete=True)
            except KeyError:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
                return
            except ValueError as exc:
                self._send_json(409, {"message": str(exc)}, self._rest_headers())
                return
            self._send_json(200, {"commit": {"sha": commit_oid, "message": message}},
                            self._rest_headers())
            return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_labels(self, method, repo, body):
        if method == "GET":
            self._send_json(200, [label_view(label)
                                  for label in repo.labels.values()],
                            self._rest_headers())
            return
        if method == "POST":
            if not isinstance(body, dict) or not body.get("name"):
                self._send_json(422, {"message": "Validation Failed"}, self._rest_headers())
                return
            color = (body.get("color") or "ededed").lstrip("#")
            label = op_create_label(STORE, repo, body["name"], color)
            self._send_json(201, label_view(label), self._rest_headers())
            return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_issues(self, method, repo, rest, query, body):
        if rest == []:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                sort = query.get("sort", ["created"])[0]
                direction = query.get("direction", ["desc"])[0]
                issues = [i for i in repo.issues.values()
                          if state == "all" or i["state"] == state]
                issues.sort(key=lambda i: i["seq"],
                            reverse=(direction == "desc"))
                page_items, _pp, _pg, _total = paginate(issues, query, 30)
                self._send_json(200, [issue_view(repo, i) for i in page_items],
                                self._rest_headers())
                return
            if method == "POST":
                if not isinstance(body, dict) or not body.get("title"):
                    self._send_json(422, {"message": "Validation Failed"},
                                    self._rest_headers())
                    return
                labels = _label_names(body.get("labels"))
                unknown = [name for name in labels if name not in repo.labels]
                if unknown:
                    self._send_json(422, {"message": "Validation Failed: label '%s' does not exist" % unknown[0]},
                                    self._rest_headers())
                    return
                issue = op_create_issue(STORE, repo, body["title"],
                                        body.get("body", ""), labels)
                self._send_json(201, issue_view(repo, issue), self._rest_headers())
                return
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        try:
            number = int(rest[0])
        except ValueError:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        issue = repo.issues.get(number)
        if issue is None:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if len(rest) == 1:
            if method == "GET":
                self._send_json(200, issue_view(repo, issue), self._rest_headers())
            elif method == "PATCH":
                if isinstance(body, dict):
                    if "state" in body and body["state"] in ("open", "closed"):
                        issue["state"] = body["state"]
                    if "title" in body:
                        issue["title"] = body["title"]
                    if "body" in body:
                        issue["body"] = body["body"]
                    if "labels" in body:
                        names = _label_names(body["labels"])
                        if all(name in repo.labels for name in names):
                            issue["labels"] = names
                    issue["updated_at"] = iso_now(STORE.next_seq())
                self._send_json(200, issue_view(repo, issue), self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if rest[1] == "comments":
            if method == "GET":
                self._send_json(200, [comment_view(repo, issue, c)
                                      for c in issue["comments"]],
                                self._rest_headers())
            elif method == "POST":
                if not isinstance(body, dict) or not body.get("body"):
                    self._send_json(422, {"message": "Validation Failed"},
                                    self._rest_headers())
                    return
                comment = op_add_comment(STORE, repo, issue, body["body"])
                self._send_json(201, comment_view(repo, issue, comment),
                                self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())

    def _handle_pulls(self, method, repo, rest, query, body):
        if rest == []:
            if method == "GET":
                state = query.get("state", ["open"])[0]
                pulls = [p for p in repo.pulls.values()
                         if state == "all" or p["state"] == state]
                pulls.sort(key=lambda p: p["number"])
                page_items, _pp, _pg, _total = paginate(pulls, query, 30)
                self._send_json(200, [pull_view(repo, p) for p in page_items],
                                self._rest_headers())
                return
            if method == "POST":
                if not isinstance(body, dict) or not body.get("title"):
                    self._send_json(422, {"message": "Validation Failed"},
                                    self._rest_headers())
                    return
                try:
                    pull = op_create_pull(STORE, repo, body["title"],
                                          body.get("head", ""), body.get("base", ""),
                                          body.get("body", ""))
                except KeyError:
                    self._send_json(422, {"message": "Validation Failed: No commits between %s and %s" % (
                        body.get("base", ""), body.get("head", ""))},
                        self._rest_headers())
                    return
                self._send_json(201, pull_view(repo, pull), self._rest_headers())
                return
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        try:
            number = int(rest[0])
        except ValueError:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        pull = repo.pulls.get(number)
        if pull is None:
            self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if len(rest) == 1:
            if method == "GET":
                self._send_json(200, pull_view(repo, pull), self._rest_headers())
            else:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
            return
        if rest[1] == "files" and method == "GET":
            head_tip = repo.ref_tip("refs/heads/" + pull["head"])
            base_tip = repo.ref_tip("refs/heads/" + pull["base"])
            if head_tip is None or base_tip is None:
                self._send_json(404, {"message": "Not Found"}, self._rest_headers())
                return
            base_commit = repo.merge_base(head_tip, base_tip)
            base_tree = repo.commit_view(base_commit)["tree"] if base_commit else None
            head_tree = repo.commit_view(head_tip)["tree"]
            files = repo.diff_trees(base_tree, head_tree)
            self._send_json(200, [{"filename": f["filename"], "status": f["status"],
                                   "additions": f["additions"],
                                   "deletions": f["deletions"],
                                   "changes": f["changes"]} for f in files],
                            self._rest_headers())
            return
        if rest[1] == "merge" and method == "PUT":
            if pull["merged"]:
                self._send_json(405, {"message": "Pull request already merged"},
                                self._rest_headers())
                return
            if pull["state"] != "open":
                self._send_json(405, {"message": "Pull request is not open"},
                                self._rest_headers())
                return
            try:
                merged = op_merge_pull(STORE, repo, pull,
                                       (body or {}).get("merge_method", "squash"))
            except RuntimeError:
                self._send_json(405, {"message": "Merge conflict"},
                                self._rest_headers())
                return
            self._send_json(200, {"merged": True, "sha": merged["merge_commit_sha"],
                                  "message": "Pull request successfully merged"},
                            self._rest_headers())
            return
        self._send_json(404, {"message": "Not Found"}, self._rest_headers())


def _label_names(labels):
    names = []
    if isinstance(labels, list):
        for item in labels:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
    return names


def _blob_size(repo, oid):
    found = repo.get_object(oid)
    return len(found[1]) if found and found[0] == "blob" else 0

# ---------------------------------------------------------------------------
# GraphQL — a small parser and executor over the shared store
# ---------------------------------------------------------------------------

class GqlError(Exception):
    pass


def gql_tokenize(src):
    tokens = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == ".":
            if src[i:i + 3] == "...":
                tokens.append(("PUNCT", "..."))
                i += 3
                continue
            raise GqlError("Unexpected character '.'")
        if ch in "{}()[]:!=$@&|":
            tokens.append(("PUNCT", ch))
            i += 1
            continue
        if ch == '"':
            j = i + 1
            out = []
            while j < n:
                c = src[j]
                if c == "\\":
                    nxt = src[j + 1] if j + 1 < n else ""
                    mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"',
                               "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
                    if nxt == "u":
                        out.append(chr(int(src[j + 2:j + 6], 16)))
                        j += 6
                    elif nxt in mapping:
                        out.append(mapping[nxt])
                        j += 2
                    else:
                        out.append(nxt)
                        j += 2
                    continue
                if c == '"':
                    break
                out.append(c)
                j += 1
            tokens.append(("STRING", "".join(out)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and src[i + 1].isdigit()):
            j = i
            if src[j] == "-":
                j += 1
            while j < n and src[j].isdigit():
                j += 1
            is_float = False
            if j < n and src[j] == "." and j + 1 < n and src[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            if j < n and src[j] in "eE":
                is_float = True
                j += 1
                if j < n and src[j] in "+-":
                    j += 1
                while j < n and src[j].isdigit():
                    j += 1
            text = src[i:j]
            tokens.append(("FLOAT" if is_float else "INT",
                           float(text) if is_float else int(text)))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            tokens.append(("NAME", src[i:j]))
            i = j
            continue
        raise GqlError("Unexpected character %r" % ch)
    tokens.append(("EOF", None))
    return tokens


class GqlParser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect_punct(self, value):
        kind, val = self.next()
        if kind != "PUNCT" or val != value:
            raise GqlError("Expected '%s', got %r" % (value, val))

    def expect_name(self):
        kind, val = self.next()
        if kind != "NAME":
            raise GqlError("Expected name, got %r" % (val,))
        return val

    def parse_document(self):
        definitions = []
        while self.peek()[0] != "EOF":
            definitions.append(self.parse_definition())
        return definitions

    def parse_definition(self):
        kind, val = self.peek()
        if kind == "PUNCT" and val == "{":
            return {"kind": "operation", "operation": "query", "name": None,
                    "variables": {}, "selection": self.parse_selection_set()}
        if kind == "NAME" and val in ("query", "mutation", "subscription"):
            self.next()
            operation = val
            name = None
            if self.peek()[0] == "NAME":
                name = self.next()[1]
            variables = {}
            if self.peek() == ("PUNCT", "("):
                variables = self.parse_variable_definitions()
            self._skip_directives()
            return {"kind": "operation", "operation": operation, "name": name,
                    "variables": variables,
                    "selection": self.parse_selection_set()}
        if kind == "NAME" and val == "fragment":
            self.next()
            name = self.expect_name()
            on = self.expect_name()  # "on"
            if on != "on":
                raise GqlError("Expected 'on' in fragment")
            type_condition = self.expect_name()
            self._skip_directives()
            return {"kind": "fragment", "name": name,
                    "type_condition": type_condition,
                    "selection": self.parse_selection_set()}
        raise GqlError("Unexpected definition %r" % (val,))

    def _skip_directives(self):
        while self.peek() == ("PUNCT", "@"):
            self.next()
            self.expect_name()
            if self.peek() == ("PUNCT", "("):
                self.parse_arguments()

    def parse_variable_definitions(self):
        variables = {}
        self.expect_punct("(")
        while self.peek() != ("PUNCT", ")"):
            self.expect_punct("$")
            name = self.expect_name()
            self.expect_punct(":")
            type_name = self.parse_type()
            default = None
            if self.peek() == ("PUNCT", "="):
                self.next()
                default = self.parse_value()
            variables[name] = (type_name, default)
        self.expect_punct(")")
        return variables

    def parse_type(self):
        if self.peek() == ("PUNCT", "["):
            self.next()
            inner = self.parse_type()
            self.expect_punct("]")
            type_name = "[%s]" % inner
        else:
            type_name = self.expect_name()
        if self.peek() == ("PUNCT", "!"):
            self.next()
            type_name += "!"
        return type_name

    def parse_selection_set(self):
        self.expect_punct("{")
        selection = []
        while self.peek() != ("PUNCT", "}"):
            selection.append(self.parse_selection())
        self.expect_punct("}")
        return selection

    def parse_selection(self):
        if self.peek() == ("PUNCT", "..."):
            self.next()
            if self.peek()[0] == "NAME":
                word = self.peek()[1]
                if word == "on":
                    self.next()
                    type_condition = self.expect_name()
                    self._skip_directives()
                    return {"kind": "inline_fragment",
                            "type_condition": type_condition,
                            "selection": self.parse_selection_set()}
                name = self.expect_name()
                self._skip_directives()
                return {"kind": "fragment_spread", "name": name}
            type_condition = None
            self._skip_directives()
            return {"kind": "inline_fragment", "type_condition": type_condition,
                    "selection": self.parse_selection_set()}
        name = self.expect_name()
        alias = None
        if self.peek() == ("PUNCT", ":"):
            self.next()
            alias = name
            name = self.expect_name()
        arguments = {}
        if self.peek() == ("PUNCT", "("):
            arguments = self.parse_arguments()
        self._skip_directives()
        selection = None
        if self.peek() == ("PUNCT", "{"):
            selection = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias,
                "arguments": arguments, "selection": selection}

    def parse_arguments(self):
        arguments = {}
        self.expect_punct("(")
        while self.peek() != ("PUNCT", ")"):
            name = self.expect_name()
            self.expect_punct(":")
            arguments[name] = self.parse_value()
        self.expect_punct(")")
        return arguments

    def parse_value(self):
        kind, val = self.next()
        if kind == "PUNCT" and val == "$":
            return ("var", self.expect_name())
        if kind == "PUNCT" and val == "[":
            items = []
            while self.peek() != ("PUNCT", "]"):
                items.append(self.parse_value())
            self.expect_punct("]")
            return ("list", items)
        if kind == "PUNCT" and val == "{":
            fields = {}
            while self.peek() != ("PUNCT", "}"):
                name = self.expect_name()
                self.expect_punct(":")
                fields[name] = self.parse_value()
            self.expect_punct("}")
            return ("object", fields)
        if kind == "STRING":
            return ("string", val)
        if kind == "INT":
            return ("int", val)
        if kind == "FLOAT":
            return ("float", val)
        if kind == "NAME":
            if val == "true":
                return ("bool", True)
            if val == "false":
                return ("bool", False)
            if val == "null":
                return ("null", None)
            return ("enum", val)
        raise GqlError("Unexpected value %r" % (val,))


def _resolve_value(node, variables):
    kind, val = node
    if kind == "var":
        return variables.get(val)
    if kind == "list":
        return [_resolve_value(item, variables) for item in val]
    if kind == "object":
        return {k: _resolve_value(v, variables) for k, v in val.items()}
    return val


def _execute_selection(obj, typename, selection, ctx):
    out = {}
    for sel in selection:
        if sel["kind"] == "field":
            response_key = sel.get("alias") or sel["name"]
            args = {k: _resolve_value(v, ctx["variables"])
                    for k, v in sel.get("arguments", {}).items()}
            value = _resolve_field(obj, typename, sel["name"], args, ctx)
            if sel.get("selection"):
                value = _execute_subselection(value, sel["selection"], ctx)
            out[response_key] = value
        elif sel["kind"] == "fragment_spread":
            frag = ctx["fragments"].get(sel["name"])
            if frag and frag.get("type_condition") in (None, typename):
                out.update(_execute_selection(obj, typename, frag["selection"], ctx))
        elif sel["kind"] == "inline_fragment":
            if sel.get("type_condition") in (None, typename):
                out.update(_execute_selection(obj, typename, sel["selection"], ctx))
    return out


def _execute_subselection(value, selection, ctx):
    if value is None:
        return None
    if isinstance(value, list):
        return [_execute_subselection(item, selection, ctx) for item in value]
    if isinstance(value, dict):
        typename = value.get("__typename")
        if typename is None:
            return value
        return _execute_selection(value, typename, selection, ctx)
    return value


def _resolve_field(obj, typename, field, args, ctx):
    if field == "__typename":
        return typename
    resolver = RESOLVERS.get((typename, field))
    if resolver is None:
        ctx["errors"].append({"message": 'Cannot query field "%s" on type "%s".' % (field, typename)})
        return None
    try:
        return resolver(obj, args, ctx)
    except Exception as exc:
        ctx["errors"].append({"message": str(exc)})
        return None


def gql_execute(source, variables, operation_name):
    try:
        document = GqlParser(gql_tokenize(source)).parse_document()
    except GqlError as exc:
        return None, [{"message": str(exc)}]
    fragments = {}
    operations = []
    for definition in document:
        if definition["kind"] == "fragment":
            fragments[definition["name"]] = definition
        else:
            operations.append(definition)
    operation = None
    if operation_name:
        for candidate in operations:
            if candidate.get("name") == operation_name:
                operation = candidate
                break
    if operation is None and operations:
        operation = operations[0]
    if operation is None:
        return None, [{"message": "No operation provided"}]
    vars_ = dict(variables or {})
    for name, (type_name, default) in operation.get("variables", {}).items():
        if name not in vars_ or vars_[name] is None and default is not None:
            if default is not None:
                vars_[name] = _resolve_value(default, {})
            elif type_name.endswith("!"):
                return None, [{"message": 'Variable "$%s" of required type "%s" was not provided.' % (name, type_name)}]
    ctx = {"fragments": fragments, "variables": vars_, "errors": []}
    root_type = "Query" if operation["operation"] == "query" else "Mutation"
    root = {"__typename": root_type}
    data = _execute_selection(root, root_type, operation["selection"], ctx)
    return data, (ctx["errors"] or None)

# -- GraphQL schema (for introspection) -----------------------------------
SCHEMA = {
    "Query": {"kind": "OBJECT", "fields": {"viewer": "User", "repository": "Repository", "node": "Node", "__type": "__Type", "__schema": "__Schema"}},
    "Mutation": {"kind": "OBJECT", "fields": {"createIssue": "CreateIssuePayload", "closeIssue": "CloseIssuePayload", "createPullRequest": "CreatePullRequestPayload", "mergePullRequest": "MergePullRequestPayload", "addComment": "AddCommentPayload"}},
    "Repository": {"kind": "OBJECT", "fields": {"id": "ID", "databaseId": "Int", "name": "String", "nameWithOwner": "String", "owner": "User", "defaultBranchRef": "Ref", "viewerPermission": "RepositoryPermission", "hasIssuesEnabled": "Boolean", "issue": "Issue", "issues": "IssueConnection", "pullRequests": "PullRequestConnection", "pullRequest": "PullRequest"}},
    "Issue": {"kind": "OBJECT", "fields": {"id": "ID", "number": "Int", "title": "String", "state": "IssueState", "body": "String"}},
    "IssueConnection": {"kind": "OBJECT", "fields": {"nodes": "[Issue]", "pageInfo": "PageInfo", "edges": "[IssueEdge]", "totalCount": "Int"}},
    "IssueEdge": {"kind": "OBJECT", "fields": {"node": "Issue", "cursor": "String"}},
    "PageInfo": {"kind": "OBJECT", "fields": {"hasNextPage": "Boolean", "hasPreviousPage": "Boolean", "startCursor": "String", "endCursor": "String"}},
    "PullRequest": {"kind": "OBJECT", "fields": {"id": "ID", "number": "Int", "state": "PullRequestState", "merged": "Boolean", "headRefName": "String", "baseRefName": "String", "mergeable": "MergeableState", "files": "PullRequestChangedFileConnection"}},
    "PullRequestConnection": {"kind": "OBJECT", "fields": {"nodes": "[PullRequest]", "pageInfo": "PageInfo", "edges": "[PullRequestEdge]", "totalCount": "Int"}},
    "PullRequestEdge": {"kind": "OBJECT", "fields": {"node": "PullRequest", "cursor": "String"}},
    "PullRequestChangedFile": {"kind": "OBJECT", "fields": {"path": "String"}},
    "PullRequestChangedFileConnection": {"kind": "OBJECT", "fields": {"nodes": "[PullRequestChangedFile]", "pageInfo": "PageInfo", "edges": "[PullRequestChangedFileEdge]", "totalCount": "Int"}},
    "PullRequestChangedFileEdge": {"kind": "OBJECT", "fields": {"node": "PullRequestChangedFile", "cursor": "String"}},
    "User": {"kind": "OBJECT", "fields": {"login": "String", "id": "ID"}},
    "Ref": {"kind": "OBJECT", "fields": {"name": "String"}},
    "Node": {"kind": "INTERFACE", "fields": {"id": "ID"}},
    "IssueState": {"kind": "ENUM", "values": ["OPEN", "CLOSED"]},
    "MergeableState": {"kind": "ENUM", "values": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "PullRequestState": {"kind": "ENUM", "values": ["OPEN", "CLOSED", "MERGED"]},
    "PullRequestMergeMethod": {"kind": "ENUM", "values": ["MERGE", "SQUASH", "REBASE"]},
    "RepositoryPermission": {"kind": "ENUM", "values": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"]},
    "SearchType": {"kind": "ENUM", "values": ["ISSUE", "PULL_REQUEST", "REPOSITORY", "USER", "DISCUSSION"]},
    "IssueOrderField": {"kind": "ENUM", "values": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "OrderDirection": {"kind": "ENUM", "values": ["ASC", "DESC"]},
    "CreateIssuePayload": {"kind": "OBJECT", "fields": {"issue": "Issue", "clientMutationId": "String"}},
    "CloseIssuePayload": {"kind": "OBJECT", "fields": {"issue": "Issue", "clientMutationId": "String"}},
    "CreatePullRequestPayload": {"kind": "OBJECT", "fields": {"pullRequest": "PullRequest", "clientMutationId": "String"}},
    "MergePullRequestPayload": {"kind": "OBJECT", "fields": {"pullRequest": "PullRequest", "clientMutationId": "String"}},
    "AddCommentPayload": {"kind": "OBJECT", "fields": {"commentEdge": "IssueCommentEdge", "subject": "Issue", "clientMutationId": "String"}},
    "IssueCommentEdge": {"kind": "OBJECT", "fields": {"node": "IssueComment", "cursor": "String"}},
    "IssueComment": {"kind": "OBJECT", "fields": {"id": "ID", "body": "String"}},
    "__Type": {"kind": "OBJECT", "fields": {"name": "String", "kind": "String", "fields": "[__Field]", "enumValues": "[__EnumValue]"}},
    "__Field": {"kind": "OBJECT", "fields": {"name": "String", "type": "__Type"}},
    "__EnumValue": {"kind": "OBJECT", "fields": {"name": "String"}},
    "__Schema": {"kind": "OBJECT", "fields": {"queryType": "__Type", "mutationType": "__Type", "types": "[__Type]"}},
    "ID": {"kind": "SCALAR"}, "String": {"kind": "SCALAR"},
    "Int": {"kind": "SCALAR"}, "Boolean": {"kind": "SCALAR"},
}


# -- GraphQL object constructors ------------------------------------------
def gql_user(user):
    return {"__typename": "User", "_user": user}


def gql_repo(repo):
    return {"__typename": "Repository", "_repo": repo}


def gql_issue(repo, issue):
    return {"__typename": "Issue", "_repo": repo, "_issue": issue}


def gql_pull(repo, pull):
    return {"__typename": "PullRequest", "_repo": repo, "_pull": pull}


def gql_comment(repo, issue, comment):
    return {"__typename": "IssueComment", "_repo": repo, "_issue": issue,
            "_comment": comment}


def gql_type(name):
    return {"__typename": "__Type", "_name": name}


def _cursor(index):
    return base64.urlsafe_b64encode(("cursor:%d" % index).encode()).decode()


def _uncursor(cursor):
    if not isinstance(cursor, str):
        return -1
    try:
        decoded = base64.urlsafe_b64decode(cursor + "==").decode()
        if decoded.startswith("cursor:"):
            return int(decoded[len("cursor:"):])
    except Exception:
        return -1
    return -1


def _connection(items, first, after, edge_type, node_type):
    start = _uncursor(after) + 1 if after else 0
    sliced = items[start:]
    if first is not None:
        page = sliced[:first]
        has_next = len(sliced) > first
    else:
        page = sliced
        has_next = False
    end_cursor = _cursor(start + len(page) - 1) if page else None
    start_cursor = _cursor(start) if page else None
    edges = [{"__typename": edge_type, "node": item,
              "cursor": _cursor(start + i)} for i, item in enumerate(page)]
    return {"__typename": node_type, "_nodes": page, "_total": len(items),
            "_page_info": {
        "__typename": "PageInfo", "_hasNextPage": has_next,
        "_hasPreviousPage": start > 0, "_startCursor": start_cursor,
        "_endCursor": end_cursor}, "_edges": edges}


def _ordered_issues(repo, states=None, order_by=None):
    issues = list(repo.issues.values())
    if states:
        wanted = {"open" if s == "OPEN" else "closed" for s in states}
        issues = [i for i in issues if i["state"] in wanted]
    direction = "DESC"
    if isinstance(order_by, dict):
        direction = order_by.get("direction", "DESC")
    issues.sort(key=lambda i: i["seq"], reverse=(direction != "ASC"))
    return issues


def _ordered_pulls(repo):
    return sorted(repo.pulls.values(), key=lambda p: p["number"])


def _find_by_global_id(gid):
    try:
        kind, parts = parse_global_id(gid)
    except Exception:
        return None
    if kind == "User":
        for user in USERS:
            if str(user["id"]) == parts[0]:
                return gql_user(user)
        return None
    if kind == "Repository":
        for repo in STORE.repos.values():
            if str(repo.id) == parts[0]:
                return gql_repo(repo)
        return None
    if kind == "Issue":
        for repo in STORE.repos.values():
            for issue in repo.issues.values():
                if str(issue["id"]) == parts[0]:
                    return gql_issue(repo, issue)
        return None
    if kind == "PullRequest":
        for repo in STORE.repos.values():
            for pull in repo.pulls.values():
                if str(pull["id"]) == parts[0]:
                    return gql_pull(repo, pull)
        return None
    return None


def _repo_from_gid(gid):
    try:
        kind, parts = parse_global_id(gid)
    except Exception:
        raise ValueError("Invalid global id")
    if kind != "Repository":
        raise ValueError("Expected a Repository id")
    for repo in STORE.repos.values():
        if str(repo.id) == parts[0]:
            return repo
    raise ValueError("Repository not found")


def _issue_from_gid(gid):
    try:
        kind, parts = parse_global_id(gid)
    except Exception:
        raise ValueError("Invalid global id")
    if kind != "Issue":
        raise ValueError("Expected an Issue id")
    for repo in STORE.repos.values():
        for issue in repo.issues.values():
            if str(issue["id"]) == parts[0]:
                return repo, issue
    raise ValueError("Issue not found")


def _pull_from_gid(gid):
    try:
        kind, parts = parse_global_id(gid)
    except Exception:
        raise ValueError("Invalid global id")
    if kind != "PullRequest":
        raise ValueError("Expected a PullRequest id")
    for repo in STORE.repos.values():
        for pull in repo.pulls.values():
            if str(pull["id"]) == parts[0]:
                return repo, pull
    raise ValueError("PullRequest not found")


RESOLVERS = {}


def _r(typename, field):
    def deco(func):
        RESOLVERS[(typename, field)] = func
        return func
    return deco


@_r("Query", "viewer")
def _q_viewer(obj, args, ctx):
    return gql_user(VIEWER)


@_r("Query", "repository")
def _q_repository(obj, args, ctx):
    repo = STORE.get_repo(args.get("owner", ""), args.get("name", ""))
    return gql_repo(repo) if repo else None


@_r("Query", "node")
def _q_node(obj, args, ctx):
    return _find_by_global_id(args.get("id", ""))


@_r("Query", "__type")
def _q_type(obj, args, ctx):
    name = args.get("name", "")
    return gql_type(name) if name in SCHEMA else None


@_r("Query", "__schema")
def _q_schema(obj, args, ctx):
    return {"__typename": "__Schema"}


@_r("User", "login")
def _u_login(obj, args, ctx):
    return obj["_user"]["login"]


@_r("User", "id")
def _u_id(obj, args, ctx):
    return global_id("User", obj["_user"]["id"])


@_r("Repository", "id")
def _repo_id(obj, args, ctx):
    return global_id("Repository", obj["_repo"].id)


@_r("Repository", "databaseId")
def _repo_dbid(obj, args, ctx):
    return obj["_repo"].id


@_r("Repository", "name")
def _repo_name(obj, args, ctx):
    return obj["_repo"].name


@_r("Repository", "nameWithOwner")
def _repo_nwo(obj, args, ctx):
    repo = obj["_repo"]
    return "%s/%s" % (repo.owner, repo.name)


@_r("Repository", "owner")
def _repo_owner(obj, args, ctx):
    return gql_user(_user_by_login(obj["_repo"].owner))


@_r("Repository", "defaultBranchRef")
def _repo_default_branch(obj, args, ctx):
    repo = obj["_repo"]
    tip = repo.ref_tip("refs/heads/" + repo.default_branch)
    if tip is None:
        return None
    return {"__typename": "Ref", "name": repo.default_branch}


@_r("Repository", "viewerPermission")
def _repo_perm(obj, args, ctx):
    return "ADMIN"


@_r("Repository", "hasIssuesEnabled")
def _repo_has_issues(obj, args, ctx):
    return True


@_r("Repository", "issue")
def _repo_issue(obj, args, ctx):
    repo = obj["_repo"]
    issue = repo.issues.get(args.get("number"))
    return gql_issue(repo, issue) if issue else None


@_r("Repository", "issues")
def _repo_issues(obj, args, ctx):
    repo = obj["_repo"]
    items = _ordered_issues(repo, args.get("states"), args.get("orderBy"))
    nodes = [gql_issue(repo, i) for i in items]
    return _connection(nodes, args.get("first"), args.get("after"),
                       "IssueEdge", "IssueConnection")


@_r("Repository", "pullRequests")
def _repo_pulls(obj, args, ctx):
    repo = obj["_repo"]
    nodes = [gql_pull(repo, p) for p in _ordered_pulls(repo)]
    return _connection(nodes, args.get("first"), args.get("after"),
                       "PullRequestEdge", "PullRequestConnection")


@_r("Repository", "pullRequest")
def _repo_pull(obj, args, ctx):
    repo = obj["_repo"]
    pull = repo.pulls.get(args.get("number"))
    return gql_pull(repo, pull) if pull else None


@_r("Issue", "id")
def _issue_id(obj, args, ctx):
    return global_id("Issue", obj["_issue"]["id"])


@_r("Issue", "number")
def _issue_number(obj, args, ctx):
    return obj["_issue"]["number"]


@_r("Issue", "title")
def _issue_title(obj, args, ctx):
    return obj["_issue"]["title"]


@_r("Issue", "state")
def _issue_state(obj, args, ctx):
    return "OPEN" if obj["_issue"]["state"] == "open" else "CLOSED"


@_r("Issue", "body")
def _issue_body(obj, args, ctx):
    return obj["_issue"].get("body", "")


@_r("IssueConnection", "nodes")
def _conn_nodes(obj, args, ctx):
    return obj["_nodes"]


@_r("IssueConnection", "pageInfo")
def _conn_page_info(obj, args, ctx):
    return obj["_page_info"]


@_r("IssueConnection", "edges")
def _conn_edges(obj, args, ctx):
    return obj["_edges"]


@_r("IssueConnection", "totalCount")
def _conn_total(obj, args, ctx):
    return obj["_total"]


@_r("IssueEdge", "node")
def _edge_node(obj, args, ctx):
    return obj["node"]


@_r("IssueEdge", "cursor")
def _edge_cursor(obj, args, ctx):
    return obj["cursor"]


@_r("PageInfo", "hasNextPage")
def _pi_next(obj, args, ctx):
    return obj["_hasNextPage"]


@_r("PageInfo", "hasPreviousPage")
def _pi_prev(obj, args, ctx):
    return obj["_hasPreviousPage"]


@_r("PageInfo", "startCursor")
def _pi_start(obj, args, ctx):
    return obj["_startCursor"]


@_r("PageInfo", "endCursor")
def _pi_end(obj, args, ctx):
    return obj["_endCursor"]


@_r("PullRequest", "id")
def _pr_id(obj, args, ctx):
    return global_id("PullRequest", obj["_pull"]["id"])


@_r("PullRequest", "number")
def _pr_number(obj, args, ctx):
    return obj["_pull"]["number"]


@_r("PullRequest", "state")
def _pr_state(obj, args, ctx):
    pull = obj["_pull"]
    if pull["merged"]:
        return "MERGED"
    return "OPEN" if pull["state"] == "open" else "CLOSED"


@_r("PullRequest", "merged")
def _pr_merged(obj, args, ctx):
    return bool(obj["_pull"]["merged"])


@_r("PullRequest", "headRefName")
def _pr_head(obj, args, ctx):
    return obj["_pull"]["head"]


@_r("PullRequest", "baseRefName")
def _pr_base(obj, args, ctx):
    return obj["_pull"]["base"]


@_r("PullRequest", "mergeable")
def _pr_mergeable(obj, args, ctx):
    repo, pull = obj["_repo"], obj["_pull"]
    return STORE.pr_mergeable(repo, pull)


@_r("PullRequest", "files")
def _pr_files(obj, args, ctx):
    repo, pull = obj["_repo"], obj["_pull"]
    head_tip = repo.ref_tip("refs/heads/" + pull["head"])
    base_tip = repo.ref_tip("refs/heads/" + pull["base"])
    if head_tip is None or base_tip is None:
        files = []
    else:
        base_commit = repo.merge_base(head_tip, base_tip)
        base_tree = repo.commit_view(base_commit)["tree"] if base_commit else None
        head_tree = repo.commit_view(head_tip)["tree"]
        files = [{"__typename": "PullRequestChangedFile", "path": f["filename"]}
                 for f in repo.diff_trees(base_tree, head_tree)]
    return _connection(files, args.get("first"), args.get("after"),
                       "PullRequestChangedFileEdge", "PullRequestChangedFileConnection")


@_r("PullRequestConnection", "nodes")
def _prc_nodes(obj, args, ctx):
    return obj["_nodes"]


@_r("PullRequestConnection", "pageInfo")
def _prc_page_info(obj, args, ctx):
    return obj["_page_info"]


@_r("PullRequestConnection", "edges")
def _prc_edges(obj, args, ctx):
    return obj["_edges"]


@_r("PullRequestConnection", "totalCount")
def _prc_total(obj, args, ctx):
    return obj["_total"]


@_r("PullRequestEdge", "node")
def _pre_node(obj, args, ctx):
    return obj["node"]


@_r("PullRequestEdge", "cursor")
def _pre_cursor(obj, args, ctx):
    return obj["cursor"]


@_r("PullRequestChangedFile", "path")
def _pcf_path(obj, args, ctx):
    return obj["path"]


@_r("PullRequestChangedFileConnection", "nodes")
def _pcfc_nodes(obj, args, ctx):
    return obj["_nodes"]


@_r("PullRequestChangedFileConnection", "pageInfo")
def _pcfc_page_info(obj, args, ctx):
    return obj["_page_info"]


@_r("PullRequestChangedFileConnection", "edges")
def _pcfc_edges(obj, args, ctx):
    return obj["_edges"]


@_r("PullRequestChangedFileConnection", "totalCount")
def _pcfc_total(obj, args, ctx):
    return obj["_total"]


@_r("PullRequestChangedFileEdge", "node")
def _pcfe_node(obj, args, ctx):
    return obj["node"]


@_r("PullRequestChangedFileEdge", "cursor")
def _pcfe_cursor(obj, args, ctx):
    return obj["cursor"]


@_r("Ref", "name")
def _ref_name(obj, args, ctx):
    return obj["name"]


@_r("IssueComment", "id")
def _comment_id(obj, args, ctx):
    return global_id("IssueComment", obj["_comment"]["id"])


@_r("IssueComment", "body")
def _comment_body(obj, args, ctx):
    return obj["_comment"]["body"]


@_r("IssueCommentEdge", "node")
def _ice_node(obj, args, ctx):
    return obj["node"]


@_r("IssueCommentEdge", "cursor")
def _ice_cursor(obj, args, ctx):
    return obj["cursor"]


@_r("__Type", "name")
def _t_name(obj, args, ctx):
    return obj["_name"]


@_r("__Type", "kind")
def _t_kind(obj, args, ctx):
    return SCHEMA.get(obj["_name"], {}).get("kind", "OBJECT")


@_r("__Type", "fields")
def _t_fields(obj, args, ctx):
    info = SCHEMA.get(obj["_name"], {})
    if info.get("kind") not in ("OBJECT", "INTERFACE"):
        return None
    return [{"__typename": "__Field", "_field": name, "_type": type_name}
            for name, type_name in info.get("fields", {}).items()]


@_r("__Type", "enumValues")
def _t_enum_values(obj, args, ctx):
    info = SCHEMA.get(obj["_name"], {})
    if info.get("kind") != "ENUM":
        return None
    return [{"__typename": "__EnumValue", "name": value}
            for value in info.get("values", [])]


@_r("__Field", "name")
def _f_name(obj, args, ctx):
    return obj["_field"]


@_r("__Field", "type")
def _f_type(obj, args, ctx):
    return gql_type(obj["_type"])


@_r("__EnumValue", "name")
def _ev_name(obj, args, ctx):
    return obj["name"]


@_r("__Schema", "queryType")
def _s_query(obj, args, ctx):
    return gql_type("Query")


@_r("__Schema", "mutationType")
def _s_mutation(obj, args, ctx):
    return gql_type("Mutation")


@_r("__Schema", "types")
def _s_types(obj, args, ctx):
    return [gql_type(name) for name in SCHEMA]


@_r("Mutation", "createIssue")
def _m_create_issue(obj, args, ctx):
    data = args.get("input") or {}
    repo = _repo_from_gid(data.get("repositoryId", ""))
    issue = op_create_issue(STORE, repo, data.get("title", ""),
                            data.get("body", ""), [])
    return {"__typename": "CreateIssuePayload", "issue": gql_issue(repo, issue),
            "clientMutationId": data.get("clientMutationId")}


@_r("Mutation", "closeIssue")
def _m_close_issue(obj, args, ctx):
    data = args.get("input") or {}
    repo, issue = _issue_from_gid(data.get("issueId", ""))
    issue["state"] = "closed"
    issue["updated_at"] = iso_now(STORE.next_seq())
    return {"__typename": "CloseIssuePayload", "issue": gql_issue(repo, issue),
            "clientMutationId": data.get("clientMutationId")}


@_r("Mutation", "createPullRequest")
def _m_create_pull(obj, args, ctx):
    data = args.get("input") or {}
    repo = _repo_from_gid(data.get("repositoryId", ""))
    pull = op_create_pull(STORE, repo, data.get("title", ""),
                          data.get("headRefName", ""), data.get("baseRefName", ""),
                          data.get("body", ""))
    return {"__typename": "CreatePullRequestPayload",
            "pullRequest": gql_pull(repo, pull),
            "clientMutationId": data.get("clientMutationId")}


@_r("Mutation", "mergePullRequest")
def _m_merge_pull(obj, args, ctx):
    data = args.get("input") or {}
    repo, pull = _pull_from_gid(data.get("pullRequestId", ""))
    merged = op_merge_pull(STORE, repo, pull, data.get("mergeMethod", "SQUASH"))
    return {"__typename": "MergePullRequestPayload",
            "pullRequest": gql_pull(repo, merged),
            "clientMutationId": data.get("clientMutationId")}


@_r("Mutation", "addComment")
def _m_add_comment(obj, args, ctx):
    data = args.get("input") or {}
    repo, issue = _issue_from_gid(data.get("subjectId", ""))
    comment = op_add_comment(STORE, repo, issue, data.get("body", ""))
    return {"__typename": "AddCommentPayload",
            "commentEdge": {"__typename": "IssueCommentEdge",
                            "node": gql_comment(repo, issue, comment),
                            "cursor": _cursor(0)},
            "subject": gql_issue(repo, issue),
            "clientMutationId": data.get("clientMutationId")}


@_r("CreateIssuePayload", "issue")
def _cip_issue(obj, args, ctx):
    return obj["issue"]


@_r("CreateIssuePayload", "clientMutationId")
def _cip_cmid(obj, args, ctx):
    return obj.get("clientMutationId")


@_r("CloseIssuePayload", "issue")
def _clp_issue(obj, args, ctx):
    return obj["issue"]


@_r("CloseIssuePayload", "clientMutationId")
def _clp_cmid(obj, args, ctx):
    return obj.get("clientMutationId")


@_r("CreatePullRequestPayload", "pullRequest")
def _cpp_pr(obj, args, ctx):
    return obj["pullRequest"]


@_r("CreatePullRequestPayload", "clientMutationId")
def _cpp_cmid(obj, args, ctx):
    return obj.get("clientMutationId")


@_r("MergePullRequestPayload", "pullRequest")
def _mpp_pr(obj, args, ctx):
    return obj["pullRequest"]


@_r("MergePullRequestPayload", "clientMutationId")
def _mpp_cmid(obj, args, ctx):
    return obj.get("clientMutationId")


@_r("AddCommentPayload", "commentEdge")
def _acp_edge(obj, args, ctx):
    return obj["commentEdge"]


@_r("AddCommentPayload", "subject")
def _acp_subject(obj, args, ctx):
    return obj["subject"]


@_r("AddCommentPayload", "clientMutationId")
def _acp_cmid(obj, args, ctx):
    return obj.get("clientMutationId")


# -- GraphQL request handler ----------------------------------------------
def _handle_graphql(self, raw):
    if not self._authenticated():
        self._send_json(401, {"message": "Requires authentication"})
        return
    body, err = self._parse_json(raw)
    if err:
        self._send_json(400, {"errors": [{"message": err}]})
        return

    def run_one(request):
        if not isinstance(request, dict) or not isinstance(request.get("query"), str):
            return {"errors": [{"message": "Request must include a query string"}]}
        with STORE.lock:
            data, errors = gql_execute(request["query"],
                                       request.get("variables") or {},
                                       request.get("operationName"))
        response = {"data": data}
        if errors:
            response["errors"] = errors
        return response

    if isinstance(body, list):
        self._send_json(200, [run_one(item) for item in body])
    else:
        self._send_json(200, run_one(body))


ForgeHandler._handle_graphql = _handle_graphql

# ---------------------------------------------------------------------------
# MCP — streamable HTTP, modern 2026-07-28 revision with a legacy fallback
# ---------------------------------------------------------------------------

MCP_SUPPORTED = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
                 "2024-11-05", "2024-10-07"]
MCP_LATEST = "2026-07-28"


def mcp_tools_list():
    return [
        {"name": "create_repo",
         "description": "Create a repository owned by the authenticated user.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "name": {"type": "string", "description": "Repository name"},
                             "description": {"type": "string", "description": "Repository description"},
                             "private": {"type": "boolean", "description": "Whether the repository is private"},
                             "owner": {"type": "string", "description": "Owner login (defaults to the authenticated user)"}},
                         "required": ["name"]}},
        {"name": "get_repo",
         "description": "Get a repository by owner and name.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"}},
                         "required": ["owner", "repo"]}},
        {"name": "create_issue",
         "description": "Create an issue in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "title": {"type": "string", "description": "Issue title"},
                             "body": {"type": "string", "description": "Issue body"}},
                         "required": ["owner", "repo", "title"]}},
        {"name": "list_issues",
         "description": "List issues in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "state": {"type": "string", "description": "Issue state: open, closed or all"}},
                         "required": ["owner", "repo"]}},
        {"name": "create_pull_request",
         "description": "Create a pull request in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "title": {"type": "string", "description": "Pull request title"},
                             "body": {"type": "string", "description": "Pull request body"},
                             "head": {"type": "string", "description": "Head branch name"},
                             "base": {"type": "string", "description": "Base branch name"}},
                         "required": ["owner", "repo", "title", "head", "base"]}},
        {"name": "merge_pull_request",
         "description": "Merge a pull request in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "pull_number": {"type": "integer", "description": "Pull request number"}},
                         "required": ["owner", "repo", "pull_number"]}},
        {"name": "get_issue",
         "description": "Get one issue by number.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "issue_number": {"type": "integer", "description": "Issue number"}},
                         "required": ["owner", "repo", "issue_number"]}},
        {"name": "list_pull_requests",
         "description": "List pull requests in a repository.",
         "inputSchema": {"type": "object",
                         "properties": {
                             "owner": {"type": "string", "description": "Repository owner login"},
                             "repo": {"type": "string", "description": "Repository name"},
                             "state": {"type": "string", "description": "Pull request state: open, closed or all"}},
                         "required": ["owner", "repo"]}},
    ]


def _mcp_result(content, structured):
    return {"content": [{"type": "text", "text": content}],
            "structuredContent": structured}


def _mcp_call_tool(name, args):
    """Run one tool. Returns (result_dict, error_dict)."""
    args = args if isinstance(args, dict) else {}
    if name == "create_repo":
        if not args.get("name"):
            return None, {"code": -32602, "message": "Missing required argument: name"}
        try:
            repo = op_create_repo(STORE, args.get("owner") or VIEWER["login"],
                                  args["name"], args.get("private", False),
                                  args.get("description", ""))
        except ValueError as exc:
            return None, {"code": -32602, "message": str(exc)}
        except KeyError:
            return None, {"code": -32602, "message": "name already exists on this account"}
        view = repo_view(repo)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"repository": view, "id": repo.id,
                            "name": repo.name, "full_name": repo.name and "%s/%s" % (repo.owner, repo.name)}), None
    if name == "get_repo":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        view = repo_view(repo)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"repository": view, "id": repo.id, "name": repo.name}), None
    if name == "create_issue":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        if not args.get("title"):
            return None, {"code": -32602, "message": "Missing required argument: title"}
        issue = op_create_issue(STORE, repo, args["title"], args.get("body", ""), [])
        view = issue_view(repo, issue)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"issue": view, "id": issue["id"], "number": issue["number"]}), None
    if name == "list_issues":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        state = args.get("state", "open")
        issues = [i for i in repo.issues.values()
                  if state == "all" or i["state"] == state]
        issues.sort(key=lambda i: i["number"])
        views = [issue_view(repo, i) for i in issues]
        return _mcp_result(json.dumps(views, separators=(",", ":")),
                           {"issues": views}), None
    if name == "create_pull_request":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        for field in ("title", "head", "base"):
            if not args.get(field):
                return None, {"code": -32602, "message": "Missing required argument: %s" % field}
        try:
            pull = op_create_pull(STORE, repo, args["title"], args["head"],
                                  args["base"], args.get("body", ""))
        except KeyError:
            return None, {"code": -32602, "message": "head or base branch not found"}
        view = pull_view(repo, pull)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"pull_request": view, "id": pull["id"],
                            "number": pull["number"]}), None
    if name == "merge_pull_request":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        number = args.get("pull_number")
        if not isinstance(number, int):
            return None, {"code": -32602, "message": "Missing required argument: pull_number"}
        pull = repo.pulls.get(number)
        if pull is None:
            return None, {"code": -32602, "message": "Pull request not found"}
        try:
            merged = op_merge_pull(STORE, repo, pull, "squash")
        except RuntimeError:
            return None, {"code": -32602, "message": "Merge conflict"}
        view = pull_view(repo, merged)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"pull_request": view, "merged": True,
                            "sha": merged["merge_commit_sha"]}), None
    if name == "get_issue":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        issue = repo.issues.get(args.get("issue_number"))
        if issue is None:
            return None, {"code": -32602, "message": "Issue not found"}
        view = issue_view(repo, issue)
        return _mcp_result(json.dumps(view, separators=(",", ":")),
                           {"issue": view}), None
    if name == "list_pull_requests":
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return None, {"code": -32602, "message": "Repository not found"}
        state = args.get("state", "open")
        pulls = [p for p in repo.pulls.values()
                 if state == "all" or p["state"] == state]
        pulls.sort(key=lambda p: p["number"])
        views = [pull_view(repo, p) for p in pulls]
        return _mcp_result(json.dumps(views, separators=(",", ":")),
                           {"pull_requests": views}), None
    return None, {"code": -32601, "message": "Unknown tool: %s" % name}


def _handle_mcp(self, raw):
    if not self._authenticated():
        self._send_json(401, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32001, "message": "Unauthorized"}})
        return
    body, err = self._parse_json(raw)
    if err:
        self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700,
                                              "message": "Parse error"}}).encode(),
                   "application/json")
        return
    if isinstance(body, list):
        self._send_json(400, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid Request"}})
        return
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        self._send_json(400, {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid Request"}})
        return

    request_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}
    is_notification = "id" not in body

    def reply(payload):
        if is_notification:
            self._send_json(200, {})
            return
        self._send_json(200, payload)

    def result(value):
        reply({"jsonrpc": "2.0", "id": request_id, "result": value})

    def error(code, message):
        reply({"jsonrpc": "2.0", "id": request_id,
               "error": {"code": code, "message": message}})

    if method == "server/discover":
        result({"serverInfo": {"name": FORGE_NAME, "version": FORGE_VERSION},
                "capabilities": {"tools": {}},
                "supportedVersions": list(MCP_SUPPORTED),
                "instructions": "A GitHub-compatible git forge: repositories, issues, pull requests, git smart HTTP, REST v3, GraphQL and MCP tools.",
                "_meta": {"serverInfo": {"name": FORGE_NAME, "version": FORGE_VERSION}}})
        return
    if method == "initialize":
        offered = params.get("protocolVersion", "")
        if offered not in MCP_SUPPORTED:
            error(-32602, "Unsupported protocol version: %s" % offered)
            return
        result({"protocolVersion": offered,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": FORGE_NAME, "version": FORGE_VERSION}})
        return
    if method == "notifications/initialized":
        reply({})
        return
    if method == "ping":
        result({})
        return
    if method == "tools/list":
        result({"tools": mcp_tools_list()})
        return
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        known = {tool["name"] for tool in mcp_tools_list()}
        if name not in known:
            error(-32602, "Unknown tool: %s" % name)
            return
        tool_result, tool_error = _mcp_call_tool(name, args)
        if tool_error is not None:
            error(tool_error["code"], tool_error["message"])
            return
        result(tool_result)
        return
    error(-32601, "Method not found: %s" % method)


ForgeHandler._handle_mcp = _handle_mcp

# ---------------------------------------------------------------------------
# git smart HTTP
# ---------------------------------------------------------------------------

UPLOAD_CAPS_V0 = ("side-band-64k multi_ack_detailed thin-pack ofs-delta shallow "
                  "agent=%s symref=HEAD:refs/heads/main" % AGENT_NAME)
RECEIVE_CAPS_V0 = ("report-status side-band-64k delete-refs ofs-delta "
                   "agent=%s symref=HEAD:refs/heads/main" % AGENT_NAME)


def _ref_advertisement(repo, caps):
    """v0 advertisement: HEAD first, then refs, then peeled tags."""
    lines = []
    refs = sorted(repo.refs.items())
    default_tip = repo.ref_tip("refs/heads/" + repo.default_branch)
    if refs:
        head_oid = default_tip or refs[0][1]
        lines.append("%s HEAD\x00%s\n" % (head_oid, caps))
        for refname, oid in refs:
            lines.append("%s %s\n" % (oid, refname))
            found = repo.get_object(oid)
            if found and found[0] == "tag":
                tag = parse_tag(found[1])
                lines.append("%s %s^{}\n" % (tag["object"], refname))
    else:
        lines.append("%s capabilities^{}\x00%s\n" % (ZERO_OID, caps))
    return lines


def _handle_git(self, method, owner, repo_name, rest, query, raw):
    if not self._authenticated():
        self._send_json(401, {"message": "Requires authentication"})
        return
    repo = STORE.get_repo(owner, repo_name)
    if repo is None:
        self._send_json(404, {"message": "Not Found"})
        return
    service = query.get("service", [None])[0]

    if rest == "/info/refs" and method == "GET":
        if service == "git-upload-pack":
            use_v2 = self.headers.get("Git-Protocol", "") == "version=2"
            body = pkt("# service=git-upload-pack\n") + b"0000"
            if use_v2:
                body += (pkt("version 2\n")
                         + pkt("agent=%s\n" % AGENT_NAME)
                         + pkt("ls-refs=unborn\n")
                         + pkt("fetch=shallow filter\n")
                         + pkt("object-format=sha1\n")
                         + b"0000")
            else:
                body += b"".join(pkt(line) for line in _ref_advertisement(repo, UPLOAD_CAPS_V0)) + b"0000"
            self._send(200, body, "application/x-git-upload-pack-advertisement")
            return
        if service == "git-receive-pack":
            body = pkt("# service=git-receive-pack\n") + b"0000"
            body += b"".join(pkt(line) for line in _ref_advertisement(repo, RECEIVE_CAPS_V0)) + b"0000"
            self._send(200, body, "application/x-git-receive-pack-advertisement")
            return
        self._send_error(404, "Not Found")
        return

    if rest == "/git-upload-pack" and method == "POST":
        self._git_upload_pack(repo, raw)
        return

    if rest == "/git-receive-pack" and method == "POST":
        self._git_receive_pack(repo, raw)
        return

    self._send_json(404, {"message": "Not Found"})


def _read_pkt_sections(raw):
    """Parse a pkt-line request body into sections of str lines plus the
    trailing raw bytes. Sections are split by delim/flush; parsing stops
    after the first flush when the remainder is a pack (receive-pack) or
    empty, and continues past it for a second section (v0 fetch)."""
    reader = PktReader(raw)
    sections = []
    current = []
    while True:
        kind, payload = reader.next()
        if kind is None:
            if current:
                sections.append(current)
            return sections, b""
        if kind == "data":
            # protocol lines end with a newline; strip it so command and
            # argument comparisons see the bare token.
            current.append(payload.decode("utf-8", "replace").rstrip("\r\n"))
        elif kind == "delim":
            sections.append(current)
            current = []
        elif kind == "flush":
            sections.append(current)
            current = []
            rest = reader.rest()
            if not rest:
                return sections, b""
            if rest.startswith(b"PACK"):
                return sections, rest
        elif kind == "end":
            sections.append(current)
            current = []
            return sections, reader.rest()


def _parse_ls_refs(raw):
    sections, _rest = _read_pkt_sections(raw)
    if not sections or not sections[0] or sections[0][0] != "command=ls-refs":
        raise GitError("expected command=ls-refs")
    first = sections[0]
    args = {}
    for line in first[1:]:
        if "=" in line:
            key, value = line.split("=", 1)
            args[key] = value
        else:
            args[line] = True
    prefixes = []
    peel = False
    symrefs = False
    unborn = False
    if len(sections) > 1:
        for line in sections[1]:
            if line.startswith("ref-prefix "):
                prefixes.append(line[len("ref-prefix "):])
            elif line == "peel":
                peel = True
            elif line == "symrefs":
                symrefs = True
            elif line == "unborn":
                unborn = True
    return prefixes, peel, symrefs, unborn


def _parse_fetch(raw):
    sections, _rest = _read_pkt_sections(raw)
    if not sections or not sections[0] or sections[0][0] != "command=fetch":
        raise GitError("expected command=fetch")
    # The want/have/shallow/deepen arguments follow the delim, in the
    # second section; the first section holds only the command + capabilities.
    arg_lines = sections[1] if len(sections) > 1 else []
    request = {"wants": [], "haves": [], "shallow": [], "deepen": None,
               "thin-pack": False, "no-progress": False, "ofs-delta": False}
    for line in arg_lines:
        if line.startswith("want "):
            request["wants"].append(line[5:].strip())
        elif line.startswith("have "):
            request["haves"].append(line[5:].strip())
        elif line.startswith("shallow "):
            request["shallow"].append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                request["deepen"] = int(line[7:].strip())
            except ValueError:
                pass
        elif line == "thin-pack":
            request["thin-pack"] = True
        elif line == "no-progress":
            request["no-progress"] = True
        elif line == "ofs-delta":
            request["ofs-delta"] = True
    return request


def _parse_fetch_v0(raw):
    sections, _rest = _read_pkt_sections(raw)
    if len(sections) < 2 or not sections[0] or not sections[1]:
        raise GitError("malformed fetch request")
    request = {"wants": [], "haves": [], "shallow": [], "deepen": None,
               "thin-pack": False, "no-progress": False, "ofs-delta": False}
    for line in sections[0]:
        if line.startswith("want "):
            oid = line[5:].split(" ", 1)[0]
            request["wants"].append(oid)
        elif line.startswith("shallow "):
            request["shallow"].append(line[8:].strip())
        elif line.startswith("deepen "):
            try:
                request["deepen"] = int(line[7:].strip())
            except ValueError:
                pass
    for line in sections[1]:
        if line.startswith("have "):
            request["haves"].append(line[5:].strip())
    return request


def _fetch_objects(repo, request):
    """Compute the pack payload for a fetch: (objects, shallow, unshallow).

    Walks the wanted commits with first-parent generations so a depth limit
    cuts the walk, and subtracts everything the client already has. The
    object set is the depth-limited commits plus their tree closures (and
    any wanted tag objects), never the parents beyond the depth cut."""
    shallow_set = set(request["shallow"])
    depth = request["deepen"]

    # --- resolve wants to commits, peeling annotated tags -----------------
    tag_oids = set()
    roots = []
    for oid in request["wants"]:
        current = oid
        while True:
            found = repo.get_object(current)
            if found is None:
                break
            if found[0] == "tag":
                tag_oids.add(current)
                current = parse_tag(found[1])["object"]
                continue
            break
        if repo.commit_view(current) is not None:
            roots.append(current)

    # --- walk wanted commits, tracking generation (depth 1 = tip) ----------
    generations = {}
    queue = [(oid, 1) for oid in roots]
    while queue:
        oid, gen = queue.pop(0)
        if oid in generations:
            continue
        view = repo.commit_view(oid)
        if view is None:
            continue
        generations[oid] = gen
        if depth is not None and gen >= depth:
            continue
        if depth is None and oid in shallow_set:
            continue
        queue.extend((p, gen + 1) for p in view["parents"])

    # --- walk what the client already has ---------------------------------
    have = set()
    queue = list(request["haves"])
    while queue:
        oid = queue.pop()
        if oid in have:
            continue
        view = repo.commit_view(oid)
        if view is None:
            continue
        have.add(oid)
        queue.extend(view["parents"])

    wanted = set(generations)
    shallow_out = []
    unshallow_out = []
    if depth is not None:
        shallow_out = [oid for oid, gen in generations.items() if gen == depth]
        for boundary in request["shallow"]:
            if boundary in generations and generations[boundary] < depth:
                unshallow_out.append(boundary)
    else:
        shallow_out = list(request["shallow"])

    # --- the object set: wanted commits + their tree closures + tags ------
    to_send = set(tag_oids)
    for oid in wanted:
        if oid in have:
            continue
        to_send.add(oid)
        view = repo.commit_view(oid)
        if view is not None:
            to_send.update(collect_reachable([view["tree"]], repo.get_object))
    # have-closure objects are never re-sent
    for oid in have:
        to_send.difference_update(collect_reachable([oid], repo.get_object))

    objects = []
    for oid in sorted(to_send):
        found = repo.get_object(oid)
        if found:
            objects.append((found[0], found[1]))
    return objects, sorted(shallow_out), sorted(unshallow_out)


def _sideband_pack(pack):
    out = bytearray()
    chunk = 65515
    for i in range(0, len(pack), chunk):
        out += pkt(b"\x01" + pack[i:i + chunk])
    out += b"0000"
    return bytes(out)


def _ls_refs_response(repo, prefixes, peel, symrefs, unborn):
    """v2 ls-refs answer: one line per ref, HEAD first, then a flush."""
    lines = []
    default_tip = repo.ref_tip("refs/heads/" + repo.default_branch)
    if default_tip:
        line = "%s HEAD" % default_tip
        if symrefs:
            line += " symref-target:refs/heads/%s" % repo.default_branch
        lines.append(line)
    elif unborn:
        line = "unborn HEAD"
        if symrefs:
            line += " symref-target:refs/heads/%s" % repo.default_branch
        lines.append(line)
    for refname, oid in sorted(repo.refs.items()):
        if prefixes and not any(refname.startswith(p) for p in prefixes):
            continue
        line = "%s %s" % (oid, refname)
        if peel:
            found = repo.get_object(oid)
            if found and found[0] == "tag":
                line += " peeled:%s" % parse_tag(found[1])["object"]
        lines.append(line)
    return b"".join(pkt(line + "\n") for line in lines) + b"0000"


def _git_upload_pack(self, repo, raw):
    protocol = self.headers.get("Git-Protocol", "")
    is_v2 = protocol == "version=2"
    if is_v2:
        try:
            sections, _rest = _read_pkt_sections(raw)
        except GitError as exc:
            self._send_error(400, str(exc))
            return
        if not sections or not sections[0]:
            self._send_error(400, "malformed upload-pack request")
            return
        command = sections[0][0]
        if command == "command=ls-refs":
            try:
                prefixes, peel, symrefs, unborn = _parse_ls_refs(raw)
            except GitError as exc:
                self._send_error(400, str(exc))
                return
            with STORE.lock:
                body = _ls_refs_response(repo, prefixes, peel, symrefs, unborn)
            self._send(200, body, "application/x-git-upload-pack-result")
            return
        if command != "command=fetch":
            self._send_error(400, "unknown command: %s" % command)
            return
        try:
            request = _parse_fetch(raw)
        except GitError as exc:
            self._send_error(400, str(exc))
            return
    else:
        try:
            request = _parse_fetch_v0(raw)
        except GitError as exc:
            self._send_error(400, str(exc))
            return
    with STORE.lock:
        objects, shallow, unshallow = _fetch_objects(repo, request)
        pack = write_pack(objects)
    if is_v2:
        body = bytearray()
        body += pkt("acknowledgments\n") + pkt("NAK\n") + b"0000"
        if shallow or unshallow:
            body += pkt("shallow-info\n")
            for oid in shallow:
                body += pkt("shallow %s\n" % oid)
            for oid in unshallow:
                body += pkt("unshallow %s\n" % oid)
            body += b"0000"
        body += pkt("packfile\n") + _sideband_pack(pack)
        self._send(200, bytes(body), "application/x-git-upload-pack-result")
        return
    body = bytearray()
    for oid in shallow:
        body += pkt("shallow %s\n" % oid)
    for oid in unshallow:
        body += pkt("unshallow %s\n" % oid)
    body += pkt("NAK\n") + _sideband_pack(pack)
    self._send(200, bytes(body), "application/x-git-upload-pack-result")


def _git_receive_pack(self, repo, raw):
    try:
        sections, pack = _read_pkt_sections(raw)
    except GitError as exc:
        self._send_error(400, str(exc))
        return
    if not sections or not sections[0]:
        self._send_error(400, "malformed receive-pack request")
        return
    commands = []
    caps = set()
    for line in sections[0]:
        if "\x00" in line:
            ref_part, caps_part = line.split("\x00", 1)
            caps.update(caps_part.split())
            line = ref_part
        parts = line.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    report = []
    with STORE.lock:
        if pack:
            try:
                if not verify_pack_checksum(pack):
                    raise GitError("pack checksum mismatch")
                new_objects, _deltas = read_pack(pack, resolve_base=repo.get_object)
                for oid, (otype, data) in new_objects.items():
                    repo.objects[oid] = (otype, data)
            except GitError as exc:
                report.append(("unpack", False, str(exc)))
                return self._send_receive_report(report, caps)
        for old, new, refname in commands:
            report.append(_apply_push(repo, old, new, refname))
    self._send_receive_report(report, caps)


def _apply_push(repo, old, new, refname):
    current = repo.refs.get(refname)
    if new == ZERO_OID:
        if current is None:
            return (refname, False, "ref does not exist")
        if old != ZERO_OID and old != current:
            return (refname, False, "non-fast-forward")
        del repo.refs[refname]
        return (refname, True, None)
    if repo.get_object(new) is None:
        return (refname, False, "object not found")
    if current is None:
        if old != ZERO_OID:
            return (refname, False, "non-fast-forward")
        repo.refs[refname] = new
        return (refname, True, None)
    if old != current:
        return (refname, False, "non-fast-forward")
    if new == current:
        return (refname, True, None)
    if not repo.is_ancestor(current, new):
        return (refname, False, "non-fast-forward")
    repo.refs[refname] = new
    return (refname, True, None)


def _send_receive_report(self, report, caps):
    banded = "side-band-64k" in caps
    lines = []
    unpack = [item for item in report if item[0] == "unpack"]
    if unpack:
        lines.append("unpack %s" % unpack[0][2])
    else:
        lines.append("unpack ok")
    for refname, ok, reason in report:
        if refname == "unpack":
            continue
        if ok:
            lines.append("ok %s" % refname)
        else:
            lines.append("ng %s %s" % (refname, reason or "failed"))
    body = bytearray()
    for line in lines:
        inner = pkt(line + "\n")
        body += pkt(b"\x01" + inner) if banded else inner
    body += b"0000"
    self._send(200, bytes(body), "application/x-git-receive-pack-result")


ForgeHandler._handle_git = _handle_git
ForgeHandler._git_upload_pack = _git_upload_pack
ForgeHandler._git_receive_pack = _git_receive_pack
ForgeHandler._send_receive_report = _send_receive_report


# ---------------------------------------------------------------------------
# server entrypoint
# ---------------------------------------------------------------------------

def create_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ForgeHandler)
    server.daemon_threads = True
    return server


def main():
    import sys
    import threading
    import urllib.request

    server = create_server()
    host, port = server.server_address
    print("forge listening on %s:%d" % (host, port), flush=True)

    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if env_url:
        # serve in a background thread while the main thread registers the
        # port and long-polls /done; exit 0 when the environment is done.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        register_url = env_url.rstrip("/") + "/register"
        payload = json.dumps({"port": port}).encode("utf-8")
        request = urllib.request.Request(
            register_url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                print("registered: %s" % response.status, flush=True)
        except Exception as exc:
            print("register failed: %s" % exc, file=sys.stderr, flush=True)
            server.shutdown()
            sys.exit(1)

        done_url = env_url.rstrip("/") + "/done"
        while True:
            try:
                with urllib.request.urlopen(done_url, timeout=30) as response:
                    if response.status == 200:
                        payload = json.loads(response.read().decode("utf-8"))
                        if isinstance(payload, dict) and payload.get("done") is True:
                            break
            except Exception:
                pass
            # /done answers {"done": false} while the suite runs; poll gently.
            import time as _time
            _time.sleep(1)
        server.shutdown()
        server.server_close()
        sys.exit(0)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
