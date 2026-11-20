#!/usr/bin/env python3
"""Relevé de l'état du VPS, en lecture seule, au format JSON sur la sortie standard.

Installé dans /usr/local/sbin/vps-status par install.sh et lancé chaque matin par
GitHub Actions (dépôt du vault) avec une clé SSH bridée : dans authorized_keys,
`restrict,command="/usr/local/sbin/vps-status"` fait que cette clé ne peut lancer
que ce script, sans terminal ni redirection, quelle que soit la commande demandée.

Ce script ne modifie rien et ne lit aucun secret : jamais de .env, de clé privée,
de donnée bancaire ni de variable d'environnement de conteneur. Il n'utilise que
la bibliothèque standard de Python (présente d'office sur Ubuntu).
"""

import calendar
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import time

VERSION = 1
TIMEOUT = 20
# Dépôts git déployés sur le VPS : on relève seulement le dernier commit.
DEPLOYED_REPOS = ["/root/mcp", "/root/revolut-mcp"]
# Certificats publics obtenus par Caddy (jamais les fichiers .key).
CERT_GLOB = "/var/lib/docker/volumes/*caddy_data/_data/caddy/certificates/*/*/*.crt"


def run(cmd):
    """Lance une commande en lecture seule ; renvoie (code, sortie) sans jamais lever."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "LC_ALL": "C"},
        )
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def section(fn):
    """Une section qui échoue n'empêche pas les autres : on note l'erreur."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:300]}


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def system():
    os_release = dict(
        line.split("=", 1) for line in read("/etc/os-release").splitlines() if "=" in line
    )
    uptime = float(read("/proc/uptime").split()[0])
    load = [float(x) for x in read("/proc/loadavg").split()[:3]]
    return {
        "hostname": socket.gethostname(),
        "os": os_release.get("PRETTY_NAME", "").strip('"'),
        "kernel": os.uname().release,
        "uptime_s": int(uptime),
        "load": load,
        "cpus": os.cpu_count(),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }


def memory():
    info = {}
    for line in read("/proc/meminfo").splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0]) * 1024
    return {
        "total": info["MemTotal"],
        "available": info["MemAvailable"],
        "swap_total": info.get("SwapTotal", 0),
        "swap_free": info.get("SwapFree", 0),
    }


def disks():
    out = []
    for mount in ["/"]:
        u = shutil.disk_usage(mount)
        out.append({"mount": mount, "total": u.total, "used": u.used, "free": u.free})
    code, text = run(["docker", "system", "df", "--format", "{{json .}}"])
    docker = [json.loads(l) for l in text.splitlines() if l.strip()] if code == 0 else None
    return {"filesystems": out, "docker": docker}


