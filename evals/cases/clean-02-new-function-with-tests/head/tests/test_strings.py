from textutil.strings import slugify, truncate


def test_slugify_strips_accents_and_punctuation():
    assert slugify("Héllo, World!") == "hello-world"


def test_slugify_of_only_punctuation_is_empty():
    assert slugify("  --  ") == ""


def test_truncate_adds_an_ellipsis():
    assert truncate("abcdef", 4) == "abc…"
