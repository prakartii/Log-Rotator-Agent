# Log-Rotator-Agent

Agent **#15** of our class-wide multi-agent Operating Systems project.

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
log_rotator/
    config.py            # directories, allowed roots, log aliases
    errors.py            # stable error codes + structured success/error results
    tools/
        safety.py        # path validation and safe open (O_NOFOLLOW + inode check)
        log_info.py      # identify_log(), list_logs(), get_log_info()
        processes.py     # which processes have a log open (/proc/<pid>/fd, fdinfo)
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

### Safety rules

The agent refuses to touch a file when:

- it is outside the allowed log directories (blocks `../../etc/passwd`, `/var/log/syslog`)
- it is a symbolic link, or a directory, FIFO, socket or device
- it has more than one hard link (another name for the same inode could live elsewhere)
- the agent lacks read/write permission
- it was replaced between validation and `open()` (TOCTOU race)

Error codes: `INVALID_REQUEST`, `LOG_NOT_FOUND`, `AMBIGUOUS_LOG`,
`NOT_A_REGULAR_FILE`, `OUTSIDE_ALLOWED_DIR`, `SYMLINK_REJECTED`,
`HARDLINK_REJECTED`, `FILE_CHANGED`, `PERMISSION_DENIED`, `OS_ERROR`.

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
