# gitcore.py — the git primitives a forge is built on, written from the wire
# format itself: pkt-lines, SHA-1 objects, trees, commits, tags, packfiles
# (plain and delta), and the seeded scenario that names the trial's users.
#
# Everything here is deterministic: object ids are content hashes, pack bytes
# are a pure function of their objects, and the scenario users come from the
# competition's fixed seed, never from a clock or unseeded randomness.

from __future__ import annotations

import base64
import hashlib
import re
import zlib

ZERO_OID = "0" * 40

TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4, "ofs-delta": 6, "ref-delta": 7}
CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag", 6: "ofs-delta", 7: "ref-delta"}

CONFORMANCE_IDENTITY = "Conformance <conformance@make.fun>"
DEFAULT_TIMESTAMP = 1700000000


class GitError(Exception):
    """A malformed git object, pack or protocol frame."""


# --------------------------------------------------------------------------
# objects
# --------------------------------------------------------------------------

def hash_object(otype: str, data: bytes) -> str:
    head = ("%s %d\0" % (otype, len(data))).encode("ascii")
    return hashlib.sha1(head + data).hexdigest()


def is_oid(value) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _tree_sort_key(entry) -> str:
    mode, name, _oid = entry
    return name + "/" if mode == "40000" else name


def build_tree(entries) -> bytes:
    """entries: iterable of (mode, name, oid-hex). Returns the tree's bytes."""
    ordered = sorted(entries, key=_tree_sort_key)
    out = bytearray()
    for mode, name, oid in ordered:
        out += ("%s %s\0" % (mode, name)).encode("utf-8")
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_tree(data: bytes):
    """Returns [(mode, name, oid-hex)] in stored order."""
    entries = []
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        mode = data[pos:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


def build_commit(tree: str, parents, message: str, author: str, committer: str,
                 timestamp: int, timezone: str = "+0000") -> bytes:
    lines = ["tree %s" % tree]
    for parent in parents:
        lines.append("parent %s" % parent)
    lines.append("author %s %d %s" % (author, timestamp, timezone))
    lines.append("committer %s %d %s" % (committer, timestamp, timezone))
    lines.append("")
    return ("\n".join(lines) + "\n" + message).encode("utf-8")


def parse_commit(data: bytes):
    """Returns {'tree', 'parents', 'author', 'committer', 'message', 'headers'}."""
    head, _, message = data.partition(b"\n\n")
    view = {"tree": "", "parents": [], "author": "", "committer": "",
            "message": message.decode("utf-8", "replace"), "headers": []}
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            view["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            view["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            view["author"] = line[7:]
        elif line.startswith("committer "):
            view["committer"] = line[10:]
        else:
            view["headers"].append(line)
    return view


def build_tag(obj: str, otype: str, tag: str, message: str, tagger: str,
              timestamp: int, timezone: str = "+0000") -> bytes:
    text = ("object %s\ntype %s\ntag %s\ntagger %s %d %s\n\n%s"
            % (obj, otype, tag, tagger, timestamp, timezone, message))
    return text.encode("utf-8")


def parse_tag(data: bytes):
    head, _, message = data.partition(b"\n\n")
    view = {"object": "", "type": "", "tag": "", "tagger": "",
            "message": message.decode("utf-8", "replace")}
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.startswith("object "):
            view["object"] = line[7:].strip()
        elif line.startswith("type "):
            view["type"] = line[5:].strip()
        elif line.startswith("tag "):
            view["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            view["tagger"] = line[7:]
    return view


def collect_tree_closure(roots, lookup, allow_missing=False):
    """Every oid reachable from roots through trees/tags only — never
    through commit parents. A shallow client holds a boundary commit and
    its tree, but NOT the boundary's parents, so this is the correct
    subtraction set for a shallow have."""

    seen = set()
    queue = [oid for oid in roots if is_oid(oid)]
    while queue:
        oid = queue.pop()
        if oid in seen:
            continue
        found = lookup(oid)
        if found is None:
            if allow_missing:
                continue
            raise GitError("object %s is missing from the store" % oid)
        otype, data = found
        seen.add(oid)
        if otype == "commit":
            queue.append(parse_commit(data)["tree"])
        elif otype == "tag":
            queue.append(parse_tag(data)["object"])
        elif otype == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    queue.append(child)
    return seen


def collect_reachable(roots, lookup, allow_missing=False):
    """Every oid reachable from roots through commits/trees/tags.

    lookup(oid) -> (otype, data) | None. Returns a set of oids.
    """
    seen = set()
    queue = [oid for oid in roots if is_oid(oid)]
    while queue:
        oid = queue.pop()
        if oid in seen:
            continue
        found = lookup(oid)
        if found is None:
            if allow_missing:
                continue
            raise GitError("object %s is missing from the store" % oid)
        otype, data = found
        seen.add(oid)
        if otype == "commit":
            view = parse_commit(data)
            queue.append(view["tree"])
            queue.extend(view["parents"])
        elif otype == "tag":
            queue.append(parse_tag(data)["object"])
        elif otype == "tree":
            for mode, _name, child in parse_tree(data):
                if mode != "160000":
                    queue.append(child)
    return seen


# --------------------------------------------------------------------------
# pkt-lines
# --------------------------------------------------------------------------

def pkt(payload) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def pkt_line(text: str) -> bytes:
    return pkt(text.encode("utf-8"))


PKT_FLUSH = b"0000"
PKT_DELIM = b"0001"
PKT_END = b"0002"


def encode_pkt_lines(items) -> bytes:
    """items: str | bytes | 'flush' | 'delim' | 'end'."""
    out = bytearray()
    for item in items:
        if item == "flush":
            out += PKT_FLUSH
        elif item == "delim":
            out += PKT_DELIM
        elif item == "end":
            out += PKT_END
        elif isinstance(item, str):
            out += pkt_line(item)
        else:
            out += pkt(item)
    return bytes(out)


class PktReader:
    """Incremental pkt-line reader over one buffer; keeps the unconsumed tail."""

    def __init__(self, data: bytes):
        self.buf = data
        self.pos = 0

    def next(self):
        """Returns ('data', payload) | ('flush', None) | ('delim', None) |
        ('end', None) | (None, None) when the buffer runs out."""
        if self.pos + 4 > len(self.buf):
            return (None, None)
        head = self.buf[self.pos:self.pos + 4]
        if head == PKT_FLUSH:
            self.pos += 4
            return ("flush", None)
        if head == PKT_DELIM:
            self.pos += 4
            return ("delim", None)
        if head == PKT_END:
            self.pos += 4
            return ("end", None)
        try:
            length = int(head.decode("ascii"), 16)
        except ValueError:
            raise GitError("bad pkt-line length %r" % head)
        if length < 4 or self.pos + length > len(self.buf):
            raise GitError("pkt-line overruns its buffer")
        payload = self.buf[self.pos + 4:self.pos + length]
        self.pos += length
        return ("data", payload)

    def rest(self) -> bytes:
        return self.buf[self.pos:]

    def lines(self):
        """All data lines up to (not including) the first flush/delim/end."""
        out = []
        while True:
            kind, payload = self.next()
            if kind == "data":
                out.append(payload)
            elif kind is None:
                raise GitError("pkt-line stream ends before its flush")
            else:
                return out


def decode_all_packets(data: bytes):
    """Every packet of a body (used for the small request bodies we accept)."""
    reader = PktReader(data)
    packets = []
    while True:
        kind, payload = reader.next()
        if kind is None:
            break
        packets.append((kind, payload))
    return packets, reader.rest()


# --------------------------------------------------------------------------
# packfiles
# --------------------------------------------------------------------------

def _pack_object_header(otype: str, size: int) -> bytes:
    code = TYPE_CODE[otype]
    out = bytearray()
    first = (code << 4) | (size & 0x0F)
    size >>= 4
    while size > 0:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def write_pack(objects) -> bytes:
    """objects: iterable of (otype, data). Plain objects only, no deltas."""
    items = list(objects)
    parts = [b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")]
    for otype, data in items:
        parts.append(_pack_object_header(otype, len(data)))
        parts.append(zlib.compress(data))
    body = b"".join(parts)
    return body + hashlib.sha1(body).digest()


def pack_checksum(pack: bytes) -> str:
    return hashlib.sha1(pack[:-20]).hexdigest()


def verify_pack_checksum(pack: bytes) -> bool:
    return len(pack) >= 20 and pack[-20:].hex() == pack_checksum(pack)


def _read_varint_size(buf: bytes, start: int):
    if start >= len(buf):
        raise GitError("pack entry has no size header")
    byte = buf[start]
    size = byte & 0x0F
    shift = 4
    pos = start + 1
    while byte & 0x80:
        if pos >= len(buf):
            raise GitError("pack entry size varint is truncated")
        byte = buf[pos]
        pos += 1
        size += (byte & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise GitError("pack entry size varint is too long")
    return size, pos


def _read_ofs_base(buf: bytes, start: int):
    if start >= len(buf):
        raise GitError("ofs-delta has no base offset")
    byte = buf[start]
    pos = start + 1
    value = byte & 0x7F
    while byte & 0x80:
        if pos >= len(buf):
            raise GitError("ofs-delta base offset is truncated")
        byte = buf[pos]
        pos += 1
        value = ((value + 1) << 7) + (byte & 0x7F)
    return value, pos


def _inflate_at(buf: bytes, offset: int, end: int):
    """Inflate one zlib stream starting at offset; returns (data, next offset)."""
    obj = zlib.decompressobj()
    try:
        data = obj.decompress(buf[offset:end])
    except zlib.error as err:
        raise GitError("zlib stream at offset %d did not inflate: %s" % (offset, err))
    consumed = (end - offset) - len(obj.unused_data)
    if consumed <= 0:
        raise GitError("zlib stream at offset %d consumed no bytes" % offset)
    return data, offset + consumed


def apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0

    def read_size():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            if pos >= len(delta):
                raise GitError("delta size varint is truncated")
            byte = delta[pos]
            pos += 1
            value += (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                return value
            if shift > 63:
                raise GitError("delta size varint is too long")

    source_size = read_size()
    target_size = read_size()
    if source_size != len(base):
        raise GitError("delta expects a %d-byte base, got %d" % (source_size, len(base)))
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            shift = 0
            for bit in range(4):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise GitError("delta copy offset is truncated")
                    copy_off += delta[pos] << shift
                    pos += 1
                shift += 8
            shift = 0
            for bit in range(4, 7):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise GitError("delta copy size is truncated")
                    copy_size += delta[pos] << shift
                    pos += 1
                shift += 8
            if copy_size == 0:
                copy_size = 0x10000
            if copy_off + copy_size > len(base):
                raise GitError("delta copy is out of the base")
            out += base[copy_off:copy_off + copy_size]
        elif op != 0:
            literal = op & 0x7F
            if pos + literal > len(delta):
                raise GitError("delta insert is truncated")
            out += delta[pos:pos + literal]
            pos += literal
        else:
            raise GitError("delta opcode 0 is invalid")
    if len(out) != target_size:
        raise GitError("delta produced %d of %d bytes" % (len(out), target_size))
    return bytes(out)


def read_pack(pack: bytes, resolve_base=None):
    """Parse a packfile; resolve OFS/REF deltas (resolve_base supplies the
    thin-pack bases the client already holds). Returns
    (objects: {oid: (otype, data)}, delta_count). Every object's SHA-1 is
    recomputed and its declared size checked."""
    if len(pack) < 32:
        raise GitError("pack is %d bytes, too short" % len(pack))
    if pack[:4] != b"PACK":
        raise GitError("pack does not start with PACK")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise GitError("unsupported pack version %d" % version)
    declared = int.from_bytes(pack[8:12], "big")
    trailer = pack[-20:].hex()
    if trailer != pack_checksum(pack):
        raise GitError("pack trailer does not match its bytes")
    trailer_start = len(pack) - 20

    raw = []          # (offset, type_code, size, data, base_offset, base_oid)
    by_offset = {}
    offset = 12
    for _ in range(declared):
        entry_off = offset
        size, offset = _read_varint_size(pack, offset)
        first = pack[entry_off]
        type_code = (first >> 4) & 0x07
        base_off = None
        base_oid = None
        if type_code == 6:
            back, offset = _read_ofs_base(pack, offset)
            if back <= 0 or back > entry_off:
                raise GitError("ofs-delta at %d names an invalid base offset %d" % (entry_off, back))
            base_off = entry_off - back
        elif type_code == 7:
            if offset + 20 > trailer_start:
                raise GitError("ref-delta base id is truncated")
            base_oid = pack[offset:offset + 20].hex()
            offset += 20
        elif type_code not in CODE_TYPE:
            raise GitError("pack entry has unknown type code %d" % type_code)
        data, offset = _inflate_at(pack, offset, trailer_start)
        if len(data) != size:
            raise GitError("pack entry at %d declares %d bytes but inflated to %d"
                           % (entry_off, size, len(data)))
        raw.append((entry_off, type_code, size, data, base_off, base_oid))
        by_offset[entry_off] = len(raw) - 1
    if offset != trailer_start:
        raise GitError("pack has %d stray bytes before its trailer" % (trailer_start - offset))

    resolved = {}     # index -> (otype, data, oid)
    resolved_by_oid = {}
    resolving = set()
    objects = {}
    delta_count = 0

    def resolve(index):
        if index in resolved:
            return resolved[index]
        if index in resolving:
            raise GitError("delta cycle at pack offset %d" % raw[index][0])
        resolving.add(index)
        _off, type_code, _size, data, base_off, base_oid = raw[index]
        if type_code == 6:
            base = resolve(by_offset[base_off])
            otype, content = base[0], apply_delta(base[1], data)
        elif type_code == 7:
            if base_oid in resolved_by_oid:
                base = resolve(resolved_by_oid[base_oid])
                otype, content = base[0], apply_delta(base[1], data)
            else:
                external = resolve_base(base_oid) if resolve_base else None
                if external is None:
                    raise GitError("ref-delta base %s is neither in the pack nor supplied" % base_oid)
                otype, content = external[0], apply_delta(external[1], data)
        else:
            otype, content = CODE_TYPE[type_code], data
        oid = hash_object(otype, content)
        resolved[index] = (otype, content, oid)
        resolved_by_oid[oid] = index
        resolving.discard(index)
        return resolved[index]

    for index, entry in enumerate(raw):
        otype, content, oid = resolve(index)
        if entry[1] in (6, 7):
            delta_count += 1
        if oid not in objects:
            objects[oid] = (otype, content)
    return objects, delta_count


# --------------------------------------------------------------------------
# the seeded scenario (wire.ts's SCENARIO_SEED, ported exactly)
# --------------------------------------------------------------------------

U64_MASK = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = [
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo",
    "iris", "jonas", "kira", "lars", "mira", "nico", "olga", "pavel",
    "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane",
]
LAST_NAMES = [
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia",
    "holm", "iversen", "jansen", "keller", "lind", "moreau", "novak",
    "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega",
    "weber", "xu", "yilmaz", "zimmer",
]


def _fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def _mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


class ScenarioRng:
    """xorshift64*, seeded exactly like wire.ts's Rng."""

    def __init__(self, seed: int):
        masked = seed & U64_MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    @classmethod
    def from_label(cls, label: str):
        return cls(_mix64(SCENARIO_SEED ^ _fnv1a64(label)))

    def next_u64(self) -> int:
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, max_exclusive: int) -> int:
        if max_exclusive == 1:
            return 0
        limit = 4294967296 - (4294967296 % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


def scenario_users():
    """The scenario's 12 users, in order: [viewer, teammate, stranger, ...]."""
    rng = ScenarioRng.from_label("users")
    used = set()
    users = []
    for i in range(12):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = (first + last).lower()
        login = base
        suffix = 2
        while login in used:
            login = "%s-%d" % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({"id": 1000 + i, "login": login,
                      "name": "%s %s" % (first, last),
                      "email": "%s@example.invalid" % login})
    return users


def global_id(kind: str, *parts) -> str:
    """An opaque Relay-style global id (never the plain database id)."""
    raw = ("%s:%s" % (kind, ":".join(str(p) for p in parts))).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_global_id(value):
    """Returns (kind, [parts]) or None when the id is not one of ours."""
    if not isinstance(value, str) or not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    kind, rest = raw.split(":", 1)
    if kind not in ("Repository", "Issue", "PullRequest", "IssueComment", "User"):
        return None
    return kind, rest.split(":")
