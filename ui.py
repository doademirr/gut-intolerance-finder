"""Development tool, not part of the app: open data/gut.duckdb in the DuckDB UI (http://localhost:4213).

Run from the project root:  .venv/bin/python ui.py
Queries run on this computer, but the UI's web page is downloaded from DuckDB's servers when it starts,
and the database file is saved in data/ (gitignored). Press Ctrl+C to stop.
"""
import time

import duckdb


def main():
    con = duckdb.connect("data/gut.duckdb")
    con.sql("CALL start_ui();")
    print("DuckDB UI running at http://localhost:4213 (Ctrl+C to stop)")
    try:
        while True:  # the UI only works while this connection stays open
            time.sleep(1)
    except KeyboardInterrupt:
        con.sql("CALL stop_ui_server();")
        con.close()


if __name__ == "__main__":
    main()
