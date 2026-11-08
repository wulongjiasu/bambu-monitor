#!/usr/bin/env python3
"""把填好访问码的 CSV 转成 config.json。

CSV 列: name,model,ip,serial,access_code[,signal_dbm]
访问码为空的机器会被跳过（不写入配置）。

用法:
    python3 tools/build_config.py docs/lan31-inventory.csv
    python3 tools/build_config.py docs/lan31-inventory.csv --out /tmp/config.json
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", help="填好访问码的 CSV")
    parser.add_argument("--out", default=str(BASE_DIR / "config.json"), help="输出路径")
    parser.add_argument("--template", default=str(BASE_DIR / "config.json"), help="沿用其 title / server 段")
    args = parser.parse_args()

    template = {}
    tpl_path = Path(args.template)
    if tpl_path.exists():
        template = json.loads(tpl_path.read_text(encoding="utf-8"))

    printers: list[dict] = []
    skipped: list[str] = []
    with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("access_code") or "").strip()
            name = (row.get("name") or "").strip()
            if not code:
                skipped.append(name)
                continue
            printers.append(
                {
                    "name": name,
                    "model": (row.get("model") or "").strip(),
                    "serial": (row.get("serial") or "").strip(),
                    "ip": (row.get("ip") or "").strip(),
                    "access_code": code,
                }
            )

    config = {
        "title": template.get("title", "打印工坊"),
        "server": template.get(
            "server", {"host": "0.0.0.0", "port": 8088, "allow_control": False}
        ),
        "printers": printers,
    }
    Path(args.out).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已写入 {args.out}: {len(printers)} 台")
    if skipped:
        print(f"跳过 {len(skipped)} 台（访问码为空）: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
