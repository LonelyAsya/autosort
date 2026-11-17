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
    names = {db.get(i)["path"].rsplit("/", 1)[-1] for i in g.ids}
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
