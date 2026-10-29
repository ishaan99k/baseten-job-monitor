#!/usr/bin/env python3
# <swiftbar.type>streamable</swiftbar.type>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>

"""SwiftBar streaming plugin: Baseten training jobs, AWS EC2 dev boxes, and
this Mac, all in one menu bar item.

This plugin stays running. Three background threads feed one menu:
  * Baseten: `truss train view` once a minute.
  * AWS: EC2 instance state once a minute, plus one SSH session per running
    box that reports CPU, RAM, disk and GPU usage.
  * Local: `top` / `ps` on this Mac.
Usage sampling and the menu redraw share one interval: 10 seconds by default,
selectable from the dropdown (2 seconds gives an htop-like live view). Click
any section header (a job group, a box, or the Mac) to pin it to the menu bar.

Security model:
  * The plugin never handles AWS credentials. Every AWS call goes through the
    `aws` CLI, which uses the user's own sign-in (SSO / `aws login`, named
    profiles, credential_process, ...). Nothing secret is written anywhere by
    this script.
  * The only per-user config is a small KEY=VALUE file that says which profile,
    region, and instances to show, plus the SSH user and key *path*. It is
    parsed as plain text (never sourced or executed), must be owned by the
    user with mode 0600, and may not contain credential-looking keys.
  * Usage metrics are collected over the user's own SSH access, in batch mode
    (never prompts), by piping a short fixed script to `sh` on the box.
  * Every subprocess call uses an argument list (no shell), and instance IDs /
    names from config are validated against strict patterns.
"""

from __future__ import annotations

import base64
import configparser
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime

_extra_paths = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.local/share/mise/shims"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]
os.environ["PATH"] = (
    ":".join(p for p in _extra_paths if os.path.isdir(p))
    + ":"
    + os.environ.get("PATH", "")
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/baseten-job-monitor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "aws.env")

ALLOWED_KEYS = {
    "AWS_PROFILE",
    "AWS_REGION",
    "BTMON_DEV_BOXES",
    "BTMON_SSH_USER",
    "BTMON_SSH_KEY",
    "BTMON_PINNED",   # instance ID / Name tag of a box, or "baseten" / "local"
    "BTMON_USAGE_INTERVAL",  # seconds between usage samples (and menu redraws)
}
# Credentials belong to the AWS CLI's own credential chain, never to this file.
SPECIAL_PINS = {"baseten", "local"}   # non-EC2 things that can sit in the menu bar

FORBIDDEN_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
}

INSTANCE_ID_RE = re.compile(r"i-[0-9a-f]{8,17}")
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9._\-]{1,128}")
SSH_USER_RE = re.compile(r"[A-Za-z0-9._\-]{1,32}")
HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,252}[A-Za-z0-9])?")
PROFILE_RE = re.compile(r"[A-Za-z0-9._\-]{1,64}")
REGION_RE = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")

AWS_TIMEOUT = 25           # seconds for a single aws CLI call
EC2_POLL_INTERVAL = 60     # seconds between EC2 state polls
EC2_RETRY_INTERVAL = 10    # faster re-check while signed out or erroring, so sign-in shows up quickly
# How often usage (CPU/RAM/GPU/processes) is sampled on each box and on this
# Mac, and how often the menu redraws. Chosen from the dropdown; stored as
# BTMON_USAGE_INTERVAL. 10s is a light touch on the boxes; pick 2s when you
# want an htop-like live view.
USAGE_INTERVAL_CHOICES = (2, 5, 10, 30, 60)
DEFAULT_USAGE_INTERVAL = 10


def stale_after(interval: int) -> float:
    """Seconds without a sample before usage is marked stale."""
    return max(10.0, interval * 2.5)
SSH_RETRY_DELAY = 6        # seconds before reconnecting a dropped SSH session


def _secure_file_problem(path: str) -> str:
    """Return why a file is unsafe to trust ('' if fine)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ""
    if st.st_uid != os.getuid():
        return "not owned by you"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"permissions are {oct(st.st_mode & 0o777)}, must be 0600"
    return ""


def load_config() -> dict:
    """Load dev box settings. Environment variables override the file."""
    cfg = {
        "profile": "",
        "region": "",
        "boxes": [],
        "ssh_user": "",
        "ssh_key": "",
        "pinned": "",
        "usage_interval": DEFAULT_USAGE_INTERVAL,
        "configured": False,
        "error": "",
        "warnings": [],
    }
    values: dict[str, str] = {}

    if os.path.exists(CONFIG_PATH):
        problem = _secure_file_problem(CONFIG_PATH)
        if problem:
            cfg["error"] = f"Refusing to read {CONFIG_PATH}: {problem}"
            return cfg
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip().upper()
                    val = val.split(" #", 1)[0].strip().strip("'\"")
                    if key in FORBIDDEN_KEYS:
                        cfg["error"] = (
                            f"{key} found in aws.env (line {lineno}). Credentials must not be "
                            "stored here; sign in with the AWS CLI instead."
                        )
                        return cfg
                    if key in ALLOWED_KEYS:
                        values[key] = val
                    else:
                        cfg["warnings"].append(f"Ignoring unknown key {key} in aws.env")
        except OSError as exc:
            cfg["error"] = f"Could not read aws.env: {exc}"
            return cfg

    for key in ALLOWED_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]

    boxes_raw = values.get("BTMON_DEV_BOXES", "")
    if not boxes_raw:
        return cfg

    for item in boxes_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if INSTANCE_ID_RE.fullmatch(item) or INSTANCE_NAME_RE.fullmatch(item):
            cfg["boxes"].append(item)
        else:
            cfg["warnings"].append(f"Ignoring invalid dev box entry: {item[:40]!r}")
    if not cfg["boxes"]:
        cfg["error"] = "BTMON_DEV_BOXES has no valid instance IDs or names"
        return cfg

    profile = values.get("AWS_PROFILE", "").strip()
    if profile and not PROFILE_RE.fullmatch(profile):
        cfg["error"] = "AWS_PROFILE contains unexpected characters"
        return cfg
    region = values.get("AWS_REGION", "").strip()
    if region and not REGION_RE.fullmatch(region):
        cfg["error"] = f"AWS_REGION does not look like a region: {region!r}"
        return cfg
    ssh_user = values.get("BTMON_SSH_USER", "").strip()
    if ssh_user and not SSH_USER_RE.fullmatch(ssh_user):
        cfg["warnings"].append("Ignoring BTMON_SSH_USER: unexpected characters")
        ssh_user = ""

    # The key is only ever passed to `ssh -i`; the plugin never reads it.
    ssh_key = os.path.expanduser(values.get("BTMON_SSH_KEY", "").strip())
    if ssh_key:
        if not os.path.isfile(ssh_key):
            cfg["warnings"].append(f"BTMON_SSH_KEY not found: {ssh_key}")
            ssh_key = ""
        else:
            problem = _secure_file_problem(ssh_key)
            if problem:
                cfg["warnings"].append(f"SSH key {problem}; run chmod 600 on it")
                ssh_key = ""

    pinned = values.get("BTMON_PINNED", "").strip()
    if pinned and pinned not in SPECIAL_PINS and not (INSTANCE_ID_RE.fullmatch(pinned) or INSTANCE_NAME_RE.fullmatch(pinned)):
        cfg["warnings"].append("Ignoring BTMON_PINNED: unexpected characters")
        pinned = ""

    usage_interval = DEFAULT_USAGE_INTERVAL
    raw_interval = values.get("BTMON_USAGE_INTERVAL", "").strip()
    if raw_interval:
        if raw_interval.isdigit() and int(raw_interval) in USAGE_INTERVAL_CHOICES:
            usage_interval = int(raw_interval)
        else:
            cfg["warnings"].append(
                f"Ignoring BTMON_USAGE_INTERVAL={raw_interval[:10]}: use one of {', '.join(map(str, USAGE_INTERVAL_CHOICES))}"
            )

    cfg.update(
        profile=profile, region=region, ssh_user=ssh_user, ssh_key=ssh_key,
        pinned=pinned, usage_interval=usage_interval, configured=True,
    )
    return cfg


def write_config_key(key: str, value: str) -> str:
    """Set (or remove, when value is '') one key in aws.env, keeping the rest.

    Returns an error message or ''. Refuses to touch an unsafe file.
    """
    if key not in ALLOWED_KEYS:
        return f"Refusing to write unknown key {key}"
    problem = _secure_file_problem(CONFIG_PATH) if os.path.exists(CONFIG_PATH) else ""
    if problem:
        return f"Refusing to edit {CONFIG_PATH}: {problem}"
    lines: list[str] = []
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    kept = [
        ln for ln in lines
        if not (ln.strip() and not ln.lstrip().startswith("#")
                and ln.partition("=")[0].strip().upper() == key)
    ]
    if value:
        kept.append(f"{key}={value}")
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept).rstrip("\n") + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)
    return ""


# --------------------------------------------------------------------------
# AWS CLI
# --------------------------------------------------------------------------

def aws_base_cmd(cfg: dict) -> list[str]:
    cmd = ["aws", "--output", "json", "--no-cli-pager"]
    if cfg.get("profile"):
        cmd += ["--profile", cfg["profile"]]
    if cfg.get("region"):
        cmd += ["--region", cfg["region"]]
    return cmd


def _profile_keys(cfg: dict) -> tuple[str, set[str]]:
    """(profile name, keys present in its ~/.aws/config section)."""
    profile = cfg.get("profile") or os.environ.get("AWS_PROFILE") or "default"
    section = "default" if profile == "default" else f"profile {profile}"
    parser = configparser.RawConfigParser()
    try:
        parser.read(os.path.expanduser("~/.aws/config"), encoding="utf-8")
    except (configparser.Error, OSError):
        pass
    keys = set(parser[section].keys()) if parser.has_section(section) else set()
    return profile, keys


def profile_uses_sso(cfg: dict) -> bool:
    """True when the profile is wired to IAM Identity Center (an org start URL)."""
    _, keys = _profile_keys(cfg)
    return "sso_session" in keys or "sso_start_url" in keys


def aws_login_cmd(cfg: dict) -> list[str]:
    """Pick the sign-in command that matches the profile's config.

    IAM Identity Center (SSO) wins when present: `aws sso login` sends the
    browser to the org's own start URL, which lives in the user's
    ~/.aws/config and never in this plugin. Plain `aws login` only works
    when the user already has a console session, so it is the fallback.
    """
    profile, _ = _profile_keys(cfg)
    profile_args = ["--profile", profile] if cfg.get("profile") else []
    if profile_uses_sso(cfg):
        return ["aws", "sso", "login", *profile_args]
    return ["aws", "login", *profile_args]


def aws_configure_sso_cmd(cfg: dict) -> list[str]:
    """The AWS CLI's own wizard: asks for the org start URL and region, opens
    the browser, lets the user pick account and role, and writes the result
    into ~/.aws/config under this profile."""
    profile, _ = _profile_keys(cfg)
    return ["aws", "configure", "sso", "--profile", profile]


def _looks_like_auth_error(stderr: str) -> bool:
    s = stderr.lower()
    return any(n in s for n in (
        "token has expired", "expiredtoken", "unable to locate credentials",
        "sso session", "aws login", "aws sso login", "invalid grant",
        "refresh token", "could not refresh", "no credentials",
        "credentials were not found",
    ))


def _run_aws(cmd: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=AWS_TIMEOUT)
    except FileNotFoundError:
        return 127, "", "aws CLI not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"aws timed out after {AWS_TIMEOUT}s"
    return result.returncode, result.stdout, result.stderr


def _instance_from_json(inst: dict) -> dict:
    tags = {t.get("Key"): t.get("Value") for t in inst.get("Tags", []) or []}
    az = inst.get("Placement", {}).get("AvailabilityZone", "")
    region = az[:-1] if re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d[a-z]", az) else ""
    return {
        "id": inst.get("InstanceId", ""),
        "name": tags.get("Name", "") or inst.get("InstanceId", ""),
        "state": inst.get("State", {}).get("Name", "unknown"),
        "type": inst.get("InstanceType", ""),
        "public_ip": inst.get("PublicIpAddress", ""),
        "public_dns": inst.get("PublicDnsName", ""),
        "private_ip": inst.get("PrivateIpAddress", ""),
        "launch_time": inst.get("LaunchTime", ""),
        "az": az,
        "region": region,
        "status_checks": "",
    }


def fetch_dev_boxes(cfg: dict) -> dict:
    """Query EC2 for the configured boxes.

    Returns {"boxes": [...], "error": str, "auth_error": bool}.
    """
    out = {"boxes": [], "error": "", "auth_error": False}
    ids = [b for b in cfg["boxes"] if INSTANCE_ID_RE.fullmatch(b)]
    names = [b for b in cfg["boxes"] if not INSTANCE_ID_RE.fullmatch(b)]

    # Filters (not --instance-ids) so a stale ID does not fail the whole call.
    calls = []
    if ids:
        calls.append(["--filters", f"Name=instance-id,Values={','.join(ids)}"])
    if names:
        calls.append(["--filters", f"Name=tag:Name,Values={','.join(names)}"])

    found: dict[str, dict] = {}
    for extra in calls:
        code, stdout, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "describe-instances", *extra])
        if code != 0:
            lines = stderr.strip().splitlines()
            out["error"] = (lines[-1] if lines else f"aws exited {code}")[:200]
            out["auth_error"] = _looks_like_auth_error(stderr)
            return out
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            out["error"] = "Could not parse aws output"
            return out
        for res in data.get("Reservations", []):
            for inst in res.get("Instances", []):
                box = _instance_from_json(inst)
                if box["state"] != "terminated":
                    found[box["id"]] = box

    running_ids = [b["id"] for b in found.values() if b["state"] == "running"]
    if running_ids:
        code, stdout, _ = _run_aws(
            aws_base_cmd(cfg) + ["ec2", "describe-instance-status", "--instance-ids", *running_ids]
        )
        if code == 0:
            try:
                for st in json.loads(stdout or "{}").get("InstanceStatuses", []):
                    iid = st.get("InstanceId")
                    if iid in found:
                        sys_ok = st.get("SystemStatus", {}).get("Status", "")
                        inst_ok = st.get("InstanceStatus", {}).get("Status", "")
                        found[iid]["status_checks"] = f"system={sys_ok} instance={inst_ok}"
                        found[iid]["impaired"] = "impaired" in (sys_ok + inst_ok)
            except json.JSONDecodeError:
                pass

    ordered = []
    for entry in cfg["boxes"]:
        matches = [b for b in found.values() if entry in (b["id"], b["name"])]
        for m in matches:
            if m not in ordered:
                ordered.append(m)
        if not matches:
            ordered.append({"id": entry, "name": entry, "state": "not found", "missing": True})
    out["boxes"] = ordered
    return out


# --------------------------------------------------------------------------
# SSH usage metrics (streamed)
# --------------------------------------------------------------------------

# Fed to `sh` on the box via stdin. Loops forever, printing one frame per
# usage interval terminated by "@@END". The CPU figure is a real sample over
# the sleep window rather than a since-boot average.
_REMOTE_METRICS_LOOP = r"""
while :; do
  set -- $(head -1 /proc/stat); u1=$(( $2+$3+$4+$7+$8+$9 )); i1=$(( $5+$6 ))
  sleep %(sleep)s
  set -- $(head -1 /proc/stat); u2=$(( $2+$3+$4+$7+$8+$9 )); i2=$(( $5+$6 ))
  t=$(( (u2-u1)+(i2-i1) )); if [ "$t" -gt 0 ]; then cpu=$(( 100*(u2-u1)/t )); else cpu=0; fi
  echo "CPU $cpu $(nproc 2>/dev/null || echo 0) $(cut -d' ' -f1-3 /proc/loadavg)"
  free -m 2>/dev/null | awk '/^Mem:/{print "MEM",$3,$2} /^Swap:/{print "SWAP",$3,$2}'
  df -Pm / 2>/dev/null | awk 'NR==2{print "DISK",$3,$2}'
  if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
      --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPU /'
    nvidia-smi --query-compute-apps=pid,process_name,used_memory \
      --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPUP /'
  fi
  ps -eo pid,user:12,pcpu,pmem,etime,args --sort=-pcpu --no-headers 2>/dev/null | head -40 | sed 's/^/PS /'
  echo "@@END"
