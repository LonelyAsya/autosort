from pathlib import Path

import pytest

from PIL import Image

from autosort.dupes import find_groups, trash_files
from autosort.pipeline import Pipeline
from autosort.report import build_report
from autosort.tagger import TagResult

from .conftest import FakeTagger, noise_image


def make_library(cfg):
    month = cfg.source / "2024-05"
    month.mkdir()
    exif = Image.Exif()
    exif[0x010F] = "Google"
    exif[0x0110] = "Pixel 7"
    noise_image(1).save(month / "IMG_20240501_100000.jpg", exif=exif)
    noise_image(2).save(month / "98765432_p0.png")
    noise_image(3, (1080, 2400)).save(month / "Screenshot_20240502.png")
    scenery = noise_image(4)
    scenery.putpixel((0, 0), (7, 7, 7))
    scenery.save(month / "landscape.png")
    # точная копия и уменьшенная копия арта
    (month / "copy.png").write_bytes((month / "98765432_p0.png").read_bytes())
    noise_image(2).resize((128, 128)).save(month / "small.jpg", quality=90)
    return month


def tagger():
    scenery = TagResult(general={"1girl": 0.8, "scenery": 0.7, "cat": 0.6},
                        characters={"saber_(fate)": 0.95}, rating={"general": 0.9})
    return FakeTagger({7: scenery})


def test_plan_does_not_touch_files(cfg, db):
    month = make_library(cfg)
    before = sorted(p.name for p in month.iterdir())
    plan = Pipeline(cfg, db, tagger(), progress=False).scan()
    assert sorted(p.name for p in month.iterdir()) == before
    by_name = {m.src.name: m for m in plan}
    assert by_name["IMG_20240501_100000.jpg"].type == "camera"
    assert by_name["Screenshot_20240502.png"].type == "screenshot"
    assert by_name["98765432_p0.png"].type == "art"
    land = by_name["landscape.png"]
    # подходит под обе категории — уходит в первую по приоритету
    assert (land.type, land.category) == ("art", "Пейзажи")
    assert land.dst == cfg.target / "Арты" / "Пейзажи" / "2024-05" / "landscape.png"
    assert by_name["IMG_20240501_100000.jpg"].dst == cfg.target / "Камера" / "2024-05" / "IMG_20240501_100000.jpg"

    row = db.get_by_path(month / "landscape.png")
    tags = {(t["kind"], t["tag"]) for t in db.tags_of(row["id"])}
    assert {("category", "Животные"), ("character", "saber_(fate)"), ("universe", "fate")} <= tags


def test_sort_and_undo(cfg, db):
    month = make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    plan = pipe.scan()
    pipe.apply(plan)
    assert not any(month.iterdir())
    assert (cfg.target / "Скриншоты" / "2024-05" / "Screenshot_20240502.png").exists()

    # повторный запуск ничего нового не находит
    assert pipe.scan() == []

    assert pipe.undo() == len(plan)
    assert len(list(month.iterdir())) == len(plan)
    assert not (cfg.target / "Камера").exists()


def test_incremental_skips_known_files(cfg, db):
    make_library(cfg)
    calls = []

    class CountingTagger(FakeTagger):
        def tag(self, frames):
            calls.append(1)
            return super().tag(frames)

    pipe = Pipeline(cfg, db, CountingTagger(), progress=False)
    pipe.scan()
    first = len(calls)
    pipe.scan()
    assert len(calls) == first
    pipe.scan(full=True)
    assert len(calls) == 2 * first


def test_duplicates(cfg, db):
    make_library(cfg)
    Pipeline(cfg, db, tagger(), progress=False).scan()
    groups = find_groups(db, cfg.phash_distance)
    assert len(groups) == 1
    g = groups[0]
    names = {Path(db.get(i)["path"]).name for i in g.ids}
    assert names == {"98765432_p0.png", "copy.png", "small.jpg"}
    assert db.get(g.keep)["width"] == 256  # большее разрешение

    report = build_report(cfg, db)
    assert "Групп: 1, лишних файлов: 2" in report
    assert "saber_(fate)" in report


def test_trash_and_ignore(cfg, db, monkeypatch):
    make_library(cfg)
    Pipeline(cfg, db, tagger(), progress=False).scan()
    g = find_groups(db, cfg.phash_distance)[0]

    trashed = []
    monkeypatch.setattr("send2trash.send2trash", lambda p: trashed.append(p))
    others = [i for i in g.ids if i != g.keep]
    assert trash_files(db, others) == 2
    assert len(trashed) == 2
    assert find_groups(db, cfg.phash_distance) == []


