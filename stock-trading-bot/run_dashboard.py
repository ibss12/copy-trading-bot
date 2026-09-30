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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    print(f"\n  Command center: {url}\n")
    if not args.no_browser:
        threading.Timer(2.5, webbrowser.open, args=(url,)).start()
    uvicorn.run("dashboard.server:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
