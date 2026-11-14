import pytest

from dayone.steps.terminal_steps import UnsupportedSpecifier, satisfies


@pytest.mark.parametrize(
    ("version", "specifier", "expected"),
    [
        ("3.10.12", ">=3.12", False),
        ("3.11.2", ">=3.12", False),
        ("3.12.0", ">=3.12", True),
        ("3.12.13", ">=3.12", True),
        ("3.13.1", ">=3.12,<3.13", False),
        ("3.12.13", ">=3.12, <3.13", True),
        ("3.12.0", "==3.12", True),
        ("3.12.1", "==3.12", False),
        ("3.12.1", "!=3.12.1", False),
    ],
)
def test_satisfies(version, specifier, expected):
    assert satisfies(version, specifier) is expected


def test_wildcards_are_refused_not_guessed():
    with pytest.raises(UnsupportedSpecifier):
        satisfies("3.12.1", "==3.12.*")
