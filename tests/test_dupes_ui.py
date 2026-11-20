import tomllib
from pathlib import Path

from autosort.appdata import save_categories
from autosort.dupes import dupes_report, find_groups, undo_trash
from autosort.pipeline import Pipeline
from autosort.review import ReviewState

from .conftest import noise_image
from .test_pipeline import tagger


def three_copies(workspace):
    folder = workspace / "Загрузки"
    folder.mkdir()
    noise_image(40).save(folder / "a.png")
    for name in ("b.png", "c.png"):
        (folder / name).write_bytes((folder / "a.png").read_bytes())
    return folder


def test_keep_several_and_undo(cfg, db, workspace, monkeypatch):
    folder = three_copies(workspace)
    Pipeline(cfg, db, tagger(), progress=False).hash_library([folder])
    trashed = []
    monkeypatch.setattr("send2trash.send2trash", lambda p: (trashed.append(p), Path(p).unlink()))
    state = ReviewState(cfg, db, roots=[folder])
    (group,) = state.groups.values()
    ids = {Path(db.get(i)["path"]).name: i for i in group.ids}

    assert state.resolve(group.key, []) == 0  # ничего не отмечено — ничего не удаляется
    assert state.resolve(group.key, [ids["a.png"], ids["c.png"]]) == 1
    assert sorted(p.name for p in folder.iterdir()) == ["a.png", "c.png"]

    report = dupes_report(db, find_groups(db, cfg.phash_distance, [folder]), folder)
    assert "Групп: 1" in report

    def fake_restore(files):
        for path, _ in files:
            path.write_bytes((folder / "a.png").read_bytes())
        return {str(p): True for p, _ in files}

    assert undo_trash(db, db.last_trash_batch(), cfg.target, log=lambda *a: None, restore=fake_restore) == 1
    assert (folder / "b.png").exists()
    assert db.get(ids["b.png"])["status"] == "hashed"


def test_save_categories_keeps_rest_of_config(cfg, workspace):
    config = workspace / "config.toml"
    before = tomllib.loads(config.read_text(encoding="utf-8"))
    save_categories(config, "Остальное", [("Второе", "rules/2.txt"), ("Первое", "rules/1.txt")])
    after = tomllib.loads(config.read_text(encoding="utf-8"))
    assert after["categories"]["default"] == "Остальное"
    assert [c["name"] for c in after["categories"]["list"]] == ["Второе", "Первое"]
    assert {k: v for k, v in after.items() if k != "categories"} == {
        k: v for k, v in before.items() if k != "categories"}


@__import__("pytest").mark.skipif(__import__("sys").platform != "win32", reason="корзина Windows")
def test_restore_from_windows_recycle_bin(tmp_path):
    from send2trash import send2trash

    from autosort.dupes import restore_from_trash

    folder = tmp_path / "Папка с дублями"
    folder.mkdir()
    files = []
    for name in ("копия 1.png", "IMG_0001.JPG"):
        path = folder / name
        path.write_bytes(name.encode() * 100)
        send2trash(str(path))
        assert not path.exists()
        files.append((path, len(name.encode()) * 100))
    result = restore_from_trash(files)
    assert all(result.values()), result
    for path, _ in files:
        assert path.read_bytes() == path.name.encode() * 100
