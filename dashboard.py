#!/usr/bin/env python3
"""A page listing the dev servers this machine publishes on the tailnet, with each one's git worktree and T3 Code thread.

  dashboard.py serve [port]   # publish https://<MagicDNS name>:<port> and serve the page (default 8000)
  dashboard.py prune          # remove `tailscale serve` entries whose server is gone
  dashboard.py install [port] # run `serve` as a systemd user service
  dashboard.py uninstall      # stop and remove the service

Nothing is configured per project. For each `tailscale serve` proxy, the page finds the process
listening on the target port, reads its working directory, and shows that directory's git state.
T3 Code's database supplies the thread title for a worktree. Every request reads the state again.

Linux only: it uses `ss` and /proc. `tailscale serve` without sudo needs
`sudo tailscale set --operator=$USER` once.
"""

import html
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_PORT = 8000
UNIT = "dev-dashboard"
T3_USERDATA = Path.home() / ".t3" / "userdata"
# The page reloads itself this often, in seconds.
REFRESH_SECONDS = 30


def run(*args: str, cwd: Path | None = None) -> str:
    """Return a command's stdout, or "" when it fails or is missing."""
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


# ---- Tailscale ----------------------------------------------------------------------------------


def dns_name() -> str | None:
    try:
        status = json.loads(run("tailscale", "status", "--json") or "{}")
    except json.JSONDecodeError:
        return None
    return status.get("Self", {}).get("DNSName", "").rstrip(".") or None


def serve_entries() -> list[dict]:
    """Each `tailscale serve` proxy to this machine, as {url, public_port, path, target_port}."""
    try:
        web = json.loads(run("tailscale", "serve", "status", "--json") or "{}").get("Web", {})
    except json.JSONDecodeError:
        return []
    entries = []
    for host_port, config in web.items():
        host, _, public_port = host_port.rpartition(":")
        for path, handler in config.get("Handlers", {}).items():
            target = urlsplit(handler.get("Proxy", ""))
            if target.hostname not in ("127.0.0.1", "localhost") or not target.port:
                continue
            suffix = "" if public_port == "443" else f":{public_port}"
            entries.append({
                "url": f"https://{host}{suffix}{path}",
                "public_port": int(public_port),
                "path": path,
                "target_port": target.port,
            })
    entries.sort(key=lambda entry: entry["public_port"])
    return entries


def publish(port: int) -> str | None:
    """Publish 127.0.0.1:<port> at https://<name>:<port>, unless it already is. Return the URL."""
    name = dns_name()
    if name is None:
        return None
    url = f"https://{name}:{port}/"
    if not any(entry["url"] == url for entry in serve_entries()):
        run("tailscale", "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}")
    return url if any(entry["url"] == url for entry in serve_entries()) else None


# ---- Local processes and git --------------------------------------------------------------------


def listeners() -> dict[int, dict]:
    """Listening TCP ports, as port -> {process, pid}. pid is None for other users' processes."""
    found = {}
    for line in run("ss", "-ltnpH").splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        port = fields[3].rpartition(":")[2]
        if not port.isdigit():
            continue
        match = re.search(r'\("([^"]+)",pid=(\d+)', line)
        found.setdefault(int(port), {"process": match[1] if match else None,
                                     "pid": int(match[2]) if match else None})
    return found


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def process_cwd(pid: int | None) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd")) if pid else None
    except OSError:
        return None


def git_state(directory: Path) -> dict | None:
    """The worktree containing directory and its git state, or None outside git."""
    top = run("git", "rev-parse", "--show-toplevel", cwd=directory).strip()
    if not top:
        return None
    path = Path(top)
    common = run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path).strip()
    project = Path(common).parent.name if common else path.name
    branch = run("git", "branch", "--show-current", cwd=path).strip()
    head = run("git", "rev-parse", "--short", "HEAD", cwd=path).strip()
    subject, _, timestamp = run("git", "log", "-1", "--format=%s%x1f%ct", cwd=path).strip().partition("\x1f")
    base = next((ref for ref in ("main", "master")
                 if run("git", "rev-parse", "--verify", "-q", ref, cwd=path)), None)
    counts = run("git", "rev-list", "--left-right", "--count", f"{base}...HEAD", cwd=path).split() if base else []
    return {
        "path": path,
        "project": project,
        "main": common == str(path / ".git"),
        "branch": branch or None,
        "head": head,
        "subject": subject,
        "committed_at": int(timestamp) if timestamp.isdigit() else None,
        "base": base,
        "behind": int(counts[0]) if len(counts) == 2 else 0,
        "ahead": int(counts[1]) if len(counts) == 2 else 0,
        "changes": len(run("git", "status", "--porcelain", cwd=path).splitlines()),
    }


