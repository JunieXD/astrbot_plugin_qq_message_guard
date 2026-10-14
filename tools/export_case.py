"""Export one local audit record as JSON without making any network or QQ calls."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def export(database, cid):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise ValueError("审核记录不存在或已过期。")
        case = dict(row)
        case["data"] = json.loads(case["data"])
        case["operations"] = [
            dict(row) for row in db.execute("SELECT * FROM operations WHERE case_id=? ORDER BY id", (cid,))
        ]
        case["calls"] = [
            dict(row) for row in db.execute("SELECT * FROM calls WHERE case_id=? ORDER BY id", (cid,))
        ]
        ids = case["data"].get("batch", {}).get("screening_ids", [])
        case["screenings"] = []
        for sid in ids:
            row = db.execute("SELECT * FROM screenings WHERE id=?", (sid,)).fetchone()
            if row:
                shared = dict(row)
                shared["data"] = json.loads(shared["data"])
                shared["case_ids"] = json.loads(shared["case_ids"])
                case["screenings"].append(shared)
        if ids:
            calls = {row["id"]: row for row in case["calls"]}
            for row in db.execute(
                "SELECT * FROM calls WHERE screening_id IN (SELECT value FROM json_each(?))",
                (json.dumps(ids),),
            ):
                calls[row["id"]] = dict(row)
            case["calls"] = sorted(calls.values(), key=lambda row: row["id"])
        return case


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = json.dumps(export(args.db, args.id), ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
