#!/usr/bin/env python3
"""daemonize.py <pidfile> <command...>

Run <command> as a fully detached daemon: double-fork + os.setsid(), so it
reparents to init/launchd (ppid 1) and survives both the launching process and the
dispatch tick ending. Writes the daemon's PID to <pidfile>, runs the command to
completion, then removes the pidfile.

`setsid` the CLI is absent on macOS; this uses the os.setsid() syscall instead.
"""
import os
import subprocess
import sys


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: daemonize.py <pidfile> <command...>")
    pidfile, command = sys.argv[1], sys.argv[2:]

    # double-fork to detach from the caller's session and process group
    if os.fork() > 0:
        os._exit(0)          # original process returns to the caller immediately
    os.setsid()
    if os.fork() > 0:
        os._exit(0)          # session leader exits; grandchild reparents to launchd

    # detach stdio: reopen stdin from /dev/null and send stdout/stderr to a log
    # beside the pidfile. Without this the daemon inherits the launcher's pipe and
    # dies with EPIPE the moment that pipe closes (i.e. as soon as the tick ends).
    logpath = (pidfile[:-4] if pidfile.endswith(".pid") else pidfile) + ".log"
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
    logfd = os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)

    with open(pidfile, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        subprocess.run(command)
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


if __name__ == "__main__":
    main()
