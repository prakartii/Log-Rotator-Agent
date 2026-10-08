# Log-Rotator-Agent



The Log Rotator Agent manages bloated log files **without interrupting the
process that is writing to them**. It archives the current contents of an
active log (snapshot → gzip → verify) and then empties the log in place with
`ftruncate()`. The log is never deleted and recreated, so its inode stays the
same and the writer process keeps running on its open file descriptor.

> Status: under active development. See the development phases below.

## Requirements

- Linux (Ubuntu / WSL2). The agent uses Linux-only system calls such as `fcntl.flock`.
- Python 3.9+ (standard library only, no extra packages needed)

## Project structure

```
agent.py                 # command-line entry point (later phase)
writer.py                # simulated long-running process that appends to a log
demo/
    inode_demo.py        # presentation demo: same inode before/after, writer keeps running
log_rotator/
    config.py            # directories, allowed roots, log aliases
    errors.py            # stable error codes + structured success/error results
    tools/
        safety.py        # path validation and safe open (O_NOFOLLOW + inode check)
        log_info.py      # identify_log(), list_logs(), get_log_info()
        processes.py     # which processes have a log open (/proc/<pid>/fd, fdinfo)
        snapshot.py      # snapshot_log(): exact byte-range copy of the open log
        compressor.py    # compress_log(), archive_name(): atomic gzip archives
        verifier.py      # verify_archive(), verify_copy(), verify_rotation(), watch_log_growth()
        truncator.py     # truncate_log(): ftruncate() on the open descriptor
        locking.py       # rotation_lock(), writer_lock(): flock-based locking
    rotate.py            # rotate_log(): the full safe-rotation pipeline
logs/                    # active demo logs (contents git-ignored)
rotated_logs/            # compressed archives (contents git-ignored)
tests/                   # unittest test suite
```

## Tools implemented so far

All tools return plain dictionaries with `"status": "success"` or
`"status": "error"` plus a stable `error_code`, so the master agent can use
the results directly as JSON.

| Tool | What it does | OS concepts |
|---|---|---|
| `identify_log(query)` | Resolves `"the apache error logs"`, a file name or a path to a validated log | aliases, path resolution |
| `list_logs()` | Lists the regular files in `logs/` | `scandir`, `lstat` (symlinks are skipped) |
| `get_log_info(path)` | Inode, size, disk usage, holes, mode, owner, timestamps, open handles | `open` + `fstat` + `close`, `lseek(SEEK_HOLE)`, `/proc` |
| `validate_log_path(path)` | Allows only regular files inside `logs/` | `realpath`, `lstat`, `S_ISREG`, `st_nlink`, `access()` |
| `open_validated(path)` | Opens the validated file without races | `O_NOFOLLOW`, `fstat` (device, inode) check against TOCTOU |
| `find_open_handles(path)` | Like `lsof`: pid, fd, access mode, `O_APPEND`, offset | `/proc/<pid>/fd`, `/proc/<pid>/fdinfo` |
| `snapshot_log(fd, dest, length)` | Copies exactly `length` bytes of the open log into a private file and returns their SHA-256 | `fstat`, `pread`, `O_EXCL`, mode `0600`, short writes, `fsync` |
| `compress_log(snapshot)` | gzips the snapshot into `rotated_logs/` | temp file + `fsync` + `link()` + `unlink` + directory `fsync` |
| `archive_name(log)` | `apache_error.log.2026-10-07T195312.gz` (optional label, e.g. `2026-09`) | |
| `verify_archive(archive, sha256, size)` | Decompresses the archive from disk and compares it with the snapshot | `O_NOFOLLOW`, `fstat`, gzip CRC-32 + length, SHA-256 |
| `truncate_log(fd)` | Empties the log in place; reports inode and size before/after | `ftruncate` on the open descriptor |
| `verify_rotation(path, before)` | After truncation: same inode, still appendable, same mode/owner, writers still attached | `lstat`, inode compare, `O_APPEND` open + zero-byte `write`, `/proc` |
| `watch_log_growth(path, inode)` | Samples inode + size until the writer's new lines appear | `stat` polling |
| `rotation_lock(path)` | Lets only one rotation of a log run at a time; names the pid holding it | `flock(LOCK_EX \| LOCK_NB)` on `.<log>.rotate.lock` |
| `rotate_log(log, ...)` | The whole pipeline below, returns one structured result | everything above |

### How an archive is written safely

1. **Snapshot.** `fstat()` the open log to pin its current size, then copy exactly
   that many bytes with `pread()`. `pread()` reads at a given position without moving the
   descriptor's offset. The writer may keep appending; those new bytes are not part
   of this snapshot. The copy is `fsync()`ed and a SHA-256 is computed on the way.
2. **Compress.** Stream the snapshot through gzip into a hidden temp file
   (`O_CREAT | O_EXCL`), then `fsync()` it.
3. **Publish atomically.** `link()` the temp file to its final name, then `unlink()`
   the temp name. `rename()` would silently replace an existing archive, but
   `link()` fails with `EEXIST`, so on a clash the archive gets a `-1`, `-2`, ...
   suffix instead. Finally the directory is `fsync()`ed so the new name is durable.
