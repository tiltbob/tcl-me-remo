import pytest

from tcl_me_remo import macro
from tcl_me_remo.macro import Step


def test_parse_all_token_forms():
    steps = macro.parse("home, 1.5s DOWN*3  ok~2 300ms c:223*2 home~0.5*2")
    assert steps == [
        Step(key="HOME"),
        Step(wait=1.5),
        Step(key="DOWN", repeat=3),
        Step(key="OK", hold=2.0),
        Step(wait=0.3),
        Step(key="C:0223", repeat=2),
        Step(key="HOME", hold=0.5, repeat=2),
    ]
    assert macro.format_steps(steps) == "HOME 1.5s DOWN*3 OK~2 300ms C:0223*2 HOME~0.5*2"
    assert macro.parse(macro.format_steps(steps)) == steps


def test_empty_and_zero_repeat():
    assert macro.parse("") == []
    assert macro.parse("  ,, ") == []
    assert macro.parse("UP*0 OK 0s") == [Step(key="OK")]


@pytest.mark.parametrize(
    "text",
    ["BOGUS", "DOWN*", "DOWN*x", "*3", "DOWN*31", "OK~0", "OK~11", "OK~abc", "31s", "5", "2.5", "DOWN*-1"],
)
def test_parse_errors(text):
    with pytest.raises(macro.MacroError):
        macro.parse(text)


def test_bare_number_error_explains_units():
    with pytest.raises(macro.MacroError, match="waits need a unit"):
        macro.parse("HOME 2 OK")


def test_step_limit():
    macro.parse(" ".join(["OK"] * macro.MAX_STEPS))
    with pytest.raises(macro.MacroError, match="limit"):
        macro.parse(" ".join(["OK"] * (macro.MAX_STEPS + 1)))


def test_render_placeholders():
    assert macro.render("DOWN*{port} UP*{port-1} RIGHT*{ port + 2 }", 3) == "DOWN*3 UP*2 RIGHT*5"
    assert macro.render("DOWN*{port-1}", 1) == "DOWN*0"
    assert macro.render("DOWN*{port-9}", 2) == "DOWN*0"  # clamped, never negative
    assert macro.parse(macro.render("HOME DOWN*{port-1} OK", 1)) == [Step(key="HOME"), Step(key="OK")]


def test_validate_checks_every_port():
    assert macro.validate("  HOME   DOWN*{port}  OK ") == "HOME DOWN*{port} OK"
    with pytest.raises(macro.MacroError):
        macro.validate("DOWN*{port+28}")  # fine on port 1 and 2, too many repeats on port 3


def test_estimate_and_runtime_limit():
    steps = macro.parse("HOME 2s DOWN*3 OK~1")
    assert macro.estimate_seconds(steps, key_press=0.1, key_gap=0.4) == pytest.approx(0.5 + 2 + 1.5 + 1.4)
    with pytest.raises(macro.MacroError, match="would take"):
        macro.check_runnable(macro.parse("30s 30s 30s 30s 1s"), key_press=0.1, key_gap=0.1)


async def test_run_plays_steps_in_order_with_gaps():
    events = []

    async def send_key(spec, hold):
        events.append(("key", spec, hold))

    async def sleep(seconds):
        events.append(("sleep", seconds))

    await macro.run(macro.parse("HOME 1s DOWN*2 OK~1.5"), send_key, key_press=0.08, key_gap=0.3, sleep=sleep)
    assert events == [
        ("key", "HOME", 0.08),
        ("sleep", 0.3),
        ("sleep", 1.0),
        ("key", "DOWN", 0.08),
        ("sleep", 0.3),
        ("key", "DOWN", 0.08),
        ("sleep", 0.3),
        ("key", "OK", 1.5),
        ("sleep", 0.3),
    ]


def test_builtin_presets():
    assert list(macro.PRESETS) == ["tcl-row", "tcl-row-home", "tcl-row-first", "tcl-column"]
    assert macro.render(macro.PRESETS["tcl-row"].template, 2) == "F3 1.5s LEFT*8 RIGHT*2 OK"
    # HDMI 1 listed first: no step to the right at all for port 1
    steps = macro.parse(macro.render(macro.PRESETS["tcl-row-first"].template, 1))
    assert [s.key for s in steps if s.key] == ["F3", "LEFT", "OK"]
    for preset in macro.PRESETS.values():
        assert preset.title and preset.description


def test_presets_are_valid_for_all_ports():
    for preset in macro.PRESETS.values():
        for port in (1, 2, 3, 4):
            macro.parse(macro.render(preset.template, port))
