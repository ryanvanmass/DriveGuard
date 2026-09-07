from driveguard import human_size


def test_none_is_a_dash():
    assert human_size(None) == "-"


def test_zero_bytes():
    assert human_size(0) == "0.0 B"


def test_bytes_under_1024_stay_bytes():
    assert human_size(512) == "512.0 B"


def test_exactly_1024_rolls_over_to_kb():
    assert human_size(1024) == "1.0 KB"


def test_megabytes():
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


def test_gigabytes():
    assert human_size(2 * 1024 ** 3) == "2.0 GB"


def test_terabytes():
    assert human_size(3 * 1024 ** 4) == "3.0 TB"


def test_beyond_terabytes_falls_back_to_petabytes():
    assert human_size(1024 ** 5) == "1.0 PB"
