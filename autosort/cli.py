"""Командная строка: autosort scan | plan | sort | dupes | dupes-only | report | undo | history."""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .config import load_config
from .db import Database
from .onedrive import CloudUnavailable


def find_config(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    for candidate in (Path("local/config.toml"), Path("config.toml")):
        if candidate.exists():
            return candidate
    from .appdata import data_dir, ensure_config

    return ensure_config(data_dir())


def print_plan_summary(plan, cfg) -> None:
    if not plan:
        print("Новых файлов нет.")
        return
    print(f"\nК переносу: {len(plan)} файлов")
    for (t, cat), n in sorted(Counter((m.type, m.category) for m in plan).items(), key=lambda x: -x[1]):
        name = cfg.folders[t] + (f" / {cat}" if cat else "")
        print(f"  {name}: {n}")


def main(argv: list[str] | None = None) -> None:
    try:
        _main(argv)
    except CloudUnavailable as e:
        print(e, file=sys.stderr)
        sys.exit(2)


def _main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="autosort", description="Автосортировка медиатеки")
    parser.add_argument("-c", "--config", help="путь к config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_text in (
        ("scan", "Сканировать: разобрать новые файлы и записать в базу, ничего не перенося"),
        ("plan", "Пробный прогон: показать, что и куда будет перенесено (plan.csv)"),
        ("sort", "Разложить: разобрать и перенести файлы"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--month", help="только эта папка-месяц, например 2024-05")
        p.add_argument("--full", action="store_true", help="перепроверить и уже разобранные файлы")
        if name == "sort":
            p.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение")

    for name, help_text in (
        ("dupes", "Показать дубли среди уже разобранных файлов"),
        ("dupes-only", "Только дубли: отпечатки всей библиотеки без сортировки, затем разбор"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--port", type=int, default=8765)
        p.add_argument("--no-browser", action="store_true", help="не открывать браузер автоматически")

    p = sub.add_parser("recategorize", help="Пересчитать категории по тегам из базы (без нейросети и скачивания)")
    p.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение перекладки")
    p = sub.add_parser("report", help="Сделать отчёт (report.txt)")
    p.add_argument("-o", "--output", help="куда сохранить отчёт")
    p = sub.add_parser("undo", help="Откатить последний перенос (или указанный)")
    p.add_argument("--batch", type=int, help="номер переноса из history")
    sub.add_parser("history", help="Список переносов и удалений")

    args = parser.parse_args(argv)
    cfg = load_config(find_config(args.config))
    db = Database(cfg.database)
    try:
        run(args, cfg, db)
    finally:
        db.close()


def run(args, cfg, db: Database) -> None:
    from .pipeline import Pipeline, write_plan_csv

    if args.cmd in ("scan", "plan", "sort"):
        if not cfg.source.exists():
            sys.exit(f"Папка-источник не найдена: {cfg.source}")
        pipe = Pipeline(cfg, db)
        plan = pipe.scan(args.month, args.full)
        print_plan_summary(plan, cfg)
        if args.cmd == "plan" and plan:
            out = cfg.base_dir / "plan.csv"
            write_plan_csv(plan, out)
            print(f"Подробный план: {out}")
        if args.cmd == "sort" and plan:
            if not args.yes and input("Перенести? [y/N] ").strip().lower() not in ("y", "д", "да", "yes"):
                print("Отменено.")
                return
            pipe.apply(plan)

    elif args.cmd in ("dupes", "dupes-only"):
        from .review import serve

        if args.cmd == "dupes-only":
            n = Pipeline(cfg, db).hash_library()
            print(f"Новых отпечатков: {n}")
        serve(cfg, db, args.port, not args.no_browser)

    elif args.cmd == "recategorize":
        pipe = Pipeline(cfg, db)
        changed, plan = pipe.recategorize()
        print(f"Категория изменилась у артов: {changed}")
        if plan:
            print_plan_summary(plan, cfg)
            if args.yes or input("Переложить уже разложенные? [y/N] ").strip().lower() in ("y", "д", "да", "yes"):
                pipe.apply(plan)

    elif args.cmd == "report":
        from .report import build_report

        out = Path(args.output) if args.output else cfg.base_dir / "report.txt"
        out.write_text(build_report(cfg, db), encoding="utf-8")
        print(f"Отчёт сохранён: {out}")

    elif args.cmd == "undo":
        Pipeline(cfg, db, progress=False).undo(args.batch)

    elif args.cmd == "history":
        rows = db.batches()
        if not rows:
            print("Журнал пуст.")
        for r in rows:
            action = "перенос" if r["action"] == "move" else "в корзину"
            undone = f", откачено {r['undone']}" if r["undone"] else ""
            print(f"№{r['batch']}  {r['at']}  {action}: {r['done']} из {r['n']}{undone}")


if __name__ == "__main__":
    main()
