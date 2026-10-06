#!/usr/bin/env python3
"""python3 _sync/test_ncweb.py - checks conflict handling, path safety, and
end to end (needs rclone) that syncing only some changes leaves listings the
next ncsync run agrees with, on a throwaway repo whose "server" is a folder."""
import importlib.util, os, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_loader(
    "ncweb", importlib.machinery.SourceFileLoader("ncweb", os.path.join(HERE, "ncweb")))
nc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nc)


def put(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def get(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else None


# -- conflict copies, path safety ------------------------------------------
tmp = tempfile.mkdtemp()
nc.FILES = tmp
for n in ("scan.exr", "scan.exr.conflict-2026-09-181", "scan.exr.conflict-2026-09-182", "notes.txt"):
    put(os.path.join(tmp, "shot", n), n)

found, more = nc.find_conflicts()
assert not more and len(found) == 1, found
g = found[0]
assert g["base"] == os.path.join("shot", "scan.exr"), g
assert [v["origin"] for v in g["versions"]] == ["current", "server", "drive"], g

# keeping this drive's copy: it takes the name, the other copy goes
assert nc.keep_version(os.path.join("shot", "scan.exr.conflict-2026-09-182")) is None
assert get(os.path.join(tmp, "shot", "scan.exr")) == "scan.exr.conflict-2026-09-182"
assert sorted(os.listdir(os.path.join(tmp, "shot"))) == ["notes.txt", "scan.exr"]
assert nc.keep_version(os.path.join("shot", "notes.txt")) == "no conflict copies of that file"

# path traversal is refused, ordinary paths are not
assert nc.under_files("../../etc/passwd") is None
assert nc.under_files("shot/scan.exr") == os.path.join(os.path.realpath(tmp), "shot", "scan.exr")
assert nc.keep_version("../../etc/passwd") == "no such file"

# -- listings: bisync's own lines read back and write out unchanged --------
line = '- 67496094 - - 2026-09-06T18:00:42.629999995+0000 "a/Angebot 12:30 \\"x\\".fbx"'
lst = os.path.join(tmp, "t.lst")
put(lst, "# bisync listing v1 from 2026-09-24T13:59:36.515278000+0000\n" + line + "\n")
e = nc.load_listing(lst)['a/Angebot 12:30 "x".fbx']
assert (e.size, nc.from_ns(e.ns)) == (67496094, "2026-09-06T18:00:42.629999995+0000"), e
nc.save_listing(lst, {"k": e, "n": e._replace(name="b/ü\tx", line=None)})
back = nc.load_listing(lst)
assert back['a/Angebot 12:30 "x".fbx'].line == line and "b/ü\tx" in back, back
assert nc.to_ns("2026-09-06T20:00:42.63+02:00") == e.ns + 5  # rclone lsjson's spelling

# a move pairs only when the match is unique: two candidates, no pair
v = {"size": 5, "mtime": 0, "dir": False}
gone = lambda k: {"key": k, "path": k, "dir": False, "local": "deleted", "server": None, "l": None, "s": v}
amb = [gone("a"), gone("b"), {"key": "c", "path": "c", "dir": False, "local": "new", "server": None, "l": v, "s": None}]
assert len(nc.pair_moves(amb)) == 3 and "from" not in amb[2], amb

if not shutil.which("rclone"):
    print("ok (end-to-end part skipped: no rclone)")
    sys.exit(0)

# -- end to end: pick some changes, then let ncsync carry on ----------------
T = tempfile.mkdtemp()
S = os.path.join(T, "server", "proj")          # the fake Nextcloud
D = os.path.join(T, "files", "proj")           # this drive
for d in ("_sync", "server/proj", "empty", "files"):
    os.makedirs(os.path.join(T, d))
put(os.path.join(T, ".repo-id"), "")
shutil.copy(os.path.join(HERE, "ncsync"), os.path.join(T, "_sync", "ncsync"))
shutil.copy(os.path.join(HERE, "filters.txt"), os.path.join(T, "_sync", "filters.txt"))
put(os.path.join(T, "_sync", "folders.txt"), "proj\n")
# a union keeps the name "nc" in rclone's state file names; an alias would not
put(os.path.join(T, "_sync", "rclone.conf"),
    "[nc]\ntype = union\nupstreams = %s %s\n" % (os.path.join(T, "server"), os.path.join(T, "empty")))
nc.REPO, nc.SELF, nc.FILES = T, os.path.join(T, "_sync"), os.path.join(T, "files")
nc.REPO_ID = os.path.join(T, ".repo-id")
for k, f in [("CONF", "rclone.conf"), ("FILTERS", "filters.txt"), ("FOLDERS", "folders.txt"),
             ("STATE", "state"), ("NCSYNC", "ncsync"), ("LOGS", "logs")]:
    setattr(nc, k, os.path.join(nc.SELF, f))
nc.MARKERS = os.path.join(nc.STATE, "initialised")


def ncsync(*args):
    # two runs in the same second would share one log file
    shutil.rmtree(nc.LOGS, ignore_errors=True)
    p = subprocess.run(["bash", nc.NCSYNC] + list(args), capture_output=True, text=True)
    log = max((os.path.join(nc.LOGS, f) for f in os.listdir(nc.LOGS)), key=os.path.getmtime)
    return p.returncode, p.stdout + p.stderr, get(log)


def kinds():
    r = nc.review("proj")
    assert "error" not in r, r
    return {c["path"]: (c["local"], c["server"]) for c in r["changes"]}


odd = "odd 12：30.txt"                    # "odd 12:30.txt" as exFAT has to store it
for n, t in [("keep.txt", "same"), ("srvmod.txt", "v1"), ("locmod.txt", "v1"),
             ("srvdel.txt", "x"), ("locdel.txt", "x"), ("both.txt", "v1"),
             ("held.txt", "v1"), ("gone/x.txt", "x"), ("gone/sub/y.txt", "y"), (odd, "colon")]:
    put(os.path.join(S, n), t)
rc, out, log = ncsync()
assert rc == 0 and "done" in out, out + (log or "")
assert get(os.path.join(D, odd)) == "colon" and kinds() == {}, kinds()

put(os.path.join(S, "srvmod.txt"), "v2 server")
put(os.path.join(S, "srvnew.txt"), "new on server")
os.remove(os.path.join(S, "srvdel.txt"))
put(os.path.join(S, "both.txt"), "server's both")
put(os.path.join(S, "srvdir", "new", "z.txt"), "zz")  # not srvdel.txt's size: that pair would read as a move
put(os.path.join(D, "locmod.txt"), "v2 local!")
put(os.path.join(D, "locnew.txt"), "new locally")
os.remove(os.path.join(D, "locdel.txt"))
put(os.path.join(D, "both.txt"), "local both, longer")
put(os.path.join(D, "held.txt"), "held back change")
put(os.path.join(D, odd), "colon, changed")
shutil.rmtree(os.path.join(D, "gone"))
os.makedirs(os.path.join(D, "emptynew"))

want = {"locmod.txt": ("modified", None), "locnew.txt": ("new", None),
        "locdel.txt": ("deleted", None), "held.txt": ("modified", None),
        "odd 12:30.txt": ("modified", None), "emptynew": ("new", None),
        "gone": ("deleted", None), "gone/x.txt": ("deleted", None),
        "gone/sub": ("deleted", None), "gone/sub/y.txt": ("deleted", None),
        "srvmod.txt": (None, "modified"), "srvnew.txt": (None, "new"),
        "srvdel.txt": (None, "deleted"), "srvdir": (None, "new"),
        "srvdir/new": (None, "new"), "srvdir/new/z.txt": (None, "new"),
        "both.txt": ("modified", "modified")}
got = kinds()
assert got == want, {k: (got.get(k), want.get(k)) for k in set(got) | set(want) if got.get(k) != want.get(k)}

# sync everything but held.txt; the conflict goes the server's way
nc.REVIEW["folders"]["proj"] = nc.review("proj")
picks = {k: ("up" if l else "down") for k, (l, s) in want.items() if k != "held.txt"}
picks["both.txt"] = "down"
os.remove(nc.FILTERS + ".md5")  # as if this page had made the first sync
job = nc.Job()
assert nc.apply_task(job, {"proj": picks}) == 0, "\n".join(job.lines)

for n, t in [("locmod.txt", "v2 local!"), ("locnew.txt", "new locally"), ("locdel.txt", None),
             ("both.txt", "server's both"), ("held.txt", "v1"), (odd, "colon, changed"),
             ("gone/x.txt", None)]:
    assert get(os.path.join(S, n)) == t, (n, get(os.path.join(S, n)))
for n, t in [("srvmod.txt", "v2 server"), ("srvnew.txt", "new on server"), ("srvdel.txt", None),
             ("srvdir/new/z.txt", "zz"), ("both.txt", "server's both"),
             ("held.txt", "held back change")]:
    assert get(os.path.join(D, n)) == t, (n, get(os.path.join(D, n)))
assert os.path.isdir(os.path.join(S, "emptynew")) and not os.path.exists(os.path.join(S, "gone"))
assert kinds() == {"held.txt": ("modified", None)}, kinds()
assert nc.REVIEW["folders"]["proj"]["changes"][0]["path"] == "held.txt"

# a later edit to a file synced above is an ordinary change, not a conflict
put(os.path.join(S, "locmod.txt"), "v3 from server")
put(os.path.join(S, "both.txt"), "server again!!")
assert kinds() == {"held.txt": ("modified", None), "locmod.txt": (None, "modified"),
                   "both.txt": (None, "modified")}, kinds()

# ncsync takes over from the listings written above: no complaints about
# them, and it finds exactly the three changes still open
rc, out, log = ncsync()
assert rc == 0 and "done" in out, out + (log or "")
assert not re.search(r"Ignoring incorrect line|Duplicate line|out of sync|conflict", log), log
seen = set(re.findall(r"- Path([12]) +File (?:changed|is new|was deleted).*?- (\S+)$", log, re.M))
assert seen == {("1", "locmod.txt"), ("1", "both.txt"), ("2", "held.txt")}, seen
assert get(os.path.join(S, "held.txt")) == "held back change"
assert get(os.path.join(D, "locmod.txt")) == "v3 from server" and kinds() == {}, kinds()

# moved on either side: renamed on the other side, nothing sent again
os.makedirs(os.path.join(D, "renamed"))
os.rename(os.path.join(D, "keep.txt"), os.path.join(D, "renamed", "keep2.txt"))
os.makedirs(os.path.join(S, "moved_srv"))
os.rename(os.path.join(S, "srvnew.txt"), os.path.join(S, "moved_srv", "srvnew.txt"))
want = {"renamed": ("new", None), "renamed/keep2.txt": ("moved", None),
        "moved_srv": (None, "new"), "moved_srv/srvnew.txt": (None, "moved")}
assert kinds() == want, kinds()
nc.REVIEW["folders"]["proj"] = nc.review("proj")
job = nc.Job()
assert nc.apply_task(job, {"proj": {k: ("up" if l else "down") for k, (l, s) in want.items()}}) == 0, \
    "\n".join(job.lines)
assert get(os.path.join(S, "renamed", "keep2.txt")) == "same" and get(os.path.join(S, "keep.txt")) is None
assert get(os.path.join(D, "moved_srv", "srvnew.txt")) == "new on server"
assert get(os.path.join(D, "srvnew.txt")) is None and kinds() == {}, kinds()
assert not any("Copied" in x for x in job.lines) and sum(x.endswith(": Moved") for x in job.lines) == 2, job.lines
rc, out, log = ncsync()
assert rc == 0 and not re.findall(r"File (?:changed|is new|was deleted)", log), log

# changed on both sides, ncsync alone: the newer copy keeps the name, the
# older is kept beside it and the page can tell whose it was
put(os.path.join(S, "both.txt"), "older, server", mtime=1700000000)
put(os.path.join(D, "both.txt"), "newer, this drive", mtime=1700000500)
rc, out, log = ncsync()
assert rc == 0, out + (log or "")
nc.FILES = os.path.join(T, "files")
g = nc.find_conflicts()[0]
assert len(g) == 1 and g[0]["base"] == os.path.join("proj", "both.txt"), g
assert [v["origin"] for v in g[0]["versions"]] == ["current", "server"], g
assert get(os.path.join(D, "both.txt")) == "newer, this drive"
assert get(os.path.join(nc.FILES, g[0]["versions"][1]["path"])) == "older, server"

shutil.rmtree(T)
print("ok")
