#!/usr/bin/env python3
"""Remote deploy helper over SSH/SFTP (paramiko).

Windows/Git-Bash friendly: no sshpass/rsync needed.

Usage:
  python remote.py probe
  python remote.py exec "uname -a"
  python remote.py put <local> <remote> [--resume]
  python remote.py load <remote.tar.gz> [more...]
  python remote.py up <remote_dir> <env_file>
  python remote.py ps <remote_dir> <env_file>
  python remote.py logs <remote_dir> <env_file> <service> [lines]

Connection settings are resolved in this order (first hit wins):
  1. environment variables: HOST / USER / PASSWORD / PORT
  2. a local, git-ignored file next to this script: .remote.env
     (copy .remote.env.example and fill it in)

No credentials are hard-coded here on purpose: this file is committed to git.
"""
import argparse
import os
import posixpath
import stat
import sys
import time

import paramiko

_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".remote.env")


def _load_local_conf(path=_CONF):
    """Load defaults from a git-ignored KEY=VALUE file next to this script."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_local_conf()

HOST = os.environ.get("HOST", "")
USER = os.environ.get("USER", "root")
PASSWORD = os.environ.get("PASSWORD", "")
PORT = int(os.environ.get("PORT", "22"))


def connect():
    if not HOST or not PASSWORD:
        sys.exit(
            "Missing connection settings: HOST / PASSWORD are not set.\n"
            "Export them, or create a git-ignored config file at:\n"
            f"  {_CONF}\n"
            "with contents like:\n"
            "  HOST=<server-ip>\n"
            "  USER=root\n"
            "  PASSWORD=<secret>\n"
            "  PORT=22"
        )
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(
        HOST,
        port=PORT,
        username=USER,
        password=PASSWORD,
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return cli


def run(cli, cmd, timeout=300, quiet=False):
    if not quiet:
        print(f"\n$ {cmd}", flush=True)
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=False)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(err, file=sys.stderr, end="" if err.endswith("\n") else "\n")
    if code != 0 and not quiet:
        print(f"[exit {code}]", file=sys.stderr)
    return code, out, err


def cmd_probe(cli, args):
    checks = [
        ("system", "uname -m; uname -r; nproc; cat /etc/os-release | head -3"),
        ("memory", "free -g; echo; free -m | head -2"),
        ("disk", "df -h / /var/lib/docker /opt 2>/dev/null"),
        ("ports", "ss -tlnp 2>/dev/null | sort -k4 | head -60"),
        ("docker_ps", "docker ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Ports}}\\t{{.Status}}' 2>&1 | head -60"),
        ("docker_net", "docker network ls 2>&1"),
        ("docker_vol", "docker volume ls 2>&1"),
        ("docker_ver", "docker --version; docker compose version 2>&1 | head -2"),
        ("opt", "ls -la /opt 2>&1"),
        ("port_check", "for p in 18100 18300 19300 5432; do (ss -tln 2>/dev/null | grep -q \":$p \" && echo \"PORT $p OCCUPIED\") || echo \"PORT $p FREE\"; done"),
        ("tools", "which curl tar gzip openssl sha256sum ss 2>&1"),
    ]
    for name, cmd in checks:
        print(f"\n===== {name} =====", flush=True)
        run(cli, cmd, quiet=True, timeout=60)


def cmd_exec(cli, args):
    code, _, _ = run(cli, args.cmd, timeout=args.timeout, quiet=args.quiet)
    return code


def _progress(name, done, total, start):
    pct = done * 100.0 / total if total else 0
    speed = done / max(time.time() - start, 0.001) / 1024 / 1024
    sys.stdout.write(f"\r  {name}: {pct:5.1f}%  {done/1024/1024:8.1f}/{total/1024/1024:.1f} MB  {speed:6.1f} MB/s")
    sys.stdout.flush()


def cmd_put(cli, args):
    local = args.local
    remote = args.remote
    sftp = cli.open_sftp()
    rdir = posixpath.dirname(remote)
    if rdir:
        try:
            sftp.stat(rdir)
        except IOError:
            _mkdirs(sftp, rdir)
    size = os.path.getsize(local)
    offset = 0
    if args.resume:
        try:
            offset = sftp.stat(remote).st_size
        except IOError:
            offset = 0
    if offset and offset >= size:
        print(f"  {remote} already complete ({size} bytes); skip")
        sftp.close()
        return
    mode = "ab" if offset else "wb"
    start = time.time()
    done = offset
    with open(local, "rb") as f, sftp.open(remote, mode) as rf:
        if offset:
            print(f"  resume from {offset} bytes")
            f.seek(offset)
            rf.seek(offset)
        rf.set_pipelined(True)
        while True:
            chunk = f.read(1024 * 512)
            if not chunk:
                break
            rf.write(chunk)
            done += len(chunk)
            if (done - offset) % (1024 * 1024 * 4) < 512 * 1024:
                _progress(os.path.basename(local), done, size, start)
    _progress(os.path.basename(local), done, size, start)
    print()
    rsize = sftp.stat(remote).st_size
    ok = rsize == size
    print(f"  uploaded {remote}: local={size} remote={rsize} {'OK' if ok else 'MISMATCH!'}")
    sftp.close()
    if not ok:
        sys.exit(2)


def _mkdirs(sftp, path):
    parts = []
    p = path
    while p and p != "/":
        parts.append(p)
        p = posixpath.dirname(p)
    for d in reversed(parts):
        try:
            sftp.stat(d)
        except IOError:
            sftp.mkdir(d)


def _compose(cli, rdir, env_file, sub):
    return run(cli, f"cd {rdir} && docker compose --env-file {env_file} {sub}", timeout=600)


def cmd_load(cli, args):
    for f in args.files:
        run(cli, f"gzip -dc {f} | docker load", timeout=1200)


def cmd_up(cli, args):
    _compose(cli, args.rdir, args.env_file, "up -d")


def cmd_ps(cli, args):
    _compose(cli, args.rdir, args.env_file, "ps")


def cmd_logs(cli, args):
    _compose(cli, args.rdir, args.env_file, f"logs --tail={args.lines} {args.service}")


def cmd_mkdir(cli, args):
    run(cli, f"mkdir -p {args.path} && ls -ld {args.path}")


def main():
    global HOST, USER, PASSWORD, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--user", default=USER)
    ap.add_argument("--password", default=PASSWORD)
    ap.add_argument("--port", type=int, default=PORT)
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("probe")
    p = sub.add_parser("exec")
    p.add_argument("cmd")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("-q", "--quiet", action="store_true",
                   help="do not echo the command or 'exit N'; only remote stdout is printed")

    p = sub.add_parser("put")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--resume", action="store_true")

    p = sub.add_parser("load")
    p.add_argument("files", nargs="+")

    p = sub.add_parser("mkdir")
    p.add_argument("path")

    p = sub.add_parser("up")
    p.add_argument("rdir")
    p.add_argument("env_file")

    p = sub.add_parser("ps")
    p.add_argument("rdir")
    p.add_argument("env_file")

    p = sub.add_parser("logs")
    p.add_argument("rdir")
    p.add_argument("env_file")
    p.add_argument("service")
    p.add_argument("lines", nargs="?", default="200")

    args = ap.parse_args()

    HOST, USER, PASSWORD, PORT = args.host, args.user, args.password, args.port

    cli = connect()
    code = 0
    try:
        code = {
            "probe": cmd_probe,
            "exec": cmd_exec,
            "put": cmd_put,
            "load": cmd_load,
            "up": cmd_up,
            "ps": cmd_ps,
            "logs": cmd_logs,
            "mkdir": cmd_mkdir,
        }[args.action](cli, args) or 0
    finally:
        cli.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