def test_ignore_group(cfg, db):
    make_library(cfg)
    Pipeline(cfg, db, tagger(), progress=False).scan()
    g = find_groups(db, cfg.phash_distance)[0]
    db.ignore_group(g.key)
    assert find_groups(db, cfg.phash_distance) == []


def test_hash_library_without_sorting(cfg, db):
    month = make_library(cfg)
    n = Pipeline(cfg, db, tagger(), progress=False).hash_library()
    assert n == len(list(month.iterdir()))
    assert db.get_by_path(month / "copy.png")["status"] == "hashed"
    assert len(find_groups(db, cfg.phash_distance)) == 1


def test_no_tagger_sends_unknown_to_review(cfg, db):
    month = make_library(cfg)
    plan = Pipeline(cfg, db, lambda: None, progress=False).scan()
    by_name = {m.src.name: m for m in plan}
    assert by_name["landscape.png"].type == "review"
    assert by_name["98765432_p0.png"].category == "Общее"
    assert by_name["landscape.png"].dst.parent == cfg.target / "На ручной разбор" / "2024-05"
    assert month.exists()


def test_nested_year_month_folders(workspace, db):
    """OneDrive/Изображения/Альбом камеры/2024/05/… → OneDrive/Изображения/Отсортированные/…/2024/05/…"""
    from autosort.appdata import set_path_setting, set_setting
    from autosort.config import load_config
    from autosort.pipeline import folder_choices, media_folders

    pictures = workspace / "OneDrive" / "Изображения"
    album = pictures / "Альбом камеры"
    for year, month, seed in (("2023", "12", 10), ("2024", "01", 11), ("2024", "05", 12)):
        (album / year / month).mkdir(parents=True)
        noise_image(seed).save(album / year / month / f"{seed}_p0_12345678_p0.png")
    config = workspace / "config.toml"
    set_path_setting(config, "source", str(album))
    set_path_setting(config, "target", str(pictures / "Отсортированные с телефона"))
    cfg = load_config(config)

    assert folder_choices(media_folders(cfg)) == ["2023", "2023/12", "2024", "2024/01", "2024/05"]

    pipe = Pipeline(cfg, db, tagger(), progress=False)
    assert len(pipe.scan("2024")) == 2  # весь год
    plan = pipe.scan()
    pipe.apply(plan)
    sorted_root = pictures / "Отсортированные с телефона" / "Арты" / "Общее"
    assert sorted(p.relative_to(sorted_root).as_posix() for p in sorted_root.rglob("*.png")) == [
        "2023/12/10_p0_12345678_p0.png", "2024/01/11_p0_12345678_p0.png", "2024/05/12_p0_12345678_p0.png"]

    # без папок год/месяц — всё прямо в категории
    assert pipe.undo() == 3
    set_setting(config, "layout", "by_month", "false")
    cfg = load_config(config)
    assert cfg.by_month is False
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.apply(pipe.scan())
    assert sorted(p.name for p in sorted_root.iterdir()) == [
        "10_p0_12345678_p0.png", "11_p0_12345678_p0.png", "12_p0_12345678_p0.png"]


def test_category_by_universe(cfg, db):
    from autosort.config import Category

    cfg.categories.insert(0, Category("Fate", {"fate"}))
    make_library(cfg)
    plan = Pipeline(cfg, db, tagger(), progress=False).scan()
    land = next(m for m in plan if m.src.name == "landscape.png")  # на нём saber_(fate)
    assert land.category == "Fate"


def test_recategorize_without_tagger(cfg, db):
    from autosort.config import Category

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.apply(pipe.scan())
    old = cfg.target / "Арты" / "Пейзажи" / "2024-05" / "landscape.png"
    assert old.exists()

    # новая категория выше по приоритету; нейросеть больше не нужна
    cfg.categories.insert(0, Category("Fate", {"fate"}))
    pipe = Pipeline(cfg, db, lambda: pytest.fail("тегер не должен вызываться"), progress=False)
    changed, plan = pipe.recategorize()
    assert changed == 1
    assert [(m.src, m.dst) for m in plan] == [(old, cfg.target / "Арты" / "Fate" / "2024-05" / "landscape.png")]
    pipe.apply(plan)
    assert not old.parent.exists()  # опустевшая папка убрана
    row = db.get(plan[0].file_id)
    assert (row["category"], row["status"]) == ("Fate", "moved")
    assert {t["tag"] for t in db.tags_of(row["id"]) if t["kind"] == "category"} == {"Пейзажи", "Животные"}

    # откат возвращает в старую папку, файл остаётся «разложенным»
    assert pipe.undo() == 1
    assert old.exists()
    assert db.get(row["id"])["status"] == "moved"


