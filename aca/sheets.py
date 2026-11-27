"""Write the tables into the Google Sheet (each tab is fully replaced every run)."""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def write_tabs(service_account_json: str, sheet_id: str, tabs: dict[str, list[list]]) -> None:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(service_account_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    book = gspread.authorize(creds).open_by_key(sheet_id)
    existing = {ws.title: ws for ws in book.worksheets()}
    for title, grid in tabs.items():
        rows, cols = max(len(grid), 1), max((len(r) for r in grid), default=1)
        ws = existing.get(title) or book.add_worksheet(title=title, rows=rows + 10, cols=cols + 2)
        ws.clear()
        ws.resize(rows=rows + 10, cols=max(cols, 1))
        for start in range(0, len(grid), 500):  # chunk to stay under request-size limits
            chunk = grid[start:start + 500]
            ws.update(values=chunk, range_name=f"A{start + 1}", value_input_option="RAW")
        ws.freeze(rows=1)
        log.info("Sheet tab %s: %s rows", title, len(grid) - 1)
    # Remove the default empty tab if the Sheet was brand new
    if "Sheet1" in existing and "Sheet1" not in tabs and len(book.worksheets()) > 1:
        book.del_worksheet(existing["Sheet1"])
