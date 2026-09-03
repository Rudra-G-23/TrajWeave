from __future__ import annotations

from trajweave.experience.signatures import error_signature


def test_stable_across_paths_and_numbers():
    a = error_signature(
        "/home/rudra/proj/tests/test_billing.py:42: AssertionError: expected 3 got 5"
    )
    b = error_signature(
        "/srv/ci/build/tests/test_billing.py:9: AssertionError: expected 12 got 99"
    )
    assert a == b
    assert "assertionerror" in a
    assert "42" not in a and "/home" not in a


def test_picks_the_error_line_out_of_a_traceback():
    text = (
        "Traceback (most recent call last):\n"
        '  File "app/main.py", line 10, in <module>\n'
        "    run()\n"
        "ModuleNotFoundError: No module named 'requests'\n"
    )
    sig = error_signature(text)
    assert sig.startswith("modulenotfounderror: no module named")
    assert "requests" in sig


def test_empty_and_none():
    assert error_signature(None) == ""
    assert error_signature("") == ""
    assert error_signature("=====") == ""


def test_uuid_and_hex_are_masked():
    sig = error_signature(
        "run 3f2504e0-4f89-11d3-9a0c-0305e82c3301 failed at 0xDEADBEEF"
    )
    assert "<uuid>" in sig and "<addr>" in sig


def test_length_capped():
    assert len(error_signature("x " * 500)) <= 200