def test_flaky_onedrive_file_is_retried(cfg, db):
    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.retry_delays = (0, 0)
    real = pipe.analyzer.analyze
    failures = {"copy.png": 2, "small.jpg": 5}

    def flaky(path):
        if failures.get(path.name, 0) > 0:
            failures[path.name] -= 1
            raise OSError(22, "Invalid argument")
        return real(path)

    pipe.analyzer.analyze = flaky
    plan = pipe.scan()
    names = {m.src.name for m in plan}
    assert "copy.png" in names  # третья попытка удалась
    assert "small.jpg" not in names  # три неудачи — ошибка, файл не тронут
    assert pipe.errors == 1
    assert db.get_by_path(cfg.source / "2024-05" / "small.jpg")["status"] == "error"

    # при следующем запуске файл с ошибкой пробуется снова
    failures.clear()
    pipe.errors = 0
    assert "small.jpg" in {m.src.name for m in pipe.scan()}


def test_onedrive_down_stops_everything(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudUnavailable

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    calls = []

    def broken(path):
        calls.append(path.name)
        raise OSError(22, "Invalid argument")

    def diagnose(path, error):
        raise CloudUnavailable("OneDrive не отдаёт файлы")

    pipe.analyzer.analyze = broken
    pipe.max_revivals = 0  # перезапуск OneDrive не помог
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    with pytest.raises(CloudUnavailable):
        pipe.scan()
    assert len(calls) == 1  # без повторов и без перебора остальных файлов
    assert db.conn.execute("SELECT COUNT(*) FROM files WHERE status = 'error'").fetchone()[0] == 0


def test_closed_onedrive_is_restarted(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudUnavailable

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe._sleep = lambda seconds: None
    real = pipe.analyzer.analyze
    state = {"running": False, "started": 0, "fail": True}

    def flaky(path):
        if state["fail"] and path.name == "small.jpg":
            raise OSError(22, "Invalid argument")
        return real(path)

    def diagnose(path, error):
        raise CloudUnavailable("OneDrive не отдаёт файлы")

    def start():
        state.update(running=True, fail=False, started=state["started"] + 1)
        return True

    pipe.analyzer.analyze = flaky
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    monkeypatch.setattr(pipeline_mod, "onedrive_running", lambda: state["running"])
    monkeypatch.setattr(pipeline_mod, "start_onedrive", start)
    plan = pipe.scan()
    assert state["started"] == 1
    assert "small.jpg" in {m.src.name for m in plan}
    assert pipe.errors == 0


def test_file_that_crashes_onedrive_is_skipped(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudUnavailable

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe._sleep = lambda seconds: None
    real = pipe.analyzer.analyze
    starts = []

    def crashes_onedrive(path):
        if path.name == "small.jpg":
            raise OSError(22, "Invalid argument")
        return real(path)

    def diagnose(path, error):
        raise CloudUnavailable("OneDrive упал")

    pipe.analyzer.analyze = crashes_onedrive
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    monkeypatch.setattr(pipeline_mod, "onedrive_running", lambda: False)
    monkeypatch.setattr(pipeline_mod, "start_onedrive", lambda: starts.append(1) or True)
    pipe.revive_timeout = 0
    plan = pipe.scan()
    assert len(starts) == 2  # перезапуск, повтор, снова упал — перезапуск и пропуск
    names = {m.src.name for m in plan}
    assert "small.jpg" not in names and "copy.png" in names  # остальные файлы разобраны
    assert db.get_by_path(cfg.source / "2024-05" / "small.jpg")["status"] == "error"


def test_sort_moves_each_folder_right_away(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudUnavailable

    first = make_library(cfg)
    second = cfg.source / "2024-06"
    second.mkdir()
    noise_image(20).save(second / "11111111_p0.png")
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.max_revivals = 0
    real = pipe.analyzer.analyze

    def broken_in_june(path):
        if path.parent == second:
            raise OSError(22, "Invalid argument")
        return real(path)

    def diagnose(path, error):
        raise CloudUnavailable("OneDrive не отдаёт файлы")

    pipe.analyzer.analyze = broken_in_june
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    with pytest.raises(CloudUnavailable):
        pipe.scan(move=True)
    assert not any(first.iterdir())  # май уже разложен, хотя июнь сорвался
    assert (second / "11111111_p0.png").exists()
    assert pipe.undo() == 6  # откат возвращает всё перенесённое за запуск
    assert len(list(first.iterdir())) == 6


def test_truncated_jpeg_is_readable(cfg, db):
    month = make_library(cfg)
    data = (month / "small.jpg").read_bytes()
    (month / "small.jpg").write_bytes(data[:-200])
    plan = Pipeline(cfg, db, tagger(), progress=False).scan()
    assert "small.jpg" in {m.src.name for m in plan}


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="только Windows")
def test_windows_read_error(tmp_path):
    from autosort.onedrive import explain_read_error, windows_read_error

    ok = tmp_path / "ок.txt"
    ok.write_text("x")
    assert windows_read_error(ok) is None
    code, text = windows_read_error(tmp_path / "нет.txt")
    assert code == 2 and text
    assert "код Windows 2" in explain_read_error(tmp_path / "нет.txt", OSError(22, "Invalid argument"))


def test_dupes_in_any_folder(cfg, db, workspace):
    from autosort.dupes import find_groups
    from autosort.speed import FILES, MOVE

    make_library(cfg)
    other = workspace / "Загрузки"
    other.mkdir()
    noise_image(30).save(other / "a.png")
    (other / "b.png").write_bytes((other / "a.png").read_bytes())
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    assert pipe.hash_library([other]) == 2
    groups = find_groups(db, cfg.phash_distance, [other])
    assert len(groups) == 1 and groups[0].exact
    # обычный поиск по библиотеке чужую папку не захватывает
    assert all(not str(db.get(i)["path"]).startswith(str(other))
               for g in find_groups(db, cfg.phash_distance, [cfg.source, cfg.target]) for i in g.ids)

    pipe.scan(move=True)
    rates = pipe.speed.rates()
    assert rates[FILES] > 0 and rates[MOVE] > 0


def test_unreadable_format_goes_to_review(cfg, db):
    month = make_library(cfg)
    # BMP-заголовок со сжатием DXT3, которое Pillow не понимает (так бывают сохранены игровые текстуры)
    import struct
    header = b"BM" + struct.pack("<IHHI", 70, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, 4, 4, 1, 32, 0x33545844, 16, 0, 0, 0, 0)
    (month / "cruiser_texture.bmp").write_bytes(header + info + b"\0" * 16)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.retry_delays = (60, 60)  # повторов быть не должно, иначе тест «зависнет»
    plan = {m.src.name: m for m in pipe.scan()}
    assert plan["cruiser_texture.bmp"].type == "review"
    assert pipe.errors == 0
    assert "не удалось прочитать" in db.get(plan["cruiser_texture.bmp"].file_id)["reason"]


def add_custom_folders(workspace, body: str):
    from autosort.config import load_config

    config = workspace / "config.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n" + body, encoding="utf-8")
    return load_config(config)


def test_custom_folders_by_name_and_tags(workspace, db):
    (workspace / "rules").mkdir(exist_ok=True)
    (workspace / "rules" / "cats.txt").write_text("cat\n", encoding="utf-8")
    cfg = add_custom_folders(workspace, """
[[custom_folders]]
name = "Телеграм"
patterns = ["*screenshot*"]

[[custom_folders]]
name = "Котики"
tags_file = "rules/cats.txt"
""")
    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    plan = {m.src.name: m for m in pipe.scan()}
    assert (plan["Screenshot_20240502.png"].type, plan["Screenshot_20240502.png"].category) == ("custom", "Телеграм")
    assert plan["landscape.png"].category == "Котики"  # у пейзажа есть тег cat
    assert plan["IMG_20240501_100000.jpg"].type == "camera"
    dst = plan["landscape.png"].dst
    assert dst.relative_to(cfg.target).parts[0] == "Котики"
    assert db.get(plan["landscape.png"].file_id)["base_type"] == "art"

    pipe.apply(list(plan.values()))
    # правило убрали — пересчёт возвращает файлы в их обычные папки без нейросети
    from autosort.config import load_config

    config = workspace / "config.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text[:text.index("[[custom_folders]]")], encoding="utf-8")
    pipe = Pipeline(load_config(config), db, None, progress=False)
    changed, moves = pipe.recategorize()
    assert changed == 2
    targets = {m.src.name: m for m in moves}
    assert targets["landscape.png"].type == "art"
    assert targets["Screenshot_20240502.png"].type == "screenshot"
    assert targets["Screenshot_20240502.png"].dst.relative_to(pipe.cfg.target).parts[0] == "Скриншоты"


def test_similarity_percent(cfg, db):
    make_library(cfg)
    Pipeline(cfg, db, tagger(), progress=False).scan()
    groups = find_groups(db, cfg.phash_distance)
    art = next(g for g in groups if len(g.ids) == 3)
    names = {Path(db.get(i)["path"]).name: art.similarity[i] for i in art.ids}
    assert names["98765432_p0.png"] == 100 and names["copy.png"] == 100
    assert names["small.jpg"] >= 90 and not art.exact  # уменьшенная копия: по виду та же

    from autosort.dupes import similarity_percent
    a = {"sha256": "1", "phash": "ffffffffffffffff"}
    assert similarity_percent(a, {"sha256": "2", "phash": "fffffffffffffff0"}) == 94  # 4 бита из 64
    assert similarity_percent(a, {"sha256": "2", "phash": "0000000000000000"}) == 0
    assert similarity_percent(a, {"sha256": "1", "phash": None}) == 100
    assert similarity_percent(a, {"sha256": "2", "phash": "ff,ff"}) is None  # картинка и видео


def test_file_refused_by_onedrive_is_skipped_without_retries(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudFileRefused

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    sleeps = []
    pipe._sleep = sleeps.append
    real = pipe.analyzer.analyze
    calls = []

    def refused(path):
        if path.name == "small.jpg":
            calls.append(path.name)
            raise OSError(22, "Invalid argument")
        return real(path)

    def diagnose(path, error):
        raise CloudFileRefused("OneDrive не отдал файл (код Windows 380)")

    pipe.analyzer.analyze = refused
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    plan = pipe.scan()
    assert calls == ["small.jpg"] and sleeps == []  # ни повторов, ни ожиданий
    assert "copy.png" in {m.src.name for m in plan}
    row = db.get_by_path(cfg.source / "2024-05" / "small.jpg")
    assert row["status"] == "error" and "380" in row["error"]


def test_many_refused_files_wake_onedrive(cfg, db, monkeypatch):
    import autosort.pipeline as pipeline_mod
    from autosort.onedrive import CloudFileRefused, CloudUnavailable

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe._sleep = lambda seconds: None
    pipe.refused_limit = 2
    pipe.max_revivals = 0  # разбудить не получилось — останавливаемся

    def refused(path):
        raise OSError(22, "Invalid argument")

    def diagnose(path, error):
        raise CloudFileRefused("OneDrive не отдал файл (код Windows 380)")

    pipe.analyzer.analyze = refused
    monkeypatch.setattr(pipeline_mod, "explain_read_error", diagnose)
    with pytest.raises(CloudUnavailable):
        pipe.scan()
    assert pipe.errors == 1  # первый файл пропущен, на втором подряд — остановка


def test_move_avoids_path_of_trashed_duplicate(cfg, db):
    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    plan = pipe.scan()
    first = plan[0]
    # дубль с таким же путём раньше удалили в корзину: файла нет, а запись в базе осталась
    db.save_file(first.dst, size=1, mtime=0, status="trashed")
    pipe.apply(plan)
    row = db.get_by_path(first.dst.with_name(f"{first.dst.stem} (1){first.dst.suffix}"))
    assert row is not None and row["id"] == first.file_id and row["status"] == "moved"
    assert db.get_by_path(first.dst)["status"] == "trashed"


def test_interrupted_move_is_written_to_db(cfg, db):
    import shutil

    make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    m = pipe.scan()[0]
    batch = db.new_batch()
    db.journal(batch, m.file_id, "move", m.src, m.dst)
    m.dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(m.src), str(m.dst))  # файл перенесён, а запись в базу сорвалась
    assert pipe.finish_interrupted_moves() == 1
    assert db.get_by_path(m.dst)["id"] == m.file_id
    assert pipe.undo(batch) == 1 and m.src.exists()


def test_changed_mtime_does_not_reanalyze(cfg, db):
    import os

    month = make_library(cfg)
    pipe = Pipeline(cfg, db, tagger(), progress=False)
    pipe.scan()
    for f in month.iterdir():  # OneDrive переустановили — даты файлов чуть сдвинулись
        st = f.stat()
        os.utime(f, (st.st_atime, int(st.st_mtime) + 0.5))
    calls = []
    real = pipe.analyzer.analyze
    pipe.analyzer.analyze = lambda path: calls.append(path) or real(path)
    pipe.scan()
    assert calls == []