done
"""


def remote_metrics_loop(interval: int) -> str:
    return _REMOTE_METRICS_LOOP % {"sleep": str(int(interval))}

# Processes that are noise in a "what code is running" view.
PROC_IGNORE_RE = re.compile(
    r"^(\[.*\]|sshd|sh|bash|zsh|ps|sleep|head|awk|sed|cut|free|df|nvidia-smi|sudo|su|"
    r"systemd.*|dbus.*|cron|rsyslogd|snapd|unattended-upgrade|packagekitd|polkitd|"
    r"containerd|containerd-shim.*|dockerd|runc|chrome.*|chromium.*|node_exporter|"
    r"ssm-agent-worker|amazon-ssm-agent|multipathd|udisksd|networkd-dispatcher|tmux.*|screen|"
    r"login|getty|agetty|init|kthreadd)$"
)


def ssh_base_cmd(cfg: dict, host: str, batch: bool = True) -> list[str]:
    cmd = ["ssh"]
    if cfg.get("ssh_key"):
        cmd += ["-i", cfg["ssh_key"]]
    if batch:
        cmd += [
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2",
            "-o", "LogLevel=ERROR",
        ]
    target = f"{cfg['ssh_user']}@{host}" if cfg.get("ssh_user") else host
    cmd.append(target)
    return cmd


def _etime_seconds(etime: str) -> int:
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, mi, s = parts[-3:]
    return days * 86400 + h * 3600 + mi * 60 + s


def _fmt_secs(secs: int) -> str:
    if secs >= 86400:
        return f"{secs // 86400}d{(secs % 86400) // 3600}h"
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    if secs >= 60:
        return f"{secs // 60}m"
    return f"{secs}s"


def _project_label(path: str) -> str:
    """'/home/u/Repos/ocr-bench/.venv/bin/python' -> 'ocr-bench'."""
    m = re.search(r"/([^/]+)/(?:\.venv|venv|\.env|env|node_modules)/", path)
    if m:
        return m.group(1)
    m = re.search(r"/(?:Repos|repos|src|work|bench|projects|code)/([^/]+)/", path)
    return m.group(1) if m else ""


# Interpreter flags that take a value, so we skip the value too.
_INTERP_VALUE_FLAGS = {"-r", "--require", "--loader", "--import", "-X", "-W", "--env-file", "-cp", "-classpath"}
# Argument flags whose value is noise in a short label.
_NOISE_VALUE_FLAGS = {"--env-file", "--log-file", "--config", "--cfg"}


def _shorten_path(tok: str) -> str:
    """'/home/u/bench/stacks/x.env' -> '…/stacks/x.env'; keeps key=value shape."""
    key, eq, val = tok.partition("=")
    target = val if eq else tok
    if target.startswith(("/", "~/")) and target.count("/") >= 3:
        parts = target.rstrip("/").split("/")
        target = "…/" + "/".join(parts[-2:])
    return f"{key}={target}" if eq else target


def describe_command(args: str) -> tuple[str, str, str]:
    """Return (project_label, head, short_command) for a ps args string.

    head is the program identity (script / module / binary) used for
    grouping; short_command adds a few args for display.
    """
    toks = args.split()
    if not toks:
        return "", "", ""
    exe = toks[0]
    base = os.path.basename(exe)
    project = _project_label(exe)
    rest = toks[1:]

    if re.fullmatch(r"(python[\d.]*|node|ruby|perl|java|uv|uvicorn|gunicorn|deno|bun)", base):
        i = 0
        while i < len(rest) and rest[i].startswith("-") and rest[i] != "-m":
            i += 2 if rest[i] in _INTERP_VALUE_FLAGS else 1
        if i < len(rest) and rest[i] == "-m" and i + 1 < len(rest):
            head, tail = f"{base} -m {rest[i + 1]}", rest[i + 2:]
        elif i < len(rest):
            script = rest[i]
            project = project or _project_label(script)
            head, tail = (os.path.basename(script) if "/" in script else script), rest[i + 1:]
        else:
            head, tail = base, []
    else:
        head, tail = base, rest

    # Drop noisy flag/value pairs, shorten paths, keep a few args so distinct
    # runs of the same script stay distinguishable.
    cleaned: list[str] = []
    skip = False
    for t in tail:
        if skip:
            skip = False
            continue
        if t in _NOISE_VALUE_FLAGS:
            skip = True
            continue
        if any(t.startswith(f + "=") for f in _NOISE_VALUE_FLAGS):
            continue
        cleaned.append(_shorten_path(t))
    short = " ".join([head, *cleaned[:4]])
    return project, head, short[:110]


def summarize_procs(procs: list[dict], limit: int = 7) -> list[dict]:
    """Group processes by (project, command) and sum CPU/MEM.

    Many near-identical workers (same script, different shard args) collapse
    into one row so the list reads as "what code", not "what pids".
    """
    rows = []
    for p in procs:
        first = p["args"].split()[0] if p["args"].split() else ""
        if not first or PROC_IGNORE_RE.match(os.path.basename(first)) or PROC_IGNORE_RE.match(first):
            continue
        if p["cpu"] < 0.5 and p["mem"] < 0.5:
            continue
        project, head, short = describe_command(p["args"])
        if short:
            rows.append((p, project, head, short))

    def build(key_fn):
        groups: dict = {}
        for p, project, head, short in rows:
            key = key_fn(project, head, short)
            g = groups.setdefault(key, {
                "project": project, "head": head, "cmd": short, "count": 0,
                "cpu": 0.0, "mem": 0.0, "etime": 0, "user": p["user"], "pids": [],
            })
            g["count"] += 1
            g["cpu"] += p["cpu"]
            g["mem"] += p["mem"]
            g["etime"] = max(g["etime"], p["etime"])
            g["pids"].append(p["pid"])
            if g["cmd"] != short:
                g["cmd"] = head + " …"  # mixed args: show just the program
        return groups

    fine = build(lambda project, head, short: (project, short))
    # Collapse (project, head) families with 3+ distinct arg variants.
    by_head: dict = {}
    for (project, _short), g in fine.items():
        by_head.setdefault((project, g["head"]), []).append(g)
    merged = []
    for (project, head), gs in by_head.items():
        if len(gs) >= 3:
            coarse = build(lambda p_, h_, s_: (p_, h_))
            merged.append(coarse[(project, head)])
        else:
            merged.extend(gs)
    return sorted(merged, key=lambda g: (-g["cpu"], -g["mem"]))[:limit]


def parse_metrics(lines: list[str]) -> dict:
    m: dict = {"gpus": [], "gpu_procs": [], "procs": []}
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        try:
            if kind == "CPU" and len(parts) >= 3:
                m["cpu_pct"] = int(parts[1])
                m["cores"] = int(parts[2])
                m["load"] = " ".join(parts[3:6])
            elif kind == "MEM" and len(parts) >= 3:
                m["mem_used_mb"], m["mem_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "SWAP" and len(parts) >= 3:
                m["swap_used_mb"], m["swap_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "DISK" and len(parts) >= 3:
                m["disk_used_mb"], m["disk_total_mb"] = int(parts[1]), int(parts[2])
            elif kind == "GPU":
                cells = [c.strip() for c in line[4:].split(",")]
                if len(cells) >= 5:
                    m["gpus"].append({
                        "index": cells[0],
                        "name": re.sub(r"^NVIDIA\s+", "", cells[1]),
                        "util_pct": int(cells[2]) if cells[2].isdigit() else None,
                        "vram_used_mb": int(cells[3]) if cells[3].isdigit() else None,
                        "vram_total_mb": int(cells[4]) if cells[4].isdigit() else None,
                        "temp_c": int(cells[5]) if len(cells) > 5 and cells[5].isdigit() else None,
                    })
            elif kind == "GPUP":
                cells = [c.strip() for c in line[5:].split(",")]
                if len(cells) >= 3:
                    m["gpu_procs"].append({
                        "pid": cells[0],
                        "name": cells[1],
                        "vram_mb": int(cells[2]) if cells[2].isdigit() else None,
                    })
            elif kind == "PS":
                # PS pid user pcpu pmem etime args...
                fields = line[3:].split(None, 5)
                if len(fields) >= 6:
                    m["procs"].append({
                        "pid": fields[0],
                        "user": fields[1],
                        "cpu": float(fields[2]),
                        "mem": float(fields[3]),
                        "etime": _etime_seconds(fields[4]),
                        "args": fields[5],
                    })
        except ValueError:
            continue
    return m


class MetricsStream(threading.Thread):
    """Keeps one SSH session to a box and parses usage frames as they arrive."""

    def __init__(self, cfg: dict, box_id: str, host: str, state: "State"):
        super().__init__(daemon=True, name=f"metrics-{box_id}")
        self.cfg, self.box_id, self.host, self.state = cfg, box_id, host, state
        self.stop_event = threading.Event()
        self.proc: subprocess.Popen | None = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def run(self) -> None:
        while not self.stop_event.is_set():
            self._session()
            if self.stop_event.is_set():
                break
            self.stop_event.wait(SSH_RETRY_DELAY)

    def _session(self) -> None:
        cmd = ssh_base_cmd(self.cfg, self.host) + ["sh"]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except FileNotFoundError:
            self.state.set_metrics_error(self.box_id, "ssh not found")
            self.stop_event.set()
            return
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.proc.stdin.write(remote_metrics_loop(self.cfg.get("usage_interval", DEFAULT_USAGE_INTERVAL)))
        self.proc.stdin.flush()
        self.proc.stdin.close()  # sh reads the whole script, then runs the loop

        frame: list[str] = []
        for line in self.proc.stdout:
            if self.stop_event.is_set():
                break
            line = line.rstrip("\n")
            if line == "@@END":
                self.state.set_metrics(self.box_id, parse_metrics(frame))
                frame = []
            else:
                frame.append(line)
        self.proc.wait()
        if not self.stop_event.is_set():
            err = self.proc.stderr.read().strip().splitlines()
            msg = err[-1][:120] if err else f"ssh exited {self.proc.returncode}"
            self.state.set_metrics_error(self.box_id, msg)


class State:
    """Shared state between the EC2 poller, the SSH streams, and the renderer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.fetched = {"boxes": [], "error": "", "auth_error": False}
        self.metrics: dict[str, dict] = {}     # box_id -> {"m": {...}, "at": ts}
        self.metrics_err: dict[str, str] = {}
        self.streams: dict[str, MetricsStream] = {}
        self.last_poll = 0.0
        self.bt = {"jobs": [], "projects": [], "error": "", "at": 0.0}
        self.local_ident = local_identity()
        self.local: dict | None = None
        self.local_at = 0.0

    def usage_interval(self) -> int:
        with self.lock:
            return int(self.cfg.get("usage_interval", DEFAULT_USAGE_INTERVAL))

    def poll_baseten(self) -> None:
        res = fetch_baseten()
        with self.lock:
            self.bt = {**res, "at": time.time()}

    def poll_local(self) -> None:
        m = fetch_local_metrics(self.local_ident)
        with self.lock:
            self.local, self.local_at = m, time.time()

    def set_metrics(self, box_id: str, m: dict) -> None:
        with self.lock:
            self.metrics[box_id] = {"m": m, "at": time.time()}
            self.metrics_err.pop(box_id, None)

    def set_metrics_error(self, box_id: str, msg: str) -> None:
        with self.lock:
            self.metrics_err[box_id] = msg

    def poll_ec2(self) -> None:
        cfg = load_config()  # re-read so edits to aws.env apply without restart
        fetched = (
            fetch_dev_boxes(cfg)
            if cfg["configured"]
            else {"boxes": [], "error": "", "auth_error": False}
        )
        with self.lock:
            self.cfg, self.fetched, self.last_poll = cfg, fetched, time.time()
            self._reconcile_streams_locked()

    def _reconcile_streams_locked(self) -> None:
        want: dict[str, str] = {}
        for b in self.fetched["boxes"]:
            host = b.get("public_dns") or b.get("public_ip")
            if b.get("state") == "running" and host and not b.get("missing"):
                want[b["id"]] = host
        for box_id, stream in list(self.streams.items()):
            if box_id not in want or stream.host != want[box_id] or not stream.is_alive():
                stream.stop()
                del self.streams[box_id]
                self.metrics.pop(box_id, None)
        for box_id, host in want.items():
            if box_id not in self.streams:
                s = MetricsStream(self.cfg, box_id, host, self)
                self.streams[box_id] = s
                s.start()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "cfg": self.cfg,
                "fetched": self.fetched,
                "metrics": dict(self.metrics),
                "metrics_err": dict(self.metrics_err),
                "last_poll": self.last_poll,
                "bt": dict(self.bt),
                "local_ident": self.local_ident,
                "local": self.local,
                "local_at": self.local_at,
            }



