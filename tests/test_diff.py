from app.github.diff import hunk_for_line, parse_patch


def test_added_and_context_lines_get_new_file_numbers():
    patch = (
        "@@ -10,3 +10,4 @@ def f():\n     a = 1\n-    b = 2\n+    b = 3\n+    c = 4\n     return a"
    )

    [hunk] = parse_patch(patch)

    assert (hunk.start, hunk.end) == (10, 13)
    assert hunk.added == {11, 12}


def test_multiple_hunks():
    patch = "@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -20,2 +21,2 @@\n x\n-y\n+z"

    hunks = parse_patch(patch)

    assert [(h.start, h.end, h.added) for h in hunks] == [(1, 3, {2}), (21, 22, {22})]


def test_new_file():
    patch = "@@ -0,0 +1,3 @@\n+one\n+two\n+three"

    [hunk] = parse_patch(patch)

    assert (hunk.start, hunk.end, hunk.added) == (1, 3, {1, 2, 3})


def test_no_newline_marker_is_not_a_line():
    patch = "@@ -1,1 +1,2 @@\n a\n+b\n\\ No newline at end of file"

    [hunk] = parse_patch(patch)

    assert (hunk.start, hunk.end, hunk.added) == (1, 2, {2})


def test_missing_count_means_one_line():
    [hunk] = parse_patch("@@ -5 +5 @@\n-old\n+new")

    assert (hunk.start, hunk.end, hunk.added) == (5, 5, {5})


def test_pure_deletion_has_nothing_to_comment_on():
    assert parse_patch("@@ -3,2 +2,0 @@\n-gone\n-also gone") == []


def test_hunk_for_line():
    hunks = parse_patch("@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -20,2 +21,2 @@\n x\n-y\n+z")

    assert hunk_for_line(hunks, 2) is hunks[0]
    assert hunk_for_line(hunks, 22) is hunks[1]
    assert hunk_for_line(hunks, 10) is None