def docker():
    code, text = run(["docker", "ps", "-aq"])
    if code != 0:
        return {"error": "docker indisponible"}
    ids = text.split()
    if not ids:
        return {"containers": [], "projects": []}
    code, text = run(["docker", "inspect", *ids])
    containers = []
    for c in json.loads(text) if code == 0 else []:
        labels = c.get("Config", {}).get("Labels") or {}
        state = c.get("State", {})
        ports = []
        for port, bindings in (c.get("NetworkSettings", {}).get("Ports") or {}).items():
            for b in bindings or []:
                ports.append({"container": port, "host_ip": b.get("HostIp"),
                              "host_port": b.get("HostPort")})
        containers.append({
            # Pas de Config.Env : les variables d'environnement contiennent des secrets.
            "name": c.get("Name", "").lstrip("/"),
            "image": c.get("Config", {}).get("Image"),
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "state": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "started_at": state.get("StartedAt"),
            "restart_count": c.get("RestartCount", 0),
            "restart_policy": (c.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name"),
            "networks": sorted((c.get("NetworkSettings", {}).get("Networks") or {}).keys()),
            "ports": ports,
            "created": c.get("Created"),
        })
    code, text = run(["docker", "compose", "ls", "-a", "--format", "json"])
    projects = []
    if code == 0 and text.strip():
        for p in json.loads(text):
            projects.append({"name": p.get("Name"), "status": p.get("Status"),
                             "config": p.get("ConfigFiles")})
    return {"containers": containers, "projects": projects}


def listening():
    code, text = run(["ss", "-tulnpH"])
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto, local = parts[0], parts[4]
        addr, _, port = local.rpartition(":")
        procs = re.findall(r'\("([^"]+)"', line)
        out.append({"proto": proto, "address": addr.strip("[]"), "port": port,
                    "process": sorted(set(procs))})
    return out


def certificates():
    out = []
    for path in sorted(glob.glob(CERT_GLOB)):
        code, text = run(["openssl", "x509", "-in", path, "-noout", "-enddate", "-issuer"])
        end = re.search(r"notAfter=(.+)", text)
        issuer = re.search(r"issuer=.*?O ?= ?([^,\n]+)", text)
        ts = None
        if end:
            ts = calendar.timegm(time.strptime(end.group(1).strip(), "%b %d %H:%M:%S %Y GMT"))
        out.append({"domain": os.path.basename(path)[:-4], "expires": ts,
                    "issuer": issuer.group(1).strip() if issuer else None})
    return out


def fail2ban():
    code, text = run(["fail2ban-client", "status"])
    if code != 0:
        return {"running": False}
    m = re.search(r"Jail list:\s*(.*)", text)
    jails = [j.strip() for j in (m.group(1) if m else "").split(",") if j.strip()]
    out = {"running": True, "jails": {}}
    for jail in jails:
        _, t = run(["fail2ban-client", "status", jail])
        def num(label):
            mm = re.search(label + r":\s*(\d+)", t)
            return int(mm.group(1)) if mm else None
        # Les adresses bannies ne sont pas relevées : seulement les compteurs.
        out["jails"][jail] = {"banned_now": num("Currently banned"),
                              "banned_total": num("Total banned"),
                              "failed_now": num("Currently failed")}
    return out


def ssh():
    keys = []
    path = "/root/.ssh/authorized_keys"
    if os.path.exists(path):
        for line in read(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # On garde le commentaire et le bridage éventuel, jamais la clé elle-même.
            m = re.search(r"(ssh-[a-z0-9-]+|ecdsa-[a-z0-9-]+)\s+\S+\s*(.*)$", line)
            forced = re.search(r'command="([^"]*)"', line)
            keys.append({"comment": (m.group(2) if m else "").strip()[:60],
                         "forced_command": forced.group(1) if forced else None})
    code, text = run(["journalctl", "--since", "-24h", "-q", "-o", "cat",
                      "-t", "sshd", "-t", "sshd-session"])
    logins = None
    if code == 0:
        logins = {"accepted": len(re.findall(r"^Accepted ", text, re.M)),
                  "failed": len(re.findall(r"^(Failed |Invalid user )", text, re.M))}
    cfg = {}
    code, text = run(["sshd", "-T"])
    for line in text.splitlines():
        k, _, v = line.partition(" ")
        if k in ("passwordauthentication", "permitrootlogin", "pubkeyauthentication",
                 "kbdinteractiveauthentication"):
            cfg[k] = v
    return {"authorized_keys": keys, "logins_24h": logins, "config": cfg}


def updates():
    code, text = run(["apt", "list", "--upgradable"])
    pkgs = [l for l in text.splitlines() if "/" in l and "upgradable" in l]
    auto = None
    if os.path.exists("/etc/apt/apt.conf.d/20auto-upgrades"):
        auto = 'Unattended-Upgrade "1"' in read("/etc/apt/apt.conf.d/20auto-upgrades")
    last_update = None
    stamp = "/var/lib/apt/periodic/update-success-stamp"
    if os.path.exists(stamp):
        last_update = int(os.path.getmtime(stamp))
    return {
        "upgradable": len(pkgs),
        "security": sum(1 for p in pkgs if "-security" in p),
        "packages": [p.split("/")[0] for p in pkgs][:40],
        "unattended_upgrades": auto,
        "apt_last_update": last_update,
    }


def systemd():
    code, text = run(["systemctl", "--failed", "--no-legend", "--plain"])
    failed = [l.split()[0] for l in text.splitlines() if l.strip()]
    status = {}
    for unit in ("docker", "ssh", "fail2ban", "unattended-upgrades"):
        _, t = run(["systemctl", "is-active", unit])
        status[unit] = t.strip() or "inconnu"
    return {"failed": failed, "services": status}


def firewall():
    code, text = run(["ufw", "status"])
    if code == 127:
        return {"ufw": "absent"}
    return {"ufw": text.splitlines()[0].replace("Status: ", "") if text else "inconnu"}


def deployed():
    out = []
    for repo in DEPLOYED_REPOS:
        if not os.path.isdir(os.path.join(repo, ".git")):
            out.append({"path": repo, "git": False, "exists": os.path.isdir(repo)})
            continue
        code, text = run(["git", "-c", f"safe.directory={repo}", "-C", repo, "log", "-1",
                          "--format=%h%x09%cI%x09%s"])
        h, date, subject = (text.strip().split("\t", 2) + ["", "", ""])[:3]
        out.append({"path": repo, "git": True, "commit": h, "date": date,
                    "subject": subject[:100]})
    return out


def main():
    report = {"version": VERSION, "collected_at": int(time.time())}
    for name, fn in [("system", system), ("memory", memory), ("disks", disks),
                     ("docker", docker), ("listening", listening),
                     ("certificates", certificates), ("fail2ban", fail2ban), ("ssh", ssh),
                     ("updates", updates), ("systemd", systemd), ("firewall", firewall),
                     ("deployed", deployed)]:
        report[name] = section(fn)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