# --------------------------------------------------------------------------
# Baseten training jobs (via the truss CLI)
# --------------------------------------------------------------------------

# truss uses Rich for tables; without a wide terminal it truncates every cell.
os.environ["COLUMNS"] = "400"

TRUSS_POLL_INTERVAL = 60   # seconds between `truss train view` calls
TRUSS_TIMEOUT = 45

def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m|\x1b\].*?\x07", "", text)


def strip_box_chars(text: str) -> str:
    # Remove Unicode box-drawing characters
    return re.sub(r"[─│┌┐└┘├┤┬┴┼╭╮╰╯╶╴╷╵═║╔╗╚╝╠╣╦╩╬▪]", "", text)


def _split_box_row(line: str) -> list[str]:
    """Split a Rich table row on │ separators and strip each cell."""
    cells = [c.strip() for c in line.split("│")]
    # Drop empty leading/trailing cells from the outer borders.
    if cells and not cells[0]:
        cells = cells[1:]
    if cells and not cells[-1]:
        cells = cells[:-1]
    return cells


def _parse_active_jobs_blocks(raw: str) -> list[dict]:
    """Parse the legacy per-job 'Training Job: <id>' detail blocks (truss <=0.14)."""
    text = strip_ansi(raw)
    jobs = []
    blocks = re.split(r"Training Job:\s+(\S+)", text)
    for i in range(1, len(blocks), 2):
        job_id = blocks[i].strip()
        content = blocks[i + 1] if i + 1 < len(blocks) else ""
        content = strip_box_chars(content)
        job = {"job_id": job_id}
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if key and val and key != val:
                    job[key] = val
        if "Status" in job:
            jobs.append(job)
    return jobs


