from autosort.appdata import ensure_config, set_path_setting
from autosort.config import load_config


def test_first_run_creates_working_config(tmp_path):
    config = ensure_config(tmp_path / "Autosort")
    assert (tmp_path / "Autosort" / "rules" / "example_scenery.txt").exists()
    set_path_setting(config, "source", "C:\\Users\\me\\OneDrive\\Camera Roll")
    set_path_setting(config, "target", str(tmp_path / 'out "x"'))
    cfg = load_config(config)
    assert cfg.source.as_posix().endswith("C:/Users/me/OneDrive/Camera Roll")
    assert cfg.target == tmp_path / 'out "x"'
    assert "# Настройки autosort" in config.read_text(encoding="utf-8")  # комментарии на месте
    assert ensure_config(tmp_path / "Autosort") == config  # второй запуск ничего не перезаписывает
