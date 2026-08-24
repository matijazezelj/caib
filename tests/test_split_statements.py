from caib_core.migrate import split_statements


def test_basic_split():
    assert split_statements("SELECT 1;\nSELECT 2;") == ["SELECT 1", "SELECT 2"]


def test_semicolons_inside_strings_and_dollar_quotes():
    sql = (
        "CREATE FUNCTION f() RETURNS text LANGUAGE sql AS $$ SELECT 'a;b'; $$;\n"
        "SELECT 'x;y';"
    )
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert "'a;b'" in stmts[0]
    assert stmts[1] == "SELECT 'x;y'"


def test_line_comments_and_comment_only_fragments_ignored():
    sql = "-- header; with semicolon\nSELECT 1; -- trailing; comment\n-- tail\n"
    stmts = split_statements(sql)
    # Leading comments stay attached to their statement (harmless SQL);
    # semicolons inside comments must not split, and comment-only tails vanish.
    assert len(stmts) == 1
    assert stmts[0].endswith("SELECT 1")


def test_escaped_quote():
    assert split_statements("SELECT 'it''s;fine'; SELECT 2;") == [
        "SELECT 'it''s;fine'",
        "SELECT 2",
    ]
