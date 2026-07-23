"""Console entry point: `wherewent run [--save PATH] <command...>`.

Launches the target command in a subprocess whose PYTHONPATH is arranged so
Python auto-imports our sitecustomize shim, which installs the recorder before
the job's own code runs. Zero changes to the job itself.
"""

import os
import signal
import subprocess
import sys

USAGE = (
    "usage: wherewent run [--save PATH] <command...>\n"
    "  e.g. wherewent run python job.py\n"
    "       wherewent run --save out.json python -m mypkg\n"
    "       wherewent run job.py            (bare script uses this interpreter)\n"
)


def _usage():
    sys.stderr.write(USAGE)


def _child_env(save):
    import wherewent
    import wherewent._shim as shim

    # Directory that literally contains sitecustomize.py (so Python auto-imports it).
    shim_dir = os.path.dirname(os.path.abspath(shim.__file__))
    # Directory that contains the `wherewent` package, so the child can import it.
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(wherewent.__file__)))

    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    parts = [shim_dir, pkg_parent]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["WHEREWENT_ACTIVE"] = "1"
    env["WHEREWENT_SAVE"] = save or ""
    return env


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "run":
        _usage()
        return 2

    args = argv[1:]
    save = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--save":
            if i + 1 >= len(args):
                _usage()
                return 2
            save = args[i + 1]
            i += 2
            continue
        break  # first non-option token starts the command
    command = args[i:]

    if not command:
        _usage()
        return 2

    # A bare `script.py` runs under this interpreter.
    if command[0].endswith(".py"):
        command = [sys.executable] + command

    env = _child_env(save)

    # While the child runs, the parent ignores Ctrl-C so the SIGINT goes to the
    # child (which finalizes and prints the report); we exit with its code.
    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        proc = subprocess.run(command, env=env)
    except FileNotFoundError:
        sys.stderr.write(f"wherewent: command not found: {command[0]}\n")
        return 127
    finally:
        signal.signal(signal.SIGINT, prev)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
