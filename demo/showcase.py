#!/usr/bin/env python3
"""Log Rotator Agent: a live run against a sandbox log, for presentations and screen recordings.

    python3 demo/showcase.py                  # presenter mode: press Enter between steps
    python3 demo/showcase.py --auto           # runs by itself, short pauses (no prompts)
    python3 demo/showcase.py --dir ~/demo     # where the demo logs live (default: ~/log-rotator-demo)

Every step types a real command and runs it for real, against a sandbox log that a
live writer process keeps appending to. Nothing outside the sandbox is touched.
Needs Linux or WSL2 (flock, ftruncate and /proc).
"""

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable or "python3"


class Showcase:
    def __init__(self, directory, auto=False, fast=False, delay=2.0, prefill_mb=20.0):
        self.dir = Path(directory).expanduser().resolve()
        self.auto, self.fast, self.delay, self.prefill_mb = auto, fast, delay, prefill_mb
        self.logs, self.archives = self.dir / "logs", self.dir / "rotated_logs"
        self.env = {**os.environ, "LOG_ROTATOR_LOG_DIR": str(self.logs),
                    "LOG_ROTATOR_ARCHIVE_DIR": str(self.archives), "PYTHONUNBUFFERED": "1"}
        self.writer = None
        self.color = sys.stdout.isatty()

    # ----- presentation helpers ------------------------------------------------------
    def paint(self, text, code):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def banner(self, number, title):
        line = "=" * 74
        print(f"\n{self.paint(line, '36')}\n{self.paint(f'  STEP {number}: {title}', '1;36')}\n"
              f"{self.paint(line, '36')}")

    def say(self, text):
        print(self.paint(f"  {text}", "33"))

    def note(self, text):
        print(self.paint(f"  {text}", "2"))

    def pause(self):
        if self.auto:
            time.sleep(self.delay)
        else:
            input(self.paint("\n  [Enter] to continue ", "2"))

    def type_out(self, text):
        """Print the command as if typed, so a viewer can read it before it runs."""
        print(self.paint("$ ", "1;32"), end="", flush=True)
        for char in text:
            print(self.paint(char, "32"), end="", flush=True)
            if not self.fast:
                time.sleep(0.012)
        print(flush=True)
        time.sleep(0.1 if self.fast else 0.4)

    def run(self, display, argv=None, shell=False, cwd=ROOT, stdin=None):
        """Type `display`, run it (argv or a bash line) and return its exit status."""
        self.type_out(display)
        command = display if shell else argv
        done = subprocess.run(command, shell=shell, cwd=cwd, env=self.env, stdin=stdin,
                              executable="/bin/bash" if shell else None)
        return done.returncode

    def agent(self, *args, confirm=False):
        """Run `python3 agent.py ...`. With confirm the presenter answers Proceed? [y/N] by hand."""
        argv = [PY, "agent.py", *args]
        if not confirm and "--yes" not in args:
            argv.insert(2, "--yes")  # reads never ask; this only keeps scripted runs prompt-free
        display = "python3 agent.py " + " ".join(shlex.quote(a) for a in args)
        if self.auto and confirm:
            argv.insert(2, "--yes")
        return self.run(display, argv)

    # ----- the writer ------------------------------------------------------------------
    def start_writer(self):
        self.logs.mkdir(parents=True, exist_ok=True)
        self.archives.mkdir(parents=True, exist_ok=True)
        out = open(self.dir / "writer.out", "w")
        self.writer = subprocess.Popen(
            [PY, str(ROOT / "writer.py"), "--prefill-mb", str(self.prefill_mb), "--rate", "50",
             "--cooperative"], stdout=out, stderr=subprocess.STDOUT, env=self.env, cwd=ROOT)
        log = self.logs / "apache_error.log"
        deadline = time.monotonic() + 60
        while not log.exists() or log.stat().st_size < self.prefill_mb * 1024 * 1024:
            if self.writer.poll() is not None or time.monotonic() > deadline:
                raise SystemExit("the demo writer did not start; see " + str(self.dir / "writer.out"))
            time.sleep(0.1)

    def stop_writer(self):
        if self.writer and self.writer.poll() is None:
            self.writer.terminate()
            try:
                self.writer.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.writer.kill()

    # ----- steps ---------------------------------------------------------------------------
    def title(self):
        print()
        print(self.paint("  LOG ROTATOR AGENT", "1;37"))
        print(self.paint("  Safe log rotation with ftruncate(): the writer is never interrupted", "2"))

    def step_setup(self):
        self.title()
        self.banner(1, "A server keeps writing to its log")
        self.say("The writer plays Apache: it opens the log ONCE and appends to it forever.")
        self.say(f"Sandbox: {self.dir}")
        self.start_writer()
        self.run("head -2 writer.out", cwd=self.dir, shell=True)
        self.say(f"Writer pid {self.writer.pid} is running with a {self.prefill_mb:.0f} MiB log.")

    def step_inspect(self):
        self.banner(2, "Look at the log before touching it")
        self.say("The agent reads the log's inode, size and who has it open (like lsof, via /proc).")
        self.agent("list")
        self.agent("info", "the apache error logs")
        self.agent("who", "apache_error")
        self.say("Remember the inode number: it must not change.")

    def step_rotate(self):
        self.banner(3, "Ask in plain English")
        self.say("First a dry run: the agent says which tool it chose and changes nothing.")
        self.agent("ask", "what would happen if you rotated the apache error logs?")
        self.pause()
        self.say("Now the real request. The agent shows the plan and asks before emptying the log.")
        if not self.auto:
            self.note("Type y at Proceed? [y/N]")
        self.agent("ask", "Zip up the apache error logs from last month and clear the current file.",
                   confirm=True)

    def step_proof(self):
        self.banner(4, "Proof from the operating system")
        self.say("ls shows the same inode, a small live log, and the new archive.")
        self.run("ls -li logs/apache_error.log rotated_logs/", cwd=self.dir, shell=True)
        archive = sorted(self.archives.glob("apache_error.log.2026-09.*.gz"))[-1].name
        self.say("The archive is a valid gzip file that holds the old contents:")
        self.run(f"gzip -t rotated_logs/{archive} && echo 'archive OK' && zcat rotated_logs/{archive} | wc -l",
                 cwd=self.dir, shell=True)
        self.say("A moment later the log has grown again: the writer never noticed the rotation.")
        time.sleep(1 if self.fast else 3)
        self.run("ls -li logs/apache_error.log", cwd=self.dir, shell=True)
        self.run("tail -2 logs/apache_error.log", cwd=self.dir, shell=True)

    def step_safety(self):
        self.banner(5, "Dangerous requests are stopped")
        self.say("The agent has no delete tool, and only touches files inside the log directory.")
        self.agent("ask", "delete everything on my drive")
        self.agent("rotate", "/etc/passwd")
        self.agent("ask", "rotate everything")
        self.say("None of these reached the file system.")

    def step_interfaces(self):
        self.banner(6, "One agent, three ways to use it")
        self.say("1. The interactive shell (type requests live with: python3 agent.py shell)")
        self.run("printf 'list the logs\\nshow the archives\\nexit\\n' | python3 agent.py --yes shell",
                 shell=True)
        self.say("2. The tool list the master agent and Claude choose from:")
        self.run("python3 agent.py tools | grep '\"name\"'", shell=True)
        self.say("3. The same tools as an MCP server, for the class master shell:")
        self.run("printf '%s\\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\"}' "
                 "'{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":"
                 "{\"name\":\"list_logs\"}}' | python3 agent.py mcp | cut -c1-200", shell=True)

    def step_finish(self):
        self.banner("END", "Summary")
        alive = self.writer.poll() is None
        print(f"  writer still running after the rotation: {'yes' if alive else 'NO'}")
        self.stop_writer()
        self.say("Demo finished. The writer has been stopped; the sandbox is kept for inspection:")
        self.note(str(self.dir))

    def steps(self):
        return [self.step_setup, self.step_inspect, self.step_rotate, self.step_proof,
                self.step_safety, self.step_interfaces]

    def play(self):
        try:
            for step in self.steps():
                step()
                self.pause()
            self.step_finish()
        finally:
            self.stop_writer()
        return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Log Rotator Agent: live run against a sandbox log")
    parser.add_argument("--dir", default="~/log-rotator-demo", help="sandbox directory for the demo logs")
    parser.add_argument("--auto", action="store_true", help="run without waiting for Enter")
    parser.add_argument("--fast", action="store_true", help="no typing effect")
    parser.add_argument("--delay", type=float, default=2.0, help="seconds between steps with --auto")
    parser.add_argument("--prefill-mb", type=float, default=20.0, help="size of the log before the demo")
    args = parser.parse_args(argv)
    if not Path("/proc").exists():
        raise SystemExit("the demo needs Linux or WSL2 (flock, ftruncate and /proc)")
    return Showcase(args.dir, args.auto, args.fast, args.delay, args.prefill_mb).play()


if __name__ == "__main__":
    sys.exit(main())
