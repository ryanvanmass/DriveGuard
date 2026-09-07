import re
import time

from driveguard import Heartbeat


def test_percentage_never_decreases_across_ticks(capsys):
    # Real thread, real sleeps -- exercising the actual _run() loop rather
    # than re-deriving its formula by hand. Note: `interval` is only a
    # fallback used when there's no estimate (see the class docstring) --
    # with an estimate given, the interval is always
    # estimated_seconds/target_pings, clamped to [1, 60]s. estimated_seconds
    # <= 15 here clamps that to the 1s floor, which sets the real minimum
    # wall-clock time this test needs.
    with Heartbeat("working", estimated_seconds=10):
        time.sleep(2.3)

    out = capsys.readouterr().out
    percentages = [int(m) for m in re.findall(r"~(\d+)% est\.", out)]

    assert len(percentages) >= 2, "expected multiple heartbeat ticks over 2.3s at the 1s floor"
    assert percentages == sorted(percentages)


def test_percentage_is_capped_at_99_even_past_the_estimate(capsys):
    with Heartbeat("working", estimated_seconds=1):
        time.sleep(2.2)

    out = capsys.readouterr().out
    percentages = [int(m) for m in re.findall(r"~(\d+)% est\.", out)]

    assert percentages, "expected at least one heartbeat tick"
    assert max(percentages) <= 99


def test_without_an_estimate_no_percentage_is_shown(capsys):
    with Heartbeat("working", interval=0.05):
        time.sleep(0.12)

    out = capsys.readouterr().out
    assert "elapsed" in out
    assert "% est." not in out


def test_interval_adapts_to_land_around_target_pings():
    hb = Heartbeat("working", estimated_seconds=150, target_pings=15)
    assert hb.interval == 10  # 150 / 15


def test_interval_is_clamped_to_a_minimum_of_one_second():
    hb = Heartbeat("working", estimated_seconds=1, target_pings=15)
    assert hb.interval == 1


def test_interval_is_clamped_to_a_maximum_of_sixty_seconds():
    hb = Heartbeat("working", estimated_seconds=100000, target_pings=15)
    assert hb.interval == 60
