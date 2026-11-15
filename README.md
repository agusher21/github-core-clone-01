# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/agusher21/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/agusher21/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in one Python process: git smart HTTP (upload-pack v2, receive-pack v0), the REST v3 API, GraphQL, and MCP tools, all served from one in-memory store. Standard library only — no install, no git binary, no outside services, no UI.

## Run it

```sh
python3 main.py
```

The server binds an ephemeral port on 127.0.0.1 and prints it. In the
competition trial it registers that port with `$MAKEFUN_ENV_URL/register`
and serves until `GET $MAKEFUN_ENV_URL/done` returns 200, then exits 0.

```sh
MAKEFUN_SHOP_TOKEN=<token> MAKEFUN_TRIAL_NOW=<epoch seconds> python3 main.py
```

`MAKEFUN_SHOP_TOKEN` is the bearer token every API, GraphQL, MCP and git
request must carry (no token configured means no auth, for local use).
`MAKEFUN_TRIAL_NOW` is the instance's opening time: the only clock the
forge reads, so commit timestamps, issue dates and ids are deterministic.

## What it serves

- **git smart HTTP** — `/{owner}/{repo}.git`: upload-pack protocol v2
  (ls-refs with peel/symrefs/unborn/ref-prefix, fetch with want/have,
  shallow/deepen, thin-pack, side-band-64k) and receive-pack v0
  (report-status, delete-refs, fast-forward enforcement, mixed ok/ng
  reports). Real packfiles: plain objects, plus OFS/REF delta resolution
  on read, so thin packs from real clients apply cleanly.
- **REST v3** — `/api/v3`: repositories, branches, commits, git refs,
  contents (base64, create/update/delete through real git commits),
  labels, issues (+ comments), pull requests (+ files diff, squash
  merge with three-way conflict detection), pagination with `Link`,
  `X-GitHub-Request-Id` on every reply.
- **GraphQL** — `/api/graphql`: viewer, repository, node (opaque global
  ids), issues/pull requests with cursor pagination, the createIssue /
  closeIssue / createPullRequest / mergePullRequest / addComment
  mutations, and `__type`/`__schema` introspection. Batching supported.
- **MCP** — `/mcp`: streamable HTTP, protocol revision 2026-07-28 with a
  legacy initialize fallback, `server/discover`, and eight tools
  (create_repo, get_repo, create_issue, list_issues, create_pull_request,
  merge_pull_request, get_issue, list_pull_requests) whose results stay
  in parity with the REST surface.

## Layout

- `main.py` — entrypoint; binds, registers, serves, exits on `/done`.
- `forge.py` — the whole forge: store, REST, GraphQL, MCP, git handlers.
- `gitcore.py` — git object model: SHA-1 objects, pkt-lines, packfile
  write/read (plain + delta), the fixed scenario RNG that derives the
  viewer login (`adaiversen`) from the competition seed.

## Health

```sh
curl http://127.0.0.1:<port>/healthz   # {"ok": true}
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=w513bxaz&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
