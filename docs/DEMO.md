# Demo guide: Log Rotator Agent

A 10-minute live demo for the class presentation. It shows the one idea the
project is built on: **a log can be emptied while a process is still writing
to it, without losing the file, the inode or the writer.**

## Before the demo

- Ubuntu or WSL2, Python 3.9+, no extra packages.
- Work on the Linux filesystem (`~`), not `/mnt/c`: inode numbers are short and
  `chmod` works there.
- Open **three terminals** in the project folder:

| Terminal | Role |
|---|---|
| 1 | the writer (plays Apache) |
| 2 | the agent |
| 3 | watching the file (`watch`, `ls -li`, `/proc`) |

Reset between rehearsals:

```bash
rm -f logs/*.log rotated_logs/*.gz
```

## 1. Start a writer (terminal 1)

```bash
python3 writer.py --prefill-mb 20 --rate 50 --cooperative
```

Point out the startup line: `pid`, `fd`, `flags=O_WRONLY|O_CREAT|O_APPEND`. The
process opens the log **once** and keeps that descriptor for its whole life.

## 2. Look at the log (terminal 2)

```bash
python3 agent.py list
python3 agent.py info "the apache error logs"
python3 agent.py who apache_error
```

Point out: inode number, size around 20 MiB, and the writer's pid, fd, `O_APPEND`
and offset, read from `/proc/<pid>/fd` and `/proc/<pid>/fdinfo`.

Terminal 3, the same facts from the shell:

```bash
ls -li logs/apache_error.log
ls -l /proc/$(pgrep -f writer.py)/fd
```

## 3. Ask first, change nothing

```bash
python3 agent.py ask "what would happen if you rotated the apache error logs?"
```

`Understood: rotate_log(log='apache error', dry_run=True)`: the agent says which
tool it chose, and a dry run changes nothing.

## 4. Rotate (terminal 2)

```bash
python3 agent.py ask "compress last month's apache error log and make sure the writer keeps running"
```

Point out in the result:

- `archive:` the `.gz` file named with the label `2026-09` (last month)
- `inode: N -> N (preserved)`: same inode before and after
- `caught up ... lost 0`: lines written *during* rotation went into the archive too
- `writer continues: yes`: the log is growing again

Terminal 1: the writer never stopped. Its status lines now show a small `size`
(it restarted at offset 0 because of `O_APPEND`) and the **same inode**.

Terminal 3:

```bash
ls -li logs/apache_error.log          # same inode, small size
python3 agent.py archives              # the new archive
zcat rotated_logs/*.gz | tail -3       # the old lines are safe
```

## 5. The wrong way, for comparison

```bash
python3 demo/inode_demo.py --dir ~/rotator-demo --compare
```

Read from the output: with `rm` + create, the name gets a **new** inode that stays
empty, and `/proc/<pid>/fd/3 -> ... (deleted)`. The writer keeps writing into a file
nobody can see, and the disk space is not freed.

## 6. Safety (terminal 2)

```bash
python3 agent.py rotate /etc/passwd                 # OUTSIDE_ALLOWED_DIR
python3 agent.py ask "delete the apache error log"  # the agent never deletes logs
python3 agent.py ask "rotate everything"            # one log at a time, lists the logs
ln -s /etc/hostname logs/evil.log
python3 agent.py rotate evil.log                    # SYMLINK_REJECTED
rm logs/evil.log
```

Concurrency: run two rotations at once; the second is refused with
`ROTATION_IN_PROGRESS` and names the pid holding the lock:

```bash
python3 agent.py rotate apache_error & python3 agent.py rotate apache_error; wait
```

## 7. For the master agent

```bash
python3 agent.py tools                                             # JSON schemas
python3 agent.py --json call rotate_log '{"log": "apache error", "dry_run": true}'
echo $?                                                            # 0 ok, 1 error, 2 usage
```

## Questions to expect

| Question | Short answer |
|---|---|
| Why not `rm` and recreate? | The writer's fd points at the inode, not the name; its lines would go into a deleted file. |
| Why does the writer continue at offset 0? | `O_APPEND`: the kernel seeks to EOF before every `write()`. |
| What about a writer without `O_APPEND`? | It keeps its old offset and leaves a hole of zeros; the agent warns about it. |
| Can a line be lost? | Not with cooperative (`flock`) writers. Other writers can lose writes from the microseconds before `ftruncate()`; `bytes_lost` reports a lower bound. |
| What if the agent crashes? | Before `ftruncate()` the log is untouched; locks are released by the kernel; the next run cleans up temp files. At worst lines are archived twice, never lost. |
| Why rules instead of an LLM for requests? | A misread request could truncate the wrong log; rules are predictable and tested, and every result says how the request was understood. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PERMISSION_DENIED` on `/mnt/c/...` | run from `~` on the Linux filesystem |
| `ROTATION_IN_PROGRESS` | another rotation is running; add `--lock-timeout 10` |
| `who` shows no process | the writer is not running, or runs as another user |
| very long inode numbers | you are on `/mnt/c`; use `--dir ~/rotator-demo` for the demo script |
