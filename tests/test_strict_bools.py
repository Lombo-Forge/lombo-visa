import pytest

from lombo_visa.config import ConfigError, _truthy


@pytest.mark.parametrize("word", ["1", "true", "YES", " on "])
def test_on_words(word):
    assert _truthy(word, "DRY_RUN") is True


@pytest.mark.parametrize("word", ["0", "false", "No", "off"])
def test_off_words(word):
    assert _truthy(word, "DRY_RUN") is False


@pytest.mark.parametrize("typo", ["treu", "ture", "flase", "2", "enabled"])
def test_a_typo_is_an_error_not_a_silent_off(typo):
    with pytest.raises(ConfigError, match="DRY_RUN must be true or false"):
        _truthy(typo, "DRY_RUN")
