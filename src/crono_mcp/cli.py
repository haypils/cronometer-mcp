"""crono-mcp command line: auth | status | fetch | logout | serve."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from crono_mcp import auth, shape
from crono_mcp.client import AuthError, CronometerClient, CronometerError


def main() -> None:
    p = argparse.ArgumentParser(prog="crono-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="Import Cronometer session from your browser")
    a.add_argument("--browser", default="chrome")
    sub.add_parser("status", help="Check stored session")
    f = sub.add_parser("fetch", help="Print an export (for testing)")
    f.add_argument("type", choices=["servings", "daily_summary", "notes", "exercises", "biometrics"])
    f.add_argument("start", nargs="?", default=date.today().isoformat())
    f.add_argument("end", nargs="?")
    f.add_argument("--raw", action="store_true", help="print raw CSV")
    sub.add_parser("logout", help="Delete stored session from keychain")
    sub.add_parser("serve", help="Run the MCP server (stdio)")
    args = p.parse_args()

    if args.cmd == "serve":
        from crono_mcp.server import serve
        serve()
        return
    if args.cmd == "logout":
        auth.clear()
        print("Stored Cronometer session removed.")
        return

    client = CronometerClient()
    try:
        if args.cmd == "auth":
            print(client.reload_from_browser(args.browser))
            print(f"Session OK (user id {client.check()}). Stored in keychain.")
        elif args.cmd == "status":
            print(f"Session OK (user id {client.check()}).")
        elif args.cmd == "fetch":
            d0 = date.fromisoformat(args.start)
            d1 = date.fromisoformat(args.end) if args.end else d0
            if args.raw:
                print(client.export_csv(args.type, d0, d1))
            else:
                rows = client.export_rows(args.type, d0, d1)
                out = shape.servings_by_day(rows) if args.type == "servings" else (
                    shape.daily_summaries(rows) if args.type == "daily_summary" else shape.compact_rows(rows))
                print(json.dumps(out, indent=1))
    except (AuthError, CronometerError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
