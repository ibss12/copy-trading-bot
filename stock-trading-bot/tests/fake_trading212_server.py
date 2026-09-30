"""Run the fake Trading 212 practice API from the tests on a local port, for trying the dashboard.

python -m tests.fake_trading212_server 8212
then: BROKER=trading212 TRADING212_API_KEY=demo-key TRADING212_API_SECRET=demo-secret \
      TRADING212_BASE_URL=http://127.0.0.1:8212/api/v0 python run_dashboard.py
Market orders fill instantly at $100.
"""

import sys
from http.server import ThreadingHTTPServer

from tests.test_trading212 import FakeTrading212

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8212
    fake = FakeTrading212()
    fake.auto_fill = True
    fake.positions["AAPL_US_EQ"] = 5.0
    ThreadingHTTPServer(("127.0.0.1", port), fake.handler()).serve_forever()
