"""Entry point: `python main.py` launches the dashboard web app.

The per-minute data collector runs separately via cron (see
docs/adr/0001-decoupled-cron-collector.md), not from here.
"""

import uvicorn


def main():
    uvicorn.run("app.main:app", host="127.0.0.1", port=8005, reload=False)


if __name__ == "__main__":
    main()