4. **On any failure** the partial snapshot or temp archive is deleted and an error
   (`SNAPSHOT_FAILED` / `COMPRESSION_FAILED`) is returned. The active log is only
   ever *read* in these steps, so it can never be damaged by a failed archive.
5. **Verify before truncating.** The archive is reopened from disk and fully
   decompressed. It passes only if it is a regular file, gzip's own CRC-32 and
   length checks succeed, and the decompressed size **and** SHA-256 equal the
   snapshot's. CRC-32 catches accidental damage; SHA-256 proves it is the right
   content. Any failure returns `VERIFY_FAILED` with the failing check, and the
   active log must not be truncated.

```json
{"status": "success", "action": "verify_archive", "verified": true,
 "archive_size": 193926, "uncompressed_size": 2097266, "sha256": "932efb0c…",
 "checks": {"regular_file": true, "gzip_valid": true, "size_match": true, "sha256_match": true}}
```

### Safety rules

The agent refuses to touch a file when:

- it is outside the allowed log directories (blocks `../../etc/passwd`, `/var/log/syslog`)
- it is a symbolic link, or a directory, FIFO, socket or device
- it has more than one hard link (another name for the same inode could live elsewhere)
- the agent lacks read/write permission
- it was replaced between validation and `open()` (TOCTOU race)

Error codes: `INVALID_REQUEST`, `LOG_NOT_FOUND`, `AMBIGUOUS_LOG`,
`NOT_A_REGULAR_FILE`, `OUTSIDE_ALLOWED_DIR`, `SYMLINK_REJECTED`,
`HARDLINK_REJECTED`, `FILE_CHANGED`, `PERMISSION_DENIED`, `SNAPSHOT_FAILED`,
`COMPRESSION_FAILED`, `VERIFY_FAILED`, `TRUNCATE_FAILED`, `ROTATION_VERIFY_FAILED`,
`OS_ERROR`.

## Safe rotation: `rotate_log()`

```python
from log_rotator.rotate import rotate_log

rotate_log("the apache error logs")            # archive + gzip + truncate
rotate_log("apache_error", truncate=False)     # archive only
rotate_log("apache_error", compress=False)     # plain (uncompressed) archive
rotate_log("apache_error", label="2026-09")    # apache_error.log.2026-09.<time>.gz
rotate_log("apache_error", dry_run=True)       # report what would happen, change nothing
```

