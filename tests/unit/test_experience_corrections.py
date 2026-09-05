from __future__ import annotations

import pytest

from trajweave.experience.corrections import is_meaningful_human_correction


@pytest.mark.parametrize(
    "text",
    [
        "yes", "no", "ok", "okay", "sure", "c", "y", "n", "continue", "go",
        "do it", "go ahead", "thanks", "thank you", "yep", "lgtm", "looks good",
        "ok thanks", "yes please", "  OK.  ", "Perfect!", "sounds good",
    ],
)
def test_bare_acknowledgements_are_not_meaningful(text):
    assert is_meaningful_human_correction(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "Don't modify package-lock manually. Run npm install.",
        "use uv instead",
        "revert that",
        "no, keep the old signature",
        "that's wrong",
        "add a test for the empty case",
        "run the migration first",
        "why did you delete config.py?",
        "the build is failing",
        "see src/app/models.py",
        "wrap it in `try/except`",
    ],
)
def test_real_redirections_are_meaningful(text):
    assert is_meaningful_human_correction(text) is True


def test_empty_and_none():
    assert is_meaningful_human_correction(None) is False
    assert is_meaningful_human_correction("") is False
    assert is_meaningful_human_correction("   ") is False


def test_min_tokens_threshold_is_configurable():
    # "make it green" -> 3 tokens; with min_tokens=2 it is "long" and passes,
    # with the default 3 it needs a signal ("make" is directive-ish? no) ...
    assert is_meaningful_human_correction("please fix the flaky test", min_tokens=3) is True
    # a 4-token neutral phrase with no signal and a high threshold stays false
    assert is_meaningful_human_correction("that one over there", min_tokens=6) is False
