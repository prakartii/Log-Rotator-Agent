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
- Python 3.8+ (standard library only, no extra packages needed)

## Project structure

```
agent.py                 # command-line entry point (later phase)
writer.py                # simulated long-running process that appends to a log
log_rotator/
    config.py            # directories, allowed roots, log aliases
    tools/               # controlled OS-level tools (snapshot, compress, truncate, ...)
logs/                    # active demo logs (contents git-ignored)
rotated_logs/            # compressed archives (contents git-ignored)
tests/                   # unittest test suite
```

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