def _parse_active_jobs_table(raw: str, projects: list[dict]) -> list[dict]:
    """Parse the single 'Active Training Jobs' Rich table (truss >=0.17).

    Columns: Job ID | Job Name | Project | Status | Instance Type | Created By | Created | Job Page
    """
    text = strip_ansi(raw)
    idx = text.find("Active Training Jobs")
    if idx < 0:
        return []
    section = text[idx:]

    name_to_pid = {p["name"]: p["project_id"] for p in projects}

    jobs = []
    headers = None
    for line in section.split("\n"):
        if "│" not in line:
            continue
        cells = _split_box_row(line)
        if not cells:
            continue
        if headers is None:
            # Header rows can wrap (e.g. " " then "Job ID", "Job Name"...). Wait
            # for the row that actually contains "Job ID".
            if any("Job ID" in c for c in cells):
                headers = cells
            continue
        # Data rows start with a 7-char alphanumeric job id.
        if not re.fullmatch(r"[a-z0-9]{7}", cells[0]):
            continue
        row = dict(zip(headers, cells))
        job = {
            "job_id": row.get("Job ID", cells[0]),
            "Job Name": row.get("Job Name", ""),
            "Project Name": row.get("Project", ""),
            "Status": row.get("Status", ""),
            "Instance Type": row.get("Instance Type", ""),
            "Created By": row.get("Created By", ""),
            "Created": row.get("Created", ""),
        }
        job["Project ID"] = name_to_pid.get(job["Project Name"], "")
        if job["Status"]:
            jobs.append(job)
    return jobs


def parse_active_jobs(raw: str, projects: list = None) -> list[dict]:
    """Parse the 'Active Training Jobs' section, supporting both truss formats."""
    jobs = _parse_active_jobs_blocks(raw)
    if jobs:
        # Older format omits Project ID inside the per-job block in some versions;
        # backfill from the projects table when possible.
        if projects:
            name_to_pid = {p["name"]: p["project_id"] for p in projects}
            for j in jobs:
                if not j.get("Project ID") and j.get("Project Name"):
                    j["Project ID"] = name_to_pid.get(j["Project Name"], "")
        return jobs
    return _parse_active_jobs_table(raw, projects or [])


def parse_projects(raw: str) -> list[dict]:
    """Parse the projects table to get project IDs and names."""
    text = strip_ansi(raw)
    # Do not let rows from the following jobs table masquerade as projects.
    section = text.split("Active Training Jobs", 1)[0]

    projects = []
    headers = None
    for line in section.split("\n"):
        if "│" not in line:
            continue
        cells = _split_box_row(line)
        if not cells:
            continue
        if headers is None:
            if any("Project ID" in c for c in cells):
                headers = cells
            continue
        if not re.fullmatch(r"[a-z0-9]{7}", cells[0]):
            continue
        row = dict(zip(headers, cells))
        projects.append({
            "project_id": row.get("Project ID", cells[0]),
            "name": row.get("Name", ""),
            "latest_job_id": row.get("Latest Job ID", ""),
        })

    if projects:
        return projects

    # Fall back to whitespace parsing for older Truss output.
    section = strip_box_chars(section)
    # Find lines that look like project rows (start with a project ID pattern)
    for line in section.split("\n"):
        line = line.strip()
        # Project IDs are 7-char alphanumeric
        match = re.match(r"^([a-z0-9]{7})\s+(.+)", line)
        if match:
            parts = re.split(r"\s{2,}", line)
            if len(parts) >= 5:
                projects.append({
                    "project_id": parts[0].strip(),
                    "name": parts[1].strip(),
                    "latest_job_id": parts[4].strip() if len(parts) > 4 else "",
                })

    return projects



def parse_gpu_info(instance: str) -> str:
    """Extract GPU count and type from instance string like 'H200:8x192x2048 - 8 H200 GPUs'."""
    m = re.search(r"(\d+)\s+(H\d+|A\d+\w*)\s+GPU", instance)
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    return ""


def total_gpus(jobs: list[dict]) -> int:
    """Sum GPU count across all active jobs."""
    total = 0
    for job in jobs:
        m = re.search(r"(\d+)\s+(?:H\d+|A\d+\w*)\s+GPU", job.get("Instance Type", ""))
        if m:
            total += int(m.group(1))
    return total


def short_status(status: str) -> str:
    return (
        status.replace("TRAINING_JOB_", "")
        .replace("_", " ")
        .title()
    )



def parse_wandb_run_url(raw: str) -> str:
    """Extract the first concrete W&B run URL from training logs."""
    urls = re.findall(r"https://(?:www\.)?wandb\.ai/[^\s<>\"']+", strip_ansi(raw))
    for url in urls:
        url = url.rstrip(".,;:)]}")
        if "/runs/" in url:
            return url
    return ""



