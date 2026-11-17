from autosort.authors import from_filename
from autosort.classify import classify_by_tags, pre_classify
from autosort.media import MediaInfo
from autosort.tagger import universe_of


def test_config_reads_tag_files(cfg):
    assert [c.name for c in cfg.categories] == ["Пейзажи", "Животные"]
    assert cfg.categories[0].tags == {"scenery", "blue_sky"}
    assert (2400, 1080) in cfg.screen_sizes


def test_camera_by_exif(cfg):
    d = pre_classify("photo.jpg", MediaInfo("image", 4000, 3000, camera="Google Pixel 7"), cfg)
    assert d.type == "camera"


def test_camera_name_without_exif_needs_tags(cfg):
    # Telegram тоже сохраняет картинки как IMG_2024…, так что одного имени мало
    assert pre_classify("IMG_20240512_123456_789.jpg", MediaInfo("image", 800, 600), cfg) is None


def test_screenshot_by_name_and_size(cfg):
    assert pre_classify("Screenshot_20240101.png", MediaInfo("image", 500, 500), cfg).type == "screenshot"
    assert pre_classify("x.png", MediaInfo("image", 1080, 2400), cfg).type == "screenshot"
    assert pre_classify("x.png", MediaInfo("image", 1080, 2400, camera="Samsung"), cfg).type == "camera"


def test_art_by_name(cfg):
    for name in ("98765432_p0.png", "__miku_drawn_by_someone__0123456789abcdef0123456789abcdef.jpg",
                 "FzX1abcXoAA1234.jpg", "0123456789abcdef0123456789abcdef.png"):
        assert pre_classify(name, MediaInfo("image", 800, 600), cfg).type == "art", name
    assert pre_classify("photo_123456789.jpg", MediaInfo("image", 800, 600), cfg) is None


def test_animation_is_art(cfg):
    assert pre_classify("funny.gif", MediaInfo("animation", 300, 300), cfg).type == "art"


def test_classify_by_tags(cfg):
    assert classify_by_tags({"1girl": 0.9}, "a.jpg", cfg).type == "art"
    assert classify_by_tags({"meme": 0.8, "1girl": 0.9}, "a.jpg", cfg).type == "meme"
    assert classify_by_tags({"meme": 0.4, "1girl": 0.9}, "a.jpg", cfg).type == "art"
    assert classify_by_tags({"realistic": 0.7}, "a.jpg", cfg).type == "review"
    assert classify_by_tags({"tree": 0.9}, "a.jpg", cfg).type == "review"


def test_authors_from_filename(cfg):
    assert from_filename("some_artist-1234567890123456789-01.jpg", cfg.author_patterns) == (
        "some_artist", "twitter:1234567890123456789")
    assert from_filename("98765432_p3.png", cfg.author_patterns) == (None, "pixiv:98765432")
    assert from_filename("random.png", cfg.author_patterns) == (None, None)


def test_universe_of():
    assert universe_of("hatsune_miku") is None
    assert universe_of("saber_(fate)") == "fate"


def test_category_by_character(cfg):
    from autosort.analyze import match_categories
    from autosort.config import Category

    cfg.categories.insert(0, Category("Мику", {"hatsune_miku"}))
    assert match_categories({"1girl": 0.9, "hatsune_miku": 0.95, "scenery": 0.5}, cfg) == ["Мику", "Пейзажи"]