# ---- T3 Code ------------------------------------------------------------------------------------


def t3_threads() -> dict[str, dict]:
    """The most recently updated open T3 Code thread for each worktree path. Empty when T3 Code's state cannot be read."""
    database = T3_USERDATA / "state.sqlite"
    if not database.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1)
        try:
            threads = {}
            for thread_id, title, path, updated_at, waiting, status in connection.execute(
                """
                select t.thread_id, t.title, t.worktree_path, t.updated_at,
                       t.pending_approval_count + t.pending_user_input_count, s.status
                from projection_threads t
                left join projection_thread_sessions s on s.thread_id = t.thread_id
                where t.deleted_at is null and t.archived_at is null and t.worktree_path is not null
                order by t.updated_at
                """
            ):
                threads[path] = {"id": thread_id, "title": title, "updated_at": updated_at,
                                 "waiting": waiting or 0, "status": status, "pulls": []}
            by_id = {thread["id"]: thread for thread in threads.values()}
            for thread_id, url, number, snapshot in connection.execute(
                "select thread_id, url, number, snapshot_json from projection_thread_pull_requests order by number"
            ):
                if thread_id in by_id:
                    try:
                        state = (json.loads(snapshot) if snapshot else {}).get("state")
                    except (json.JSONDecodeError, AttributeError):
                        state = None
                    by_id[thread_id]["pulls"].append({"url": url, "number": number, "state": state})
            return threads
        finally:
            connection.close()
    except sqlite3.Error:
        return {}


