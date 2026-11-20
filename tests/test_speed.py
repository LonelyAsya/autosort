from autosort.speed import DOWNLOAD, FILES, MOVE, Speedometer


def test_rates_over_window():
    now = [100.0]
    s = Speedometer(window=10, clock=lambda: now[0])
    for _ in range(20):
        s.add(FILES)
    s.add(DOWNLOAD, 30 * 1024**2)
    now[0] += 10
    r = s.rates()
    assert r[FILES] == 2.0 and r[DOWNLOAD] == 3 * 1024**2
    assert MOVE not in r
    assert s.text() == "2.0 файл/с · скачивание 3.0 МБ/с"
    now[0] += 11  # всё старше окна забывается
    assert s.text() == ""