Pipeline (every step is timed and listed in the result's `steps`):

```
identify ─► open(O_RDWR) ─► snapshot ─► compress ─► verify ─► catch up ─► ftruncate(fd, 0)
                │                                              │
     same descriptor for every step                 new lines the writer appended
     (same inode, even if renamed)                  during rotation -> archive
```

### Why truncate instead of delete?

The writer process holds a **file descriptor** pointing at the log's **inode**, not its name.

| | `rm log` + create a new one | `ftruncate(fd, 0)` (what we do) |
|---|---|---|
| Inode | new inode | **same inode** |
| Writer's descriptor | still points at the old, now nameless inode | still valid |
| Writer's new lines | silently lost (nobody can see them) | appear in the log again from offset 0 |
| Disk space | not freed until the writer restarts | freed immediately |
| Writer process | must be restarted / signalled | **keeps running** |

The writer must open the log with `O_APPEND`. Then the kernel moves its offset
to the end of the file before every write, so after truncation it writes at offset 0.
A writer without `O_APPEND` keeps its old offset and leaves a hole of zero bytes;
`get_log_info()` and `rotate_log()` warn about such writers.

### Lines written during rotation (catch-up)

Snapshot, compress and verify take a little time (≈200 ms for a 3 MiB log), and
the writer keeps appending. Truncating straight away would cut those lines off.
So just before `ftruncate()` the agent `fstat()`s the log again, reads only the new
tail with `pread()` and appends it to the verified archive as an **extra gzip
member** (gzip allows several members; `gunzip` outputs them in order). This
repeats until the log stops growing (at most `MAX_CATCHUP_ROUNDS`). If appending
fails, the archive is rolled back with `ftruncate()` to its last verified size.

The result reports `caught_up_bytes` and `bytes_lost` (bytes that arrived in the
microseconds between the final check and `ftruncate()`, normally 0). In a live test
with `writer.py` at 500 lines/s, every line number appears exactly once across the
archive and the live log.

### Failure safety

| Failure | Log truncated? | Archive |
|---|---|---|
| log missing / outside `logs/` / symlink / no permission | no | none created |
| snapshot fails | no | none (snapshot removed) |
| compression fails (e.g. disk full) | no | none (temp file removed) |
| verification fails (corrupt / wrong content) | no | **removed** (not trustworthy) |
| truncation fails | no | kept (it is verified) |
| post-rotation check fails | **yes** | kept, and its path is in the error result |

### Verifying the rotation itself

Success is only reported after `verify_rotation()` has re-checked the log **by its
path** (what users and new writers will open):

| Check | How | Catches |
|---|---|---|
| `exists`, `regular_file` | `lstat()` | log deleted, or replaced by a symlink/directory |
| `same_inode` | `(st_dev, st_ino)` before == after | delete + recreate, `mv` + create |
| `not_deleted` | `st_nlink >= 1` | the inode lost its name |
| `mode_preserved`, `owner_preserved` | `st_mode`, `st_uid`, `st_gid` | permissions changed underneath |
| `appendable` | `open(O_WRONLY \| O_APPEND)` + `write(b"")` | log no longer writable (adds no data) |
| `writers_attached` | `/proc/<pid>/fd` of every writer seen before | a writer died or holds a different inode |

If a check fails after truncation the result is `ROTATION_VERIFY_FAILED` (never
"success") with `truncated: true` and the archive path, which then holds the only
copy of the old data.

With `rotate_log(..., watch_writer=3)` the agent also samples the log for up to
3 seconds and reports `writer_continues: true` once the same inode grows again. A
writer that is merely idle only adds a warning.

Every failure before truncation returns `"status": "error"`, an `error_code`,
`"truncated": false` and `"log_unchanged": true`.

### Example result

```json
{
  "status": "success", "action": "rotate", "dry_run": false,
  "log": "/home/user/Log-Rotator-Agent/logs/apache_error.log",
  "inode_before": 1234567, "inode_after": 1234567, "inode_preserved": true,
  "original_size": 3186979, "size_after": 171,
  "archive": "/home/user/Log-Rotator-Agent/rotated_logs/apache_error.log.2026-10-07T160131.gz",
  "compressed": true, "archive_size": 295100, "compression_ratio": 0.0925,
  "bytes_archived": 3191349, "caught_up_bytes": 4370, "bytes_lost": 0,
  "truncated": true,
  "open_by": [{"pid": 456, "command": "python3 writer.py", "fd": 3, "access": "write", "append": true, "offset": 3191349}],
  "warnings": [],
  "steps": [
    {"step": "identify", "ok": true, "ms": 1.98},   {"step": "open", "ok": true, "ms": 3.1},
    {"step": "snapshot", "ok": true, "ms": 28.87},  {"step": "compress", "ok": true, "ms": 52.66},
    {"step": "verify_archive", "ok": true, "ms": 9.78},
    {"step": "catch_up", "ok": true, "ms": 2.14, "bytes": 4370, "rounds": 1},
    {"step": "truncate", "ok": true, "ms": 1.55}
  ]
}
```

`size_after_truncate` is the size returned by `ftruncate()` itself (always 0).
`size_after`, measured at the end, can already be above 0: the writer continued
writing the moment the log was emptied. Successful results also contain
`rotation_checks` (all `true`), and with `watch_writer` also `writer_continues`
and `growth` samples.

## Demo: the inode stays the same

```bash
python3 demo/inode_demo.py --dir ~/rotator-demo --compare
```

`--dir` puts the demo logs on the Linux filesystem, which gives short inode numbers
(on `/mnt/c` they are long NTFS ids). Real output:

```
[2] Before rotation
  BEFORE   inode = 739                  size =   10.0 MiB   writer pid 500: running

[3] Rotating: snapshot -> gzip -> verify -> catch up -> ftruncate(fd, 0) -> verify
      identify         ok         0.30 ms
      open             ok         0.18 ms
      snapshot         ok        39.19 ms
      compress         ok       157.36 ms
      verify_archive   ok        47.53 ms
      catch_up         ok         0.40 ms
      truncate         ok         3.35 ms
      verify_rotation  ok         1.35 ms
      watch_writer     ok       100.44 ms

[4] After rotation
  AFTER    inode = 739                  size =        0 B   (at ftruncate; inode preserved: True)
  +100  ms inode = 739                  size =    2.9 KiB
  +1s      inode = 739                  size =   33.1 KiB   writer pid 500: running

[X] The WRONG way for comparison: rm + create  (wrong_way.log)
  BEFORE   inode = 30                   size =    1.0 MiB   writer pid 434: running
  +1s      inode = 31                   size =        0 B   writer pid 434: running
  writer's fd 3 -> /tmp/rotator-demo/logs/wrong_way.log (deleted)
```

Same inode, size 10 MiB → 0 → growing, writer never restarted. With rm + create
the name gets a new inode that stays empty while the writer keeps writing into
the deleted one.

## Simulated writer

```bash
python3 writer.py                          # append to logs/apache_error.log, 20 lines/s
python3 writer.py --prefill-mb 50          # first create a 50 MiB log to rotate
python3 writer.py --no-append              # open WITHOUT O_APPEND (shows the sparse-file problem)
```

Each status line shows the descriptor's offset, the file size, the inode and
the link count, so you can see from the writer's own terminal what happens
to its file during rotation.

## Running the tests

```bash
python3 -m unittest discover -v
```

## Development phases

1. Project structure and simulated log writer
2. Log discovery, metadata and path validation
3. Snapshot and compression
4. Archive verification
5. Safe `ftruncate`-based rotation
6. Inode preservation and rotation verification
7. File locking for concurrent rotation
8. Agent tool interface and CLI
9. Natural-language command handling
10. Concurrency, writer and permission tests
11. Documentation and demo guide
