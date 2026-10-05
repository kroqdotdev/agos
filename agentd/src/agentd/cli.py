"""agentd command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from typing import Any

from agentd import __version__
from agentd.auth import SCOPES, TokenStore, parse_scopes
from agentd.config import Config, load_config
from agentd.errors import AgentdError


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agentd", description="agos computer-use control daemon")
    ap.add_argument("--version", action="version", version=f"agentd {__version__}")
    ap.add_argument("--config", help="config file (default ~/.config/agentd/config.toml)")
    ap.add_argument("--display", help="X display to drive, e.g. :1")
    ap.add_argument("--socket", help="Unix socket path of the server")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP + Unix socket server (REST, MCP, UI)")
    s.add_argument("--listen", help="TCP host:port ('' disables TCP)")

    m = sub.add_parser("mcp", help="MCP over stdio")
    m.add_argument(
        "--mode",
        choices=("auto", "local", "remote"),
        help="remote = forward to `agentd serve` over its socket; local = drive the "
        "display in-process; auto = remote if the server answers",
    )

    st = sub.add_parser("status", help="show server status")
    st.add_argument("--json", action="store_true")

    t = sub.add_parser("takeover", help="a human takes control; agent input pauses")
    t.add_argument("--reason", default="")
    t.add_argument("--by", default=None)
    t.add_argument("--ttl", type=float, default=None, help="hand back automatically after N seconds")

    sub.add_parser("handback", help="return control to the agent (invalidates old screenshots)")

    tok = sub.add_parser("token", help="manage bearer tokens")
    toks = tok.add_subparsers(dest="token_cmd", required=True)
    c = toks.add_parser("create", help="mint a token (printed once; only its hash is stored)")
    c.add_argument("--scopes", required=True, help=f"comma-separated: {','.join(SCOPES)}")
    c.add_argument("--name", default=None)
    c.add_argument("--stdin", action="store_true", help="store a token read from stdin instead of minting one")
    toks.add_parser("list", help="list token names and scopes")
    r = toks.add_parser("revoke", help="delete a token by name")
    r.add_argument("name")
    return ap


def _config(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {"display": args.display, "socket": args.socket}
    if getattr(args, "listen", None) is not None:
        overrides["listen"] = args.listen
    if getattr(args, "mode", None) is not None:
        overrides["mcp_mode"] = args.mode
    return load_config(args.config, overrides)


def _unix(cfg: Config, method: str, url: str, body: Any = None) -> Any:
    from agentd.service import unix_request

    try:
        status, _, data = unix_request(cfg.socket, method, url, body, timeout=30)
    except OSError as exc:
        raise SystemExit(f"agentd: cannot reach the server at {cfg.socket}: {exc}") from None
    if status >= 400:
        err = AgentdError.from_json(data or {}, status)
        raise SystemExit(f"agentd: {err.code}: {err.message}")
    return data


def _print_status(s: dict[str, Any]) -> None:
    screen = "x".join(map(str, s["screen"])) if s.get("screen") else "unavailable"
    print(f"agentd {s.get('version')} on {s.get('display')} ({screen})")
    lease = s.get("lease") or {}
    if lease.get("held"):
        reason = f" - {lease['reason']}" if lease.get("reason") else ""
        print(f"input: paused, {lease.get('by')} took over at {lease.get('since')}{reason}")
    else:
        print("input: agent")
    print(
        f"epoch {s.get('epoch')}, frame {s.get('frame_id')}, {s.get('session_count')} session(s), "
        f"up {s.get('uptime_s')}s"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        cfg = _config(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"agentd: config: {exc}", file=sys.stderr)
        return 1

    if args.cmd == "serve":
        from agentd.server import serve

        try:
            asyncio.run(serve(cfg))
        except RuntimeError as exc:
            print(f"agentd: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "mcp":
        from agentd.stdio import run_stdio

        run_stdio(cfg, display_overridden=args.display is not None)
        return 0

    if args.cmd == "status":
        s = _unix(cfg, "GET", "/v1/status")
        print(json.dumps(s, indent=2)) if args.json else _print_status(s)
        return 0

    if args.cmd == "takeover":
        body = {"by": args.by or f"{os.environ.get('USER', 'human')}@cli", "reason": args.reason, "ttl": args.ttl}
        lease = _unix(cfg, "POST", "/v1/takeover", body)
        print(f"taken over by {lease.get('by')}; agent input is paused until `agentd handback`")
        return 0

    if args.cmd == "handback":
        res = _unix(cfg, "DELETE", "/v1/takeover", {"by": f"{os.environ.get('USER', 'human')}@cli"})
        print(f"handed back; epoch is now {res.get('epoch')}" if res.get("changed") else "no takeover was active")
        return 0

    if args.cmd == "token":
        store = TokenStore(cfg.tokens_path)
        try:
            if args.token_cmd == "create":
                scopes = parse_scopes(args.scopes)
                name = args.name or f"token-{secrets.token_hex(3)}"
                given = sys.stdin.readline().strip() if args.stdin else None
                token = store.create(name, scopes, given)
                if not args.stdin:
                    print(token)
                print(f"stored {name} ({','.join(scopes)}) in {cfg.tokens_path}", file=sys.stderr)
            elif args.token_cmd == "list":
                for e in store.entries():
                    print(f"{e.name}\t{','.join(e.scopes)}\t{e.created}")
            elif args.token_cmd == "revoke":
                if not store.revoke(args.name):
                    print(f"agentd: no token named {args.name!r}", file=sys.stderr)
                    return 1
        except ValueError as exc:
            print(f"agentd: {exc}", file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
