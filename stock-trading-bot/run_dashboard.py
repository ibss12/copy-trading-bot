"""Open the live command center in your browser: python run_dashboard.py"""

import argparse
import logging
import threading
import webbrowser

import uvicorn

import config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Keep 127.0.0.1 so only this computer can reach it")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab automatically")
    args = parser.parse_args()
    config.validate()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not config.DASHBOARD_PASSWORD:
        raise SystemExit(
            "Refusing to listen on other devices without a password: set DASHBOARD_PASSWORD in "
            "stock-trading-bot/.env (or keep --host 127.0.0.1)."
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    print(f"\n  Command center: {url}\n")
    if not args.no_browser:
        threading.Timer(2.5, webbrowser.open, args=(url,)).start()
    # Trusts X-Forwarded-For/-Proto only from a proxy on this machine (Caddy on the cloud install).
    uvicorn.run(
        "dashboard.server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
