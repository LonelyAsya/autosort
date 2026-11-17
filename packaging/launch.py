# Точка входа для PyInstaller. Если программа падает ещё до окна,
# показываем ошибку и пишем её в crash.log — иначе exe без консоли закрылся бы молча.
import sys
import traceback

try:
    from autosort.gui import main

    main()
except Exception:
    text = traceback.format_exc()
    try:
        from autosort.appdata import data_dir

        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "crash.log").write_text(text, encoding="utf-8")
    except Exception:
        pass
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0, "Autosort не смог запуститься.\n\n" + text[-2000:], "Autosort", 0x10)
    raise SystemExit(1)