def t3_port() -> int | None:
    try:
        return int(json.loads((T3_USERDATA / "server-runtime.json").read_text())["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---- Collecting the page ------------------------------------------------------------------------


def collect(own_port: int) -> dict:
    entries = [entry for entry in serve_entries() if entry["target_port"] != own_port]
    listening_on = listeners()
    threads = t3_threads()
    special = {t3_port(): "T3 Code"}

    # Group the entries by the server behind them.
    servers: dict[int, dict] = {}
    for entry in entries:
        servers.setdefault(entry["target_port"], {"port": entry["target_port"], "entries": []})["entries"].append(entry)

    def describe(server: dict) -> dict:
        port = server["port"]
        listener = listening_on.get(port)
        server["up"] = listener is not None or listening(port)
        server["process"] = (listener or {}).get("process")
        cwd = process_cwd((listener or {}).get("pid"))
        server["git"] = git_state(cwd) if cwd and port not in special else None
        # Prefer the entry that keeps the port number, e.g. :3010 for 3010 over an older :8443 for 3000.
        server["entries"].sort(key=lambda entry: (entry["public_port"] != port, entry["path"] != "/"))
        return server

    with ThreadPoolExecutor(max_workers=8) as pool:
        described = list(pool.map(describe, servers.values()))

    worktrees = [server for server in described if server["up"] and server["git"]]
    for server in worktrees:
        server["thread"] = threads.get(str(server["git"]["path"]))
    others = [server for server in described if server["up"] and not server["git"]]
    for server in others:
        server["label"] = special.get(server["port"]) or server["process"] or f"127.0.0.1:{server['port']}"
    down = [server for server in described if not server["up"]]

    # Worktrees T3 Code knows about that have no published server running.
    serving = {str(server["git"]["path"]) for server in worktrees}
    idle = []
    for path, thread in threads.items():
        if path not in serving and Path(path).is_dir():
            idle.append({"path": Path(path), "thread": thread})
    idle.sort(key=lambda row: row["thread"]["updated_at"], reverse=True)

    return {"worktrees": worktrees, "others": others, "down": down, "idle": idle, "dns_name": dns_name()}


# ---- Rendering ----------------------------------------------------------------------------------

e = html.escape


def parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def ago(moment: datetime | None) -> str:
    if moment is None:
        return ""
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def last_activity(server: dict) -> float:
    moments = [server["git"]["committed_at"] or 0]
    if (updated := parse_time((server.get("thread") or {}).get("updated_at"))) is not None:
        moments.append(updated.timestamp())
    return max(moments)


def thread_line(thread: dict) -> str:
    if thread["waiting"]:
        status = '<span class="pill waiting">needs you</span>'
    elif thread["status"] == "running":
        status = '<span class="pill working">working</span>'
    else:
        status = '<span class="pill off">idle</span>'
    pulls = " ".join(
        f'<a href="{e(pull["url"])}">#{pull["number"]}</a>' + (f' {e(pull["state"])}' if pull["state"] else "")
        for pull in thread["pulls"]
    )
    return f'T3 {status} {pulls} <span class="hint">{ago(parse_time(thread["updated_at"]))}</span>'


def render_worktree(server: dict) -> str:
    git, thread = server["git"], server.get("thread")
    if thread:
        title = thread["title"]
    elif git["main"]:
        title = "Main checkout"
    else:
        title = git["branch"] or git["path"].name
    first, *more = server["entries"]
    also = " ".join(f'<a class="hint" href="{e(entry["url"])}">also :{entry["public_port"]}{e(entry["path"].rstrip("/"))}</a>'
                    for entry in more)

    details = [f'<code>{e(git["branch"] or "detached " + git["head"])}</code>']
    if git["ahead"] or git["behind"]:
        details.append(f'{git["ahead"]} ahead, {git["behind"]} behind {e(git["base"])}')
    if git["changes"]:
        details.append(f'{git["changes"]} uncommitted')
    lines = [" · ".join(details)]
    if git["subject"]:
        committed = datetime.fromtimestamp(git["committed_at"], timezone.utc) if git["committed_at"] else None
        lines.append(f'{e(git["subject"])} <span class="hint">{ago(committed)}</span>')
    if thread:
        lines.append(thread_line(thread))
    lines.append(f'<span class="hint">{e(str(git["path"]))}</span>')

    body = "".join(f"<div>{line}</div>" for line in lines)
    return (f'<li class="card"><div class="head"><h3><a href="{e(first["url"])}">{e(title)}</a></h3>'
            f'<span class="pill up">:{server["port"]}</span>{(" " + also) if also else ""}</div>{body}</li>')


def render_simple(server: dict, label: str) -> str:
    links = " ".join(f'<a href="{e(entry["url"])}">{e(entry["url"])}</a>' for entry in server["entries"])
    return f'<li>{e(label)} <span class="hint">:{server["port"]}</span> {links}</li>'


def favicon(threads: list[dict]) -> str:
    """A server rack on an indigo tile, badged amber when a thread needs you and blue while one is working."""
    if any(thread["waiting"] for thread in threads):
        badge = "#f59e0b"
    elif any(thread["status"] == "running" for thread in threads):
        badge = "#3b82f6"
    else:
        badge = None
    racks = "".join(
        f'<rect x="10" y="{y}" width="44" height="11" rx="3" fill="#fff" fill-opacity=".95"/>'
        f'<rect x="15" y="{y + 4}" width="16" height="3" rx="1.5" fill="#6366f1" fill-opacity=".45"/>'
        f'<circle cx="46" cy="{y + 5.5}" r="2.6" fill="#22c55e"/>'
        for y in (12, 26.5, 41)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#6366f1"/><stop offset="1" stop-color="#a855f7"/></linearGradient></defs>'
        f'<rect width="64" height="64" rx="14" fill="url(#g)"/>{racks}'
        + (f'<circle cx="51" cy="13" r="10" fill="{badge}" stroke="#fff" stroke-width="3"/>' if badge else "")
        + "</svg>"
    )
    return "data:image/svg+xml," + quote(svg)


def collapsible(key: str, title: str, body: str) -> str:
    # Starts collapsed; the script at the bottom of the page keeps it open across auto-reloads.
    return f'<details data-key="{key}"><summary><h2>{e(title)}</h2></summary>{body}</details>'


def render(data: dict) -> str:
    projects: dict[str, list[dict]] = {}
    for server in sorted(data["worktrees"], key=lambda server: (not server["git"]["main"], -last_activity(server))):
        projects.setdefault(server["git"]["project"], []).append(server)
    sections = "".join(
        f'<h2>{e(project)}</h2><ul>{"".join(render_worktree(server) for server in servers)}</ul>'
        for project, servers in sorted(projects.items())
    ) or '<p class="hint">No published dev servers are running.</p>'

    others = "".join(render_simple(server, server["label"]) for server in data["others"])
    down = "".join(render_simple(server, "Nothing listening") for server in data["down"])
    idle = "".join(
        f'<li>{e(row["thread"]["title"])} <span class="hint">{e(row["path"].name)}</span> {thread_line(row["thread"])}</li>'
        for row in data["idle"]
    )
    extra = ""
    if others:
        extra += collapsible("others", "Other servers", f'<ul class="list">{others}</ul>')
    if idle:
        extra += collapsible("idle", "T3 Code worktrees without a running server", f'<ul class="list">{idle}</ul>')
    if down:
        extra += collapsible("down", "Published but down", f'<ul class="list">{down}</ul>'
                             '<p class="hint"><code>dashboard.py prune</code> removes the ones on the same port on both sides.</p>')
    tailnet_note = "" if data["dns_name"] else '<p class="hint">Tailscale is not running.</p>'
    threads = [server["thread"] for server in data["worktrees"] if server["thread"]]
    threads += [row["thread"] for row in data["idle"]]

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<title>Dev servers</title>
<link rel="icon" type="image/svg+xml" href="{favicon(threads)}">
<style>
  :root {{ color-scheme: light dark; --muted: #6b7280; --card: #f9fafb; --line: #e5e7eb; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --muted: #9ca3af; --card: #1f2937; --line: #374151; }} }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 860px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  h2 {{ font-size: 17px; margin: 20px 0 8px; }}
  h3 {{ font-size: 16px; margin: 0; flex: 1; min-width: 0; overflow-wrap: anywhere; }}
  ul {{ list-style: none; padding: 0; margin: 0; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin-bottom: 10px; }}
  .card > div, .list li {{ overflow-wrap: anywhere; }}
  .list li {{ padding: 4px 0; }}
  .head {{ display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; margin-bottom: 4px; }}
  .hint {{ color: var(--muted); font-size: 13px; }}
  .pill {{ font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 999px; white-space: nowrap; }}
  .up {{ background: #dcfce7; color: #166534; }}
  .off {{ background: #e5e7eb; color: #374151; }}
  .working {{ background: #dbeafe; color: #1e40af; }}
  .waiting {{ background: #fef3c7; color: #92400e; }}
  code {{ font-size: 13px; }}
  summary {{ cursor: pointer; margin: 20px 0 8px; }}
  summary h2 {{ display: inline; }}
</style>
</head>
<body>
<h1>Dev servers</h1>
{tailnet_note}
{sections}
{extra}
<p class="hint">Reloads every {REFRESH_SECONDS}s. {datetime.now().strftime("%H:%M:%S")}</p>
<script>
  for (const el of document.querySelectorAll("details[data-key]")) {{
    const key = "open:" + el.dataset.key;
    el.open = sessionStorage.getItem(key) === "1";
    el.addEventListener("toggle", () => sessionStorage.setItem(key, el.open ? "1" : "0"));
  }}
</script>
</body>
</html>
"""


# ---- Commands -----------------------------------------------------------------------------------


def serve(port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/":
                self.send_error(404)
                return
            body = render(collect(port)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = publish(port)
    print(f"Dashboard on {url}" if url else f"Could not publish port {port} with tailscale serve.", flush=True)
    print(f"Dashboard on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


def prune() -> None:
    # Only entries like the ones `tailscale serve --https=N N` makes: the same port on both sides.
    # A hand-made mapping such as :8443 -> 3000 stays even while its server is stopped.
    stale = [entry for entry in serve_entries()
             if entry["path"] == "/" and entry["public_port"] == entry["target_port"]
             and not listening(entry["target_port"])]
    if not stale:
        print("Every same-port entry has a server listening.")
    for entry in stale:
        print(f"Removing {entry['url']}")
        subprocess.run(["tailscale", "serve", f"--https={entry['public_port']}", "off"], check=True)


def unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "systemd" / "user" / f"{UNIT}.service"


def install(port: int) -> None:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""[Unit]
Description=Dev server dashboard ({Path(__file__).resolve()})
After=network-online.target

[Service]
Environment=PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
ExecStart={sys.executable} -u {Path(__file__).resolve()} serve {port}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", UNIT], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    print(f"Installed {path}. Logs: journalctl --user -u {UNIT}")
    if "Linger=yes" not in run("loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"):
        print("Run `sudo loginctl enable-linger $USER` so it also runs while you are logged out.")


def uninstall() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False)
    unit_path().unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Removed {UNIT}. The tailscale serve entry stays; `tailscale serve --https=<port> off` removes it.")


def main() -> None:
    command, *rest = sys.argv[1:] or ["serve"]
    port_arg = len(rest) <= 1 and all(arg.isdigit() for arg in rest)
    port = int(rest[0]) if rest and port_arg else DEFAULT_PORT
    if command == "serve" and port_arg:
        serve(port)
    elif command == "install" and port_arg:
        install(port)
    elif command == "prune" and not rest:
        prune()
    elif command == "uninstall" and not rest:
        uninstall()
    else:
        print(__doc__, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
