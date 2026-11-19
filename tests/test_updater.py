import hashlib

import pytest

from autosort import updater


def test_versions():
    assert updater.parse_version("v1.4.0") == (1, 4, 0)
    assert updater.parse_version("dev") is None
    assert updater.is_newer("1.4.1", "1.4.0")
    assert updater.is_newer("2.0.0", "1.10.3")
    assert not updater.is_newer("1.4.0", "1.4.0")
    assert not updater.is_newer("1.3.9", "1.4.0")
    assert not updater.is_newer("garbage", "1.4.0")


def release_json(**asset):
    return {
        "tag_name": "v9.1.0",
        "html_url": "https://example.invalid/r",
        "body": "### Новое\n- **Кнопка** `обновить`\n\nВсе изменения: [CHANGELOG.md](x)",
        "assets": [
            {"name": "Autosort-portable-9.1.0.zip", "browser_download_url": "zip"},
            {"name": "Autosort-Setup-9.1.0.exe", "browser_download_url": "setup", "size": 3, **asset},
        ],
    }


def test_parse_release_and_notes():
    r = updater.parse_release(release_json(digest="sha256:ABC"))
    assert (r.version, r.setup_url, r.setup_name, r.setup_size, r.setup_sha256) == (
        "9.1.0", "setup", "Autosort-Setup-9.1.0.exe", 3, "abc")
    assert updater.short_notes(r.notes) == "Новое\n- Кнопка обновить"
    assert updater.parse_release(release_json()).setup_sha256 is None


def make_release(tmp_path, data: bytes, digest: str | None):
    src = tmp_path / "src.exe"
    src.write_bytes(data)
    r = updater.parse_release(release_json(size=len(data), **({"digest": digest} if digest else {})))
    r.setup_url = src.as_uri()
    r.setup_name = "Autosort-Setup-test.exe"
    return r


def test_download_checks_sha256(tmp_path):
    data = b"installer bytes"
    good = make_release(tmp_path, data, "sha256:" + hashlib.sha256(data).hexdigest())
    path = updater.download(good)
    assert path.read_bytes() == data
    path.unlink()

    bad = make_release(tmp_path, data, "sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="Контрольная сумма"):
        updater.download(bad)


def test_not_installed_when_running_from_source():
    assert updater.installed_dir() is None