def open_wandb_run(job_id: str) -> None:
    """Resolve a job's W&B URL from its logs and open it in the browser."""
    if not re.fullmatch(r"[a-z0-9]{7}", job_id):
        notify(f"Invalid Baseten job ID: {job_id}")
        return

    try:
        result = subprocess.run(
            [
                "truss",
                "train",
                "logs",
                "--job-id",
                job_id,
                "--non-interactive",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        notify(f"Could not read logs for {job_id}")
        return

    url = parse_wandb_run_url(result.stdout + result.stderr)
    if not url:
        notify(f"No W&B run URL found in logs for {job_id}")
        return

    subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=5)



def status_icon(status: str) -> str:
    """Colored dot for a Baseten job status."""
    s = status.upper()
    if "RUNNING" in s:
        return sf_icon("circle.fill", GREEN)
    if "CREATED" in s:
        return sf_icon("circle.fill", ORANGE)
    if "FAILED" in s:
        return sf_icon("circle.fill", RED)
    if "STOPPED" in s or "COMPLETED" in s:
        return sf_icon("circle.fill", GREY)
    return sf_icon("circle", GREY)


def fetch_baseten() -> dict:
    """Run truss once. Returns {"jobs", "projects", "error"}."""
    try:
        result = subprocess.run(
            ["truss", "train", "view", "--non-interactive"],
            capture_output=True, text=True, timeout=TRUSS_TIMEOUT,
        )
    except FileNotFoundError:
        return {"jobs": [], "projects": [], "error": "truss not found in PATH"}
    except subprocess.TimeoutExpired:
        return {"jobs": [], "projects": [], "error": f"truss timed out after {TRUSS_TIMEOUT}s"}
    raw = result.stdout + result.stderr
    if result.returncode != 0:
        tail = strip_ansi(result.stderr).strip().splitlines()
        return {"jobs": [], "projects": [], "error": f"truss exited {result.returncode}: {tail[-1][:160] if tail else ''}"}
    projects = parse_projects(raw)
    return {"jobs": parse_active_jobs(raw, projects), "projects": projects, "error": ""}


# --------------------------------------------------------------------------
# This Mac
# --------------------------------------------------------------------------



def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def local_identity() -> dict:
    """Static facts about this machine (read once)."""
    try:
        name = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    chip = _sysctl("machdep.cpu.brand_string")
    cores = _sysctl("hw.ncpu")
    mem = _sysctl("hw.memsize")
    return {
        "name": name or os.uname().nodename.split(".")[0],
        "chip": re.sub(r"\s+", " ", chip),
        "cores": int(cores) if cores.isdigit() else 0,
        "mem_total_mb": int(mem) // (1024 * 1024) if mem.isdigit() else 0,
    }


_TOP_MEM_RE = re.compile(r"PhysMem:\s*([\d.]+)([KMG]) used.*?([\d.]+)([KMG]) unused")
_TOP_CPU_RE = re.compile(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle")
_TOP_LOAD_RE = re.compile(r"Load Avg:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)")


def _to_mb(value: str, unit: str) -> int:
    scale = {"K": 1 / 1024, "M": 1, "G": 1024}[unit]
    return int(float(value) * scale)


def fetch_local_metrics(ident: dict) -> dict:
    """Sample this Mac: real CPU% over ~1s via top, memory, disk, processes."""
    m: dict = {"gpus": [], "gpu_procs": [], "procs": []}
    try:
        top = subprocess.run(
            ["/usr/bin/top", "-l", "2", "-n", "0", "-s", "1"],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        top = ""
    cpu = _TOP_CPU_RE.findall(top)
    if cpu:
        user, sys_, _idle = cpu[-1]  # second sample is the real interval
        m["cpu_pct"] = int(round(float(user) + float(sys_)))
    load = _TOP_LOAD_RE.findall(top)
    if load:
        m["load"] = " ".join(load[-1])
    m["cores"] = ident.get("cores", 0)
    mem = _TOP_MEM_RE.findall(top)
    if mem:
        used_v, used_u, unused_v, unused_u = mem[-1]
        used = _to_mb(used_v, used_u)
        total = ident.get("mem_total_mb") or used + _to_mb(unused_v, unused_u)
        m["mem_used_mb"], m["mem_total_mb"] = used, total
    # macOS keeps "used" near 100% by design (compressed + cached pages), so
    # memory pressure is the honest signal. memory_pressure reports free %.
    try:
        mp = subprocess.run(["/usr/bin/memory_pressure"], capture_output=True, text=True, timeout=5).stdout
        free = re.search(r"free percentage:\s*(\d+)%", mp)
        if free:
            m["mem_pressure_pct"] = 100 - int(free.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        # The Data volume is where user files live; "/" is the small sealed system volume.
        df = subprocess.run(["/bin/df", "-k", "/System/Volumes/Data"], capture_output=True, text=True, timeout=5).stdout.splitlines()
        if len(df) >= 2:
            cols = df[1].split()
            m["disk_used_mb"], m["disk_total_mb"] = int(cols[2]) // 1024, int(cols[1]) // 1024
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    try:
        ps = subprocess.run(
            ["/bin/ps", "-Ao", "pid,user,%cpu,%mem,etime,args", "-r"],
            capture_output=True, text=True, timeout=5,
        ).stdout.splitlines()[1:40]
        for line in ps:
            fields = line.split(None, 5)
            if len(fields) >= 6:
                m["procs"].append({
                    "pid": fields[0], "user": fields[1], "cpu": float(fields[2]),
                    "mem": float(fields[3]), "etime": _etime_seconds(fields[4]), "args": fields[5],
                })
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return m


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

GREEN, ORANGE, RED, GREY, BLUE = "#34c759", "#ff9f0a", "#ff453a", "#8e8e93", "#0a84ff"


def sf_icon(symbol: str, color: str = "", scale: str = "medium") -> str:
    """SwiftBar params for a colored SF Symbol icon (color goes via sfconfig)."""
    if not color:
        return f"sfimage={symbol}"
    cfg = {"renderingMode": "Hierarchical", "colors": [color], "scale": scale}
    b64 = base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode()
    return f"sfimage={symbol} sfconfig={b64}"


def box_icon(box: dict) -> str:
    """Simple colored dot for the box state (the one icon we use)."""
    if box.get("missing"):
        return sf_icon("circle", GREY)
    if box.get("impaired"):
        return sf_icon("circle.fill", RED)
    return {
        "running": sf_icon("circle.fill", GREEN),
        "pending": sf_icon("circle.fill", ORANGE),
        "stopping": sf_icon("circle.fill", ORANGE),
        "shutting-down": sf_icon("circle.fill", ORANGE),
        "stopped": sf_icon("circle.fill", GREY),
        "terminated": sf_icon("circle.fill", RED),
    }.get(box.get("state", ""), sf_icon("circle", GREY))


def elapsed_time(created_at: str, now: datetime | None = None) -> str:
    if not created_at:
        return ""
    value = created_at.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return ""
    if now is None:
        now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    seconds = max(0, int((now - created).total_seconds()))
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def usage_bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return "░" * width
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _gb(mb: int | None) -> str:
    if mb is None:
        return "?"
    return f"{mb / 1024:.0f}" if mb >= 10240 else f"{mb / 1024:.1f}"


def _pct(used: int | None, total: int | None) -> float | None:
    if used is None or not total:
        return None
    return 100.0 * used / total


def _ram_pct(m: dict) -> float | None:
    """RAM figure for summaries: memory pressure on macOS, used/total elsewhere."""
    if m.get("mem_pressure_pct") is not None:
        return float(m["mem_pressure_pct"])
    return _pct(m.get("mem_used_mb"), m.get("mem_total_mb"))


def _usage_color(pct: float | None) -> str:
    if pct is None:
        return GREY
    if pct >= 90:
        return RED
    if pct >= 70:
        return ORANGE
    return GREEN


def _fmt_pct(p: float | None) -> str:
    return "  ?" if p is None else f"{p:>3.0f}"


def _vram_pct(m: dict) -> float | None:
    """Overall VRAM use across GPUs, or None when the box has none."""
    used = sum(g["vram_used_mb"] for g in m.get("gpus", []) if g.get("vram_used_mb") is not None)
    total = sum(g["vram_total_mb"] for g in m.get("gpus", []) if g.get("vram_total_mb"))
    return _pct(used, total) if total else None


def metrics_summary(m: dict) -> str:
    """Dropdown summary: CPU · RAM · GPU util (and VRAM) when present."""
    bits = []
    if m.get("cpu_pct") is not None:
        bits.append(f"CPU {m['cpu_pct']}%")
    mem = _ram_pct(m)
    if mem is not None:
        bits.append(f"RAM {mem:.0f}%")
    utils = [g["util_pct"] for g in m.get("gpus", []) if g.get("util_pct") is not None]
    if utils:
        bits.append(f"GPU {max(utils)}%")
    vram = _vram_pct(m)
    if vram is not None:
        bits.append(f"VRAM {vram:.0f}%")
    return " · ".join(bits)


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# Menu bar title separator: a dot with extra breathing room. (A literal "|"
# can't be used; it is SwiftBar's text/params separator.)
SEP = "  ·  "


def title_preview(box: dict, m: dict | None) -> str:
    """Menu bar text for one box: SKU │ short name │ CPU │ RAM │ VRAM."""
    parts = [box.get("type", ""), _truncate(box.get("name", box["id"]), 12)]
    if m:
        if m.get("cpu_pct") is not None:
            parts.append(f"CPU {m['cpu_pct']}%")
        mem = _pct(m.get("mem_used_mb"), m.get("mem_total_mb"))
        if mem is not None:
            parts.append(f"RAM {mem:.0f}%")
        vram = _vram_pct(m)
        if vram is not None:
            parts.append(f"VRAM {vram:.0f}%")
    return SEP.join(p for p in parts if p)


def metrics_lines(m: dict, age: float, interval: int = DEFAULT_USAGE_INTERVAL) -> list[str]:
    mono = "font=Menlo size=11"
    out = []
    stale = f"  (stale {age:.0f}s)" if age > stale_after(interval) else ""

    def row(label: str, pct: float | None, detail: str) -> str:
        # Text only; a hot row turns orange/red via the line color.
        color = "" if pct is None or pct < 70 else f" color={_usage_color(pct)}"
        return f"--{label} {usage_bar(pct)} {_fmt_pct(pct)}%  {detail} | {mono}{color}"

    cpu = m.get("cpu_pct")
    tail = " · ".join(x for x in (
        f"{m['cores']} cores" if m.get("cores") else "",
        f"load {m['load']}" if m.get("load") else "",
    ) if x)
    out.append(row("CPU ", cpu, tail + stale))

    if m.get("mem_pressure_pct") is not None:
        used = _pct(m.get("mem_used_mb"), m.get("mem_total_mb"))
        detail = f"pressure · {_gb(m['mem_used_mb'])}/{_gb(m['mem_total_mb'])} GB used" if used is not None else "pressure"
        out.append(row("RAM ", float(m["mem_pressure_pct"]), detail))
    else:
        mem = _pct(m.get("mem_used_mb"), m.get("mem_total_mb"))
        if mem is not None:
            out.append(row("RAM ", mem, f"{_gb(m['mem_used_mb'])}/{_gb(m['mem_total_mb'])} GB"))
    swap = _pct(m.get("swap_used_mb"), m.get("swap_total_mb"))
    if swap is not None and m.get("swap_used_mb", 0) > 0:
        out.append(row("Swap", swap, f"{_gb(m['swap_used_mb'])}/{_gb(m['swap_total_mb'])} GB"))
    for g in m.get("gpus", []):
        util, vram = g.get("util_pct"), _pct(g.get("vram_used_mb"), g.get("vram_total_mb"))
        temp = f" · {g['temp_c']}°C" if g.get("temp_c") is not None else ""
        label = f"GPU{g['index']}" if len(m["gpus"]) > 1 else "GPU "
        out.append(row(label, util, f"{g['name']}{temp}"))
        out.append(row("VRAM", vram, f"{_gb(g['vram_used_mb'])}/{_gb(g['vram_total_mb'])} GB"))
    disk = _pct(m.get("disk_used_mb"), m.get("disk_total_mb"))
    if disk is not None:
        out.append(row("Disk", disk, f"{_gb(m['disk_used_mb'])}/{_gb(m['disk_total_mb'])} GB on /"))
    return out


def process_lines(m: dict) -> list[str]:
    """'Running code' section: grouped processes and GPU holders."""
    mono = "font=Menlo size=11"
    out: list[str] = []
    groups = summarize_procs(m.get("procs", []))
    gpu_procs = m.get("gpu_procs", [])
    if not groups and not gpu_procs:
        return out

    out.append("-----")
    out.append(f"--{'CPU%':>5} {'MEM%':>5} {'TIME':>6}  PROCESS | {mono} color=gray")
    for g in groups:
        count = f"{g['count']}x " if g["count"] > 1 else ""
        project = f"[{g['project']}] " if g["project"] else ""
        user = f" ({g['user']})" if g["user"] not in ("ubuntu", "ec2-user", "") else ""
        label = f"{count}{project}{g['cmd']}{user}"
        stats = f"{g['cpu']:>4.0f}% {g['mem']:>4.1f}% {_fmt_secs(g['etime']):>6}"
        out.append(f"--{stats}  {label} | {mono} length=120")

    if gpu_procs:
        by_pid = {p["pid"]: p for p in m.get("procs", [])}
        for gp in gpu_procs:
            proc = by_pid.get(gp["pid"])
            if proc:
                project, _head, short = describe_command(proc["args"])
            else:
                project, short = _project_label(gp["name"]), os.path.basename(gp["name"])
            label = (f"[{project}] " if project else "") + (short or gp["name"])
            vram = f"{gp['vram_mb']}M" if gp.get("vram_mb") is not None else "?"
            out.append(f"--{'GPU':>5} {vram:>6} {'pid ' + gp['pid']:>11}  {label} | {mono} length=120")
    return out


def console_url(box: dict, cfg: dict) -> str:
    region = box.get("region") or cfg.get("region") or "us-east-1"
    return (
        f"https://{region}.console.aws.amazon.com/ec2/home?region={region}"
        f"#InstanceDetails:instanceId={box['id']}"
    )


def _bt_title(bt: dict) -> tuple[str, str]:
    """(text, color) for the Baseten part of the menu bar."""
    if bt["error"]:
        return "BT: err", "red"
    jobs = bt["jobs"]
    running = [j for j in jobs if "RUNNING" in j.get("Status", "").upper()]
    created = [j for j in jobs if "CREATED" in j.get("Status", "").upper()]
    active = running or created
    gpus = total_gpus(active) if active else 0
    gpu_str = f" ({gpus} GPUs)" if gpus else ""
    if running:
        return f"BT: {len(running)} running{gpu_str}", "green"
    if created:
        return f"BT: {len(created)} starting{gpu_str}", "orange"
    if jobs:
        return f"BT: {len(jobs)} jobs", ""
    if not bt["at"]:
        return "BT: …", ""
    return "BT: idle", ""


def _local_title(ident: dict, m: dict | None) -> str:
    text = _truncate(ident.get("name") or "Mac", 14)
    if m:
        bits = []
        if m.get("cpu_pct") is not None:
            bits.append(f"CPU {m['cpu_pct']}%")
        mem = _ram_pct(m)
        if mem is not None:
            bits.append(f"RAM {mem:.0f}%")
        if bits:
            text += SEP + SEP.join(bits)
    return text


def _ec2_title(cfg: dict, fetched: dict, snap: dict, now: float) -> tuple[str, str]:
    if not cfg["configured"]:
        return ("EC2: config err", "red") if cfg["error"] else ("", "")
    if fetched["auth_error"]:
        return "EC2: sign in", "orange"
    if fetched["error"]:
        return "EC2: err", "red"
    boxes = fetched["boxes"]
    running = [b for b in boxes if b.get("state") == "running"]
    cpus, rams, vrams = [], [], []
    for b in running:
        entry = snap["metrics"].get(b["id"])
        if entry and now - entry["at"] <= stale_after(cfg["usage_interval"]):
            m = entry["m"]
            if m.get("cpu_pct") is not None:
                cpus.append(m["cpu_pct"])
            ram = _pct(m.get("mem_used_mb"), m.get("mem_total_mb"))
            if ram is not None:
                rams.append(ram)
            vram = _vram_pct(m)
            if vram is not None:
                vrams.append(vram)
    text = f"EC2: {len(running)}/{len(boxes)} up"
    if cpus:
        text += f"{SEP}CPU {max(cpus)}%"
    if rams:
        text += f"{SEP}RAM {max(rams):.0f}%"
    if vrams:
        text += f"{SEP}VRAM {max(vrams):.0f}%"
    if any(b.get("impaired") for b in running):
        return text + " ⚠", "red"
    return text, ""


def render_title(snap: dict, now: float) -> str:
    cfg, fetched, bt = snap["cfg"], snap["fetched"], snap["bt"]
    pinned_key = cfg.get("pinned", "")

    if pinned_key == "baseten":
        text, color = _bt_title(bt)
        return f"{text} | color={color}" if color else text
    if pinned_key == "local":
        fresh = snap["local"] if snap["local"] and now - snap["local_at"] <= stale_after(cfg["usage_interval"]) else None
        return _local_title(snap["local_ident"], fresh)
    pinned = next(
        (b for b in fetched["boxes"] if pinned_key and pinned_key in (b["id"], b.get("name")) and not b.get("missing")),
        None,
    )
    if pinned:
        entry = snap["metrics"].get(pinned["id"])
        fresh = entry["m"] if entry and now - entry["at"] <= stale_after(cfg["usage_interval"]) else None
        if pinned.get("state") != "running":
            return f"{title_preview(pinned, None)}: {pinned.get('state', 'unknown')} | color=gray"
        text = title_preview(pinned, fresh)
        return f"{text} | color=red" if pinned.get("impaired") else text

    # Nothing pinned: compact summary of all three.
    bt_text, bt_color = _bt_title(bt)
    ec2_text, ec2_color = _ec2_title(cfg, fetched, snap, now)
    ec2_short = ec2_text.split(SEP)[0] if ec2_text else ""   # just "EC2: 2/2 up"
    local = snap["local"] if snap["local"] and now - snap["local_at"] <= stale_after(cfg["usage_interval"]) else None
    local_short = f"Mac: CPU {local['cpu_pct']}%" if local and local.get("cpu_pct") is not None else ""
    text = SEP.join(t for t in (bt_text, ec2_short, local_short) if t)
    color = bt_color or ec2_color
    return f"{text} | color={color}" if color else text


def render_baseten(snap: dict, me: str) -> list[str]:
    bt = snap["bt"]
    python = shlex.quote(sys.executable)
    script = shlex.quote(os.path.abspath(__file__))
    pinned = snap["cfg"].get("pinned") == "baseten"
    head_text, _ = _bt_title(bt)
    head_click = f"{me} param2={'--aws-unpin' if pinned else '--aws-pin param3=baseten'} terminal=false"
    lines = [f"Baseten — {head_text[4:]} | {head_click}{' checked=true' if pinned else ''}"]

    if bt["error"]:
        lines.append(f"--⚠️ {bt['error']} | color=red size=12")
        return lines
    if not bt["at"]:
        lines.append("--Loading… | color=gray size=12")
        return lines

    jobs = bt["jobs"]
    if not jobs:
        lines.append("--No active training jobs | size=12")
    for job in jobs:
        job_id = job.get("job_id", "?")
        job_name = job.get("Job Name", job_id)
        status = job.get("Status", "UNKNOWN")
        project_id = job.get("Project ID", "")
        instance = job.get("Instance Type", "")
        created_by = job.get("Created By", "")
        created_at = job.get("Created", "")
        elapsed = elapsed_time(created_at)
        url = f"https://app.baseten.co/training/{project_id}/logs/{job_id}"
        gpu = parse_gpu_info(instance)
        gpu_label = f" [{gpu}]" if gpu else ""
        elapsed_label = f" · {elapsed}" if elapsed else ""
        lines.append(f"--{job_name} — {short_status(status)}{elapsed_label}{gpu_label} | {status_icon(status)} href={url}")
        lines.append(f"----Job ID: {job_id} | font=monospace size=12")
        lines.append(f"----Instance: {instance} | size=12")
        if created_by:
            lines.append(f"----Launched by: {created_by} | size=12")
        if elapsed:
            duration_name = "Running for" if "RUNNING" in status.upper() else "Elapsed"
            lines.append(f"----{duration_name}: {elapsed} | size=12")
        lines.append(f"----Created: {created_at} | size=12")
        lines.append(f"----Open Baseten Logs | href={url}")
        lines.append(
            f"----Open W&B Run | bash={python} param1={script} "
            f"param2=--open-wandb param3={job_id} terminal=false refresh=false"
        )
        lines.append(f"----Copy Job ID | bash=/bin/bash param1=-c param2='echo -n {job_id} | pbcopy' terminal=false")

    if bt["projects"]:
        lines.append("-------")
        lines.append("--All Projects")
        for pr in bt["projects"]:
            lines.append(f"----{pr['name']} ({pr['project_id']}) | href=https://app.baseten.co/training/{pr['project_id']}")
    return lines


def render_aws(snap: dict, me: str, now: float) -> list[str]:
    cfg, fetched = snap["cfg"], snap["fetched"]
    lines: list[str] = []
    scope = " · ".join(x for x in (cfg.get("profile"), cfg.get("region")) if x)
    lines.append("AWS Dev Boxes" + (f" ({scope})" if scope else ""))

    if not cfg["configured"]:
        if cfg["error"]:
            lines.append(f"--⚠️ {cfg['error']} | color=red size=12")
            if "permissions" in cfg["error"]:
                lines.append(
                    f"--Fix permissions (chmod 600) | bash=/bin/chmod param1=600 "
                    f"param2={shlex.quote(CONFIG_PATH)} terminal=false refresh=true"
                )
        else:
            lines.append("--Not configured | size=12")
        lines.append(f"--Set up AWS dev boxes… | {me} param2=--aws-setup terminal=false refresh=true")
        return lines

    for w in cfg["warnings"]:
        lines.append(f"--⚠️ {w} | color=orange size=12")

    if fetched["auth_error"]:
        login = " ".join(aws_login_cmd(cfg))
        lines.append("🔐 AWS sign-in required | color=orange")
        lines.append(f"--{fetched['error']} | size=12")
        lines.append(f"--Sign in ({login}) | {me} param2=--aws-login terminal=false")
        if not profile_uses_sso(cfg):
            lines.append("-----")
            lines.append("--Does your org sign in through a start URL like https://xxx.awsapps.com/start ? | size=11 color=gray")
            lines.append("--Then set up SSO once; after that Sign in opens your org's page. | size=11 color=gray")
            lines.append(f"--Set up SSO sign-in (aws configure sso)… | {me} param2=--aws-configure-sso terminal=false")
    elif fetched["error"]:
        lines.append(f"⚠️ {fetched['error']} | color=red")
    elif not fetched["boxes"] and snap["last_poll"] == 0:
        lines.append("Loading… | color=gray")

    for box in fetched["boxes"]:
        icon = box_icon(box)
        name, state = box.get("name", box["id"]), box.get("state", "unknown")
        if box.get("missing"):
            lines.append(f"{name} — not found (check aws.env) | {icon} color=gray")
            continue

        up = elapsed_time(box.get("launch_time", "")) if state == "running" else ""
        type_label = f" [{box['type']}]" if box.get("type") else ""
        up_label = f" · up {up}" if up else ""
        url = console_url(box, cfg)
        host = box.get("public_dns") or box.get("public_ip") or ""
        has_public = bool(host)
        is_pinned = bool(cfg.get("pinned")) and cfg["pinned"] in (box["id"], name)
        # Clicking a box pins it to the menu bar (or unpins the pinned one).
        click = f"{me} param2={'--aws-unpin' if is_pinned else '--aws-pin param3=' + box['id']} terminal=false"

        entry = snap["metrics"].get(box["id"])
        m = entry["m"] if entry else None
        age = now - entry["at"] if entry else 0
        summary = f" · {metrics_summary(m)}" if m else ""
        pin_flag = " checked=true" if is_pinned else ""
        lines.append(f"{name} — {state}{up_label}{type_label}{summary} | {icon} {click}{pin_flag}")

        if m:
            lines.extend(metrics_lines(m, age, cfg["usage_interval"]))
            lines.extend(process_lines(m))
            lines.append("-----")
        elif state == "running" and has_public:
            err = snap["metrics_err"].get(box["id"])
            msg = f"Usage unavailable: {err}" if err else "Connecting for usage…"
            lines.append(f"--{msg} | color=gray size=11")
            lines.append("-----")

        lines.append(f"--Instance ID: {box['id']} | font=monospace size=12")
        if box.get("type"):
            lines.append(f"--Type: {box['type']} · {box.get('az', '')} | size=12")
        if box.get("public_ip"):
            lines.append(f"--Public IP: {box['public_ip']} | font=monospace size=12")
        if box.get("private_ip"):
            lines.append(f"--Private IP: {box['private_ip']} | font=monospace size=12")
        if box.get("status_checks"):
            lines.append(f"--Status checks: {box['status_checks']} | size=12")
        if box.get("launch_time"):
            lines.append(f"--Launched: {box['launch_time']} | size=12")
        lines.append("-----")
        lines.append(f"--Open in AWS Console | href={url}")
        if state == "running":
            lines.append(f"--Connect via SSM (Terminal) | {me} param2=--aws-ssm param3={box['id']} terminal=false")
        if state == "running" and has_public:
            lines.append(f"--SSH (Terminal) | {me} param2=--aws-ssh param3={box['id']} param4={host} terminal=false")
            lines.append(f"--Open htop (Terminal) | {me} param2=--aws-htop param3={box['id']} param4={host} terminal=false")
        if is_pinned:
            lines.append(f"--Unpin from menu bar | {me} param2=--aws-unpin terminal=false")
        else:
            lines.append(f"--Pin to menu bar | {me} param2=--aws-pin param3={box['id']} terminal=false")
        if box.get("public_ip"):
            lines.append(
                f"--Copy Public IP | bash=/bin/bash param1=-c "
                f"param2='echo -n {box['public_ip']} | pbcopy' terminal=false"
            )
        lines.append(
            f"--Copy Instance ID | bash=/bin/bash param1=-c "
            f"param2='echo -n {box['id']} | pbcopy' terminal=false"
        )
        if state == "stopped":
            lines.append(f"--▶ Start instance | {me} param2=--aws-start param3={box['id']} terminal=false")
        elif state == "running":
            lines.append(f"--■ Stop instance… | {me} param2=--aws-stop param3={box['id']} terminal=false")
    return lines


def render_local(snap: dict, me: str, now: float) -> list[str]:
    ident, m = snap["local_ident"], snap["local"]
    pinned = snap["cfg"].get("pinned") == "local"
    click = f"{me} param2={'--aws-unpin' if pinned else '--aws-pin param3=local'} terminal=false"
    chip = f" [{ident['chip']}]" if ident.get("chip") else ""
    summary = f" · {metrics_summary(m)}" if m else ""
    lines = [f"{ident['name']} — this Mac{chip}{summary} | {sf_icon('circle.fill', GREEN)} {click}{' checked=true' if pinned else ''}"]
    if m:
        lines.extend(metrics_lines(m, now - snap["local_at"], snap["cfg"]["usage_interval"]))
        lines.extend(process_lines(m))
    else:
        lines.append("--Sampling… | color=gray size=11")
    lines.append("-----")
    if pinned:
        lines.append(f"--Unpin from menu bar | {me} param2=--aws-unpin terminal=false")
    else:
        lines.append(f"--Pin to menu bar | {me} param2=--aws-pin param3=local terminal=false")
    lines.append("--Open Activity Monitor | bash=/usr/bin/open param1=-a param2=Activity\\ Monitor terminal=false")
    return lines


def render(snap: dict) -> str:
    """Build the full menu text for one frame."""
    python = shlex.quote(sys.executable)
    script = shlex.quote(os.path.abspath(__file__))
    me = f"bash={python} param1={script}"
    now = time.time()

    lines: list[str] = [render_title(snap, now), "---"]
    lines += render_baseten(snap, me)
    lines.append("---")
    lines += render_aws(snap, me, now)
    lines.append("---")
    lines += render_local(snap, me, now)

    lines.append("---")
    bt_age = f"Baseten {now - snap['bt']['at']:.0f}s ago" if snap["bt"]["at"] else "Baseten loading"
    ec2_age = f"EC2 {now - snap['last_poll']:.0f}s ago" if snap["last_poll"] else "EC2 loading"
    interval = snap["cfg"]["usage_interval"]
    lines.append(f"Polled: {bt_age} · {ec2_age} · usage every {interval}s | size=11 color=gray")
    lines.append(f"Usage refresh: every {interval}s")
    for choice in USAGE_INTERVAL_CHOICES:
        flag = " checked=true" if choice == interval else ""
        lines.append(f"--Every {choice}s | {me} param2=--aws-usage-interval param3={choice} terminal=false{flag}")
    lines.append(f"Edit AWS dev box settings… | {me} param2=--aws-setup terminal=false refresh=true")
    lines.append("Refresh | refresh=true")
    return "\n".join(lines)


def stream_forever() -> None:
    state = State()

    def poller():
        while True:
            try:
                state.poll_ec2()
            except Exception as exc:  # keep the stream alive no matter what
                with state.lock:
                    state.fetched = {"boxes": [], "error": f"poll failed: {exc}"[:200], "auth_error": False}
            with state.lock:
                unhealthy = bool(state.fetched.get("error")) or state.fetched.get("auth_error")
            time.sleep(EC2_RETRY_INTERVAL if unhealthy else EC2_POLL_INTERVAL)

    def bt_poller():
        while True:
            try:
                state.poll_baseten()
            except Exception as exc:
                with state.lock:
                    state.bt = {"jobs": [], "projects": [], "error": f"poll failed: {exc}"[:200], "at": time.time()}
            time.sleep(TRUSS_POLL_INTERVAL)

    def local_poller():
        while True:
            started = time.time()
            try:
                state.poll_local()
            except Exception:
                pass
            # `top` itself takes ~1s, so sleep only the remainder of the period.
            time.sleep(max(0.2, state.usage_interval() - (time.time() - started)))

    threading.Thread(target=poller, daemon=True, name="ec2-poller").start()
    threading.Thread(target=bt_poller, daemon=True, name="truss-poller").start()
    threading.Thread(target=local_poller, daemon=True, name="local-poller").start()

    # First frame right away so the menu bar is not blank while things load.
    while True:
        try:
            sys.stdout.write("~~~\n")
            sys.stdout.write(render(state.snapshot()) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return  # SwiftBar went away
        time.sleep(state.usage_interval())


# --------------------------------------------------------------------------
# Menu actions (run as a separate short-lived process)
# --------------------------------------------------------------------------

LAUNCH_DIR = os.path.expanduser("~/Library/Application Support/baseten-job-monitor/launch")


REFRESH_SELF = ["/usr/bin/open", "-g", "swiftbar://refreshplugin?name=monitor"]


def _terminal_app() -> str:
    """Honor SwiftBar's terminal preference (Terminal or iTerm); default Terminal."""
    try:
        out = subprocess.run(
            ["/usr/bin/defaults", "read", "com.ameba.SwiftBar", "Terminal"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    return "iTerm" if "iterm" in out else "Terminal"


def open_in_terminal(cmd: list[str], title: str, after: list[str] | None = None) -> None:
    """Open a new terminal window running `cmd`.

    SwiftBar's own `terminal=false` drives Terminal through AppleScript and
    System Events, which needs Automation + Accessibility permissions and
    fails silently without them. Writing a .command file and handing it to
    `open` needs no permissions and always gets a fresh window.
    """
    os.makedirs(LAUNCH_DIR, mode=0o700, exist_ok=True)
    os.chmod(LAUNCH_DIR, 0o700)
    safe_title = re.sub(r"[^A-Za-z0-9._ \-]", "", title)[:60]
    path = os.path.join(LAUNCH_DIR, f"{re.sub(r'[^A-Za-z0-9_-]', '-', title)[:40]}-{os.getpid()}.command")
    quoted = " ".join(shlex.quote(c) for c in cmd)
    body = (
        "#!/bin/sh\n"
        'rm -f "$0"\n'  # one-shot launcher; the shell already has it open
        f"printf '\\033]0;%s\\007' {shlex.quote(safe_title)}\n"
        f"echo {shlex.quote('$ ' + quoted)}\n"
    )
    if after:
        # Run the main command, then the follow-up regardless of its exit code.
        body += f"{quoted}; rc=$?\n{' '.join(shlex.quote(c) for c in after)}\nexit $rc\n"
    else:
        body += f"exec {quoted}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    subprocess.run(["/usr/bin/open", "-a", _terminal_app(), path], capture_output=True, timeout=10)


def run_interactive(cmd: list[str], title: str, after: list[str] | None = None) -> None:
    """Run `cmd` in the current terminal if we have one, else open a new one."""
    if sys.stdin.isatty():
        print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        if after:
            rc = subprocess.run(cmd).returncode
            subprocess.run(after)
            sys.exit(rc)
        os.execvp(cmd[0], cmd)
    open_in_terminal(cmd, title, after)


def notify(message: str, title: str = "Baseten Monitor") -> None:
    message = message.replace('"', '\\"')
    title = title.replace('"', '\\"')
    subprocess.run(
        ["/usr/bin/osascript", "-e", f'display notification "{message}" with title "{title}"'],
        capture_output=True, timeout=5,
    )


def _confirm_dialog(message: str, button: str) -> bool:
    message = message.replace('"', '\\"')
    script = (
        f'display dialog "{message}" with title "AWS Dev Boxes" '
        f'buttons {{"Cancel", "{button}"}} default button "Cancel" '
        f'cancel button "Cancel" with icon caution'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=120
    )
    return result.returncode == 0 and button in result.stdout


def _find_box(cfg: dict, instance_id: str) -> dict | None:
    fetched = fetch_dev_boxes({**cfg, "boxes": [instance_id]})
    if fetched["error"]:
        notify(fetched["error"])
        return None
    for b in fetched["boxes"]:
        if b.get("id") == instance_id and not b.get("missing"):
            return b
    notify(f"Instance {instance_id} not found")
    return None


def action(name: str, instance_id: str, host: str = "") -> None:
    cfg = load_config()
    if cfg["error"]:
        notify(cfg["error"])
        return

    if name == "--aws-login":
        # Once the login finishes, restart the stream so the menu re-polls EC2
        # right away instead of waiting for the next scheduled poll.
        run_interactive(aws_login_cmd(cfg), "AWS sign in", after=REFRESH_SELF)
        return

    if name == "--aws-configure-sso":
        run_interactive(aws_configure_sso_cmd(cfg), "AWS SSO setup", after=REFRESH_SELF)
        return

    if name == "--aws-usage-interval":
        if not (instance_id.isdigit() and int(instance_id) in USAGE_INTERVAL_CHOICES):
            notify(f"Invalid usage interval: {instance_id[:10]}")
            return
        err = write_config_key("BTMON_USAGE_INTERVAL", instance_id)
        if err:
            notify(err)
            return
        # Restart the stream so the new period takes effect everywhere.
        subprocess.run(["/usr/bin/open", "-g", "swiftbar://refreshplugin?name=monitor"], capture_output=True)
        return

    if name in ("--aws-pin", "--aws-unpin"):
        if name == "--aws-pin" and instance_id not in SPECIAL_PINS and not INSTANCE_ID_RE.fullmatch(instance_id):
            notify(f"Invalid instance ID: {instance_id[:40]}")
            return
        err = write_config_key("BTMON_PINNED", instance_id if name == "--aws-pin" else "")
        if err:
            notify(err)
            return
        # Restart the stream so the menu bar switches right away.
        subprocess.run(["/usr/bin/open", "-g", "swiftbar://refreshplugin?name=monitor"], capture_output=True)
        return

    if not INSTANCE_ID_RE.fullmatch(instance_id):
        notify(f"Invalid instance ID: {instance_id[:40]}")
        return

    if name == "--aws-start":
        code, _, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "start-instances", "--instance-ids", instance_id])
        notify(f"Starting {instance_id}" if code == 0 else f"Start failed: {stderr.strip()[-160:]}")
    elif name == "--aws-stop":
        box = _find_box(cfg, instance_id)
        if not box:
            return
        if not _confirm_dialog(
            f"Stop dev box \"{box['name']}\" ({instance_id})?\n\nAnything running on it will be interrupted.",
            "Stop",
        ):
            return
        code, _, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "stop-instances", "--instance-ids", instance_id])
        notify(f"Stopping {box['name']}" if code == 0 else f"Stop failed: {stderr.strip()[-160:]}")
    elif name == "--aws-ssm":
        cmd = ["aws"]
        if cfg.get("profile"):
            cmd += ["--profile", cfg["profile"]]
        if cfg.get("region"):
            cmd += ["--region", cfg["region"]]
        cmd += ["ssm", "start-session", "--target", instance_id]
        run_interactive(cmd, f"SSM {instance_id}")
    elif name in ("--aws-ssh", "--aws-htop"):
        # The menu passes the host it already knows so the window opens at
        # once; fall back to an EC2 lookup when run by hand.
        label = instance_id
        if host and not HOST_RE.fullmatch(host):
            notify(f"Invalid host: {host[:60]}")
            return
        if not host:
            box = _find_box(cfg, instance_id)
            if not box:
                return
            host = box.get("public_dns") or box.get("public_ip")
            label = box["name"]
            if not host:
                notify(f"{box['name']} has no public address; use SSM instead.")
                return
        cmd = ssh_base_cmd(cfg, host, batch=False)
        if name == "--aws-htop":
            cmd += ["-t", "command -v htop >/dev/null 2>&1 && exec htop || exec top"]
        run_interactive(cmd, f"{'htop' if name == '--aws-htop' else 'ssh'} {label}")


def setup() -> None:
    """Interactive setup, run in a Terminal from the menu or by hand."""
    existing = load_config()
    print("AWS dev box setup")
    print("=" * 40)
    print(
        "This stores ONLY the profile, region, which instances to show, and the\n"
        f"SSH user/key path, in  {CONFIG_PATH}  (mode 0600).\n"
        "No credentials are stored. The plugin uses your AWS CLI sign-in.\n"
    )
    if existing["error"]:
        print(f"Current config problem: {existing['error']}\n")

    default_profile = existing.get("profile") or os.environ.get("AWS_PROFILE") or "default"
    profile = input(f"AWS profile [{default_profile}]: ").strip() or default_profile
    if not PROFILE_RE.fullmatch(profile):
        print("Profile name has unexpected characters; aborting.")
        return
    region = input(f"AWS region (blank = profile default) [{existing.get('region') or ''}]: ").strip()
    region = region or existing.get("region", "")
    if region and not REGION_RE.fullmatch(region):
        print("That does not look like an AWS region; aborting.")
        return
    cfg = {"profile": "" if profile == "default" else profile, "region": region}

    print("\nLooking up instances you can see…")
    stdout = ""
    for attempt in range(2):
        code, stdout, stderr = _run_aws(aws_base_cmd(cfg) + ["ec2", "describe-instances"])
        if code == 0:
            break
        if _looks_like_auth_error(stderr) and attempt == 0:
            if not profile_uses_sso(cfg):
                print("Not signed in.")
                print("If your org uses IAM Identity Center (a start URL like https://xxx.awsapps.com/start),")
                print("answer y to run `aws configure sso`, which asks for that URL and writes it to ~/.aws/config.")
                ans = input("Set up SSO sign-in now? [y/N] ").strip().lower()
                if ans == "y":
                    subprocess.run(aws_configure_sso_cmd(cfg))
                    continue
            login = aws_login_cmd(cfg)
            print(f"Running: {' '.join(login)}")
            subprocess.run(login)
            continue
        print(f"aws failed: {stderr.strip()[-300:]}")
        return

    instances = []
    for res in json.loads(stdout or "{}").get("Reservations", []):
        for inst in res.get("Instances", []):
            box = _instance_from_json(inst)
            if box["state"] != "terminated":
                instances.append(box)
    instances.sort(key=lambda b: (b["name"].lower(), b["id"]))
    if instances:
        print()
        for i, b in enumerate(instances, 1):
            print(f"  {i:>3}. {b['name']:<32} {b['id']:<21} {b['state']:<9} {b['type']}")
        print()
    else:
        print("No instances visible with this profile/region.")

    current = ", ".join(existing.get("boxes", []))
    print("Enter your dev boxes as numbers from the list, instance IDs, or Name tags,")
    print("separated by commas.")
    raw = input(f"Dev boxes [{current}]: ").strip() or current
    boxes = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.isdigit() and 1 <= int(item) <= len(instances):
            boxes.append(instances[int(item) - 1]["id"])
        elif INSTANCE_ID_RE.fullmatch(item) or INSTANCE_NAME_RE.fullmatch(item):
            boxes.append(item)
        else:
            print(f"Skipping invalid entry: {item!r}")
    if not boxes:
        print("No dev boxes selected; nothing written.")
        return

    ssh_user = input(f"SSH username (blank = use ~/.ssh/config) [{existing.get('ssh_user') or ''}]: ").strip()
    ssh_user = ssh_user or existing.get("ssh_user", "")
    if ssh_user and not SSH_USER_RE.fullmatch(ssh_user):
        print("SSH username has unexpected characters; leaving blank.")
        ssh_user = ""

    ssh_key = input(
        f"SSH private key path, e.g. ~/.ssh/my-key.pem (blank = none) [{existing.get('ssh_key') or ''}]: "
    ).strip() or existing.get("ssh_key", "")
    if ssh_key:
        expanded = os.path.expanduser(ssh_key)
        problem = "" if os.path.isfile(expanded) else "not found"
        problem = problem or _secure_file_problem(expanded)
        if problem:
            print(f"Key file {problem}. Fix with: chmod 600 {shlex.quote(expanded)}  — leaving blank.")
            ssh_key = ""

    lines = [
        "# AWS dev boxes for the SwiftBar monitor. No credentials belong in this file.",
        "# The plugin signs in via the AWS CLI (aws login / aws sso login).",
        f"AWS_PROFILE={profile}",
    ]
    if region:
        lines.append(f"AWS_REGION={region}")
    lines.append(f"BTMON_DEV_BOXES={','.join(boxes)}")
    if ssh_user:
        lines.append(f"BTMON_SSH_USER={ssh_user}")
    if ssh_key:
        lines.append(f"BTMON_SSH_KEY={ssh_key}")
    if existing.get("pinned"):
        lines.append(f"BTMON_PINNED={existing['pinned']}")
    if existing.get("usage_interval", DEFAULT_USAGE_INTERVAL) != DEFAULT_USAGE_INTERVAL:
        lines.append(f"BTMON_USAGE_INTERVAL={existing['usage_interval']}")

    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"\nWrote {CONFIG_PATH}")
    subprocess.run(["/usr/bin/open", "-g", "swiftbar://refreshplugin?name=monitor"], capture_output=True)


INSTANCE_ACTIONS = {"--aws-start", "--aws-stop", "--aws-ssm", "--aws-ssh", "--aws-htop", "--aws-pin", "--aws-usage-interval"}

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) in (2, 3) and args[0] in INSTANCE_ACTIONS:
        action(args[0], args[1], args[2] if len(args) == 3 else "")
    elif len(args) == 2 and args[0] == "--open-wandb":
        open_wandb_run(args[1])
    elif args in (["--aws-login"], ["--aws-unpin"], ["--aws-configure-sso"]):
        action(args[0], "")
    elif args == ["--aws-setup"]:
        if sys.stdin.isatty():
            setup()
        else:  # launched from the menu: get a terminal first, then run setup in it
            open_in_terminal([sys.executable, os.path.abspath(__file__), "--aws-setup"], "AWS dev box setup")
    elif args == ["--once"]:
        # Debug helper: one EC2 poll, a few seconds of metrics, one frame.
        st = State()
        threading.Thread(target=st.poll_baseten, daemon=True).start()
        threading.Thread(target=st.poll_local, daemon=True).start()
        st.poll_ec2()
        time.sleep(float(os.environ.get("BTMON_ONCE_WAIT", "5")))
        print(render(st.snapshot()))
    else:
        stream_forever()
