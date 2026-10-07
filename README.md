# dev-dashboard

One page, at `https://<this machine's MagicDNS name>:8000`, listing the dev
servers this machine publishes on the tailnet. Each card shows the git
worktree behind the server (branch, ahead/behind `main`, uncommitted
changes, last commit) and that worktree's T3 Code thread (title, whether the
agent is working or needs you, linked PRs). The page reloads every 30 seconds.

Nothing is configured per project. For each `tailscale serve` proxy, the
dashboard finds the process listening on the target port with `ss`, reads its
working directory from `/proc`, and asks git about that directory. So a
project only has to publish its dev server, for example:

```
tailscale serve --bg --https=3010 http://127.0.0.1:3010
```

observe4success's `just dev` does this when `O4S_DEV_TAILSCALE=1` is set.

The page also lists T3 Code worktrees with no running server, other published
servers (such as T3 Code itself), and published ports with nothing listening.

## Commands

```
./dashboard.py serve [port]    # publish the page and serve it (default 8000)
./dashboard.py install [port]  # run it as the dev-dashboard systemd user service
./dashboard.py uninstall       # stop and remove the service
./dashboard.py prune           # remove same-port tailscale serve entries with nothing listening
```

Logs: `journalctl --user -u dev-dashboard`. After editing `dashboard.py`, run
`systemctl --user restart dev-dashboard`.

## Requirements

Linux, Python 3.10+ (standard library only), git, `ss`, and Tailscale with
this user as operator (`sudo tailscale set --operator=$USER`). The service
keeps running while logged out only with `sudo loginctl enable-linger $USER`.
