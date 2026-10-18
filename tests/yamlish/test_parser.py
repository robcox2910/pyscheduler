"""Tests for the YAML-subset reader."""

import pytest

from pyscheduler.yamlish import Map, Scalar, Seq, YamlError, parse

LINE_TWO = 2
LINE_THREE = 3
LINE_FOUR = 4


def _text(node: object) -> str:
    """Return the text of a scalar node, failing the test for other nodes."""
    assert isinstance(node, Scalar)
    return node.value


class TestParse:
    """Reading well-formed documents."""

    def test_empty_document(self) -> None:
        """An empty document is an empty mapping on line one."""
        assert parse("") == Map({}, {}, 1)
        assert parse("# only a comment\n\n") == Map({}, {}, 1)

    def test_block_mapping_and_sequence(self) -> None:
        """Mappings and sequences nest by indentation and remember lines."""
        node = parse("a: 1\nb:\n  - x\n  - y\n")
        assert node == Map(
            {
                "a": Scalar("1", 1, quoted=False),
                "b": Seq(
                    [
                        Scalar("x", LINE_THREE, quoted=False),
                        Scalar("y", LINE_FOUR, quoted=False),
                    ],
                    LINE_THREE,
                ),
            },
            {"a": 1, "b": LINE_TWO},
            1,
        )

    def test_sequence_at_key_indent(self) -> None:
        """A sequence may sit at the same indent as its key."""
        node = parse("b:\n- x\n- y\nc: 1\n")
        assert isinstance(node, Map)
        assert node.items["b"] == Seq(
            [Scalar("x", LINE_TWO, quoted=False), Scalar("y", LINE_THREE, quoted=False)],
            LINE_TWO,
        )
        assert _text(node.items["c"]) == "1"

    def test_nested_sequence_of_mappings(self) -> None:
        """Each dash starts a mapping whose keys line up after it."""
        node = parse("- a: 1\n  b: 2\n- c: 3\n")
        assert node == Seq(
            [
                Map(
                    {"a": Scalar("1", 1, quoted=False), "b": Scalar("2", LINE_TWO, quoted=False)},
                    {"a": 1, "b": LINE_TWO},
                    1,
                ),
                Map({"c": Scalar("3", LINE_THREE, quoted=False)}, {"c": LINE_THREE}, LINE_THREE),
            ],
            1,
        )

    def test_empty_value_is_empty_text(self) -> None:
        """A key with nothing after it holds an empty plain scalar."""
        node = parse("a:\nb: 1\n")
        assert isinstance(node, Map)
        assert node.items["a"] == Scalar("", 1, quoted=False)

    def test_scalars_stay_strings(self) -> None:
        """Nothing is converted: no, 08:00, 1e3, ~ and null stay as typed."""
        node = parse("a: no\nb: 08:00\nc: 1e3\nd: ~\ne: null\n")
        assert isinstance(node, Map)
        got = {key: (_text(value), value) for key, value in node.items.items()}
        assert {key: text for key, (text, _) in got.items()} == {
            "a": "no",
            "b": "08:00",
            "c": "1e3",
            "d": "~",
            "e": "null",
        }
        assert all(isinstance(v, Scalar) and not v.quoted for _, v in got.values())

    def test_flow_collections(self) -> None:
        """Flow maps and lists parse, with quoted items marked as quoted."""
        node = parse("p: {a: 1, b: [x, 'y z']}")
        assert node == Map(
            {
                "p": Map(
                    {
                        "a": Scalar("1", 1, quoted=False),
                        "b": Seq([Scalar("x", 1, quoted=False), Scalar("y z", 1, quoted=True)], 1),
                    },
                    {"a": 1, "b": 1},
                    1,
                )
            },
            {"p": 1},
            1,
        )

    def test_multiline_flow_collection(self) -> None:
        """A flow list may wrap over lines; items know their own line."""
        node = parse("deps: [a,\n  b, # note\n  c]\nnext: 1\n")
        assert isinstance(node, Map)
        deps = node.items["deps"]
        assert isinstance(deps, Seq)
        assert [(_text(i), i.line) for i in deps.items] == [("a", 1), ("b", 2), ("c", LINE_THREE)]
        assert _text(node.items["next"]) == "1"
        assert node.key_lines["next"] == LINE_FOUR

    def test_quoted_scalars(self) -> None:
        """Double quotes honour escapes; single quotes only double up."""
        node = parse(
            'a: "l1\\nl2\\t\\"q\\" \\\\ \\u00e9"\nb: \'it\'\'s # fine\'\nc: "é"\nd: "x\\x41"\n'
        )
        assert isinstance(node, Map)
        assert node.items["a"] == Scalar('l1\nl2\t"q" \\ é', 1, quoted=True)
        assert node.items["b"] == Scalar("it's # fine", LINE_TWO, quoted=True)
        assert _text(node.items["c"]) == "é"
        assert _text(node.items["d"]) == "xA"

    def test_quoted_key(self) -> None:
        """Keys may be quoted."""
        node = parse("\"a b\": 1\n'c': 2\n")
        assert isinstance(node, Map)
        assert list(node.items) == ["a b", "c"]

    def test_block_scalars(self) -> None:
        """Literal and folded scalars follow clip, strip and keep chomping."""
        node = parse(
            "a: |\n  one\n  two\n\n"
            "b: |-\n  one\n  two\n\n"
            "c: |+\n  one\n  two\n\n"
            "d: >\n  fold\n  me\n  \n  para\n\n"
            "e: >-\n  fold\n  me\n"
            "f: |\n  keep\n    indent\n"
        )
        assert isinstance(node, Map)
        assert {key: _text(value) for key, value in node.items.items()} == {
            "a": "one\ntwo\n",
            "b": "one\ntwo",
            "c": "one\ntwo\n\n",
            "d": "fold me\npara\n",
            "e": "fold me",
            "f": "keep\n  indent\n",
        }
        assert node.items["a"].line == 1

    def test_block_scalar_in_sequence(self) -> None:
        """A dash may introduce a block scalar."""
        node = parse("- |\n  echo hi\n  echo bye\n- x\n")
        assert isinstance(node, Seq)
        assert _text(node.items[0]) == "echo hi\necho bye\n"
        assert _text(node.items[1]) == "x"

    def test_comments_and_doc_start(self) -> None:
        """Comments are dropped except inside quotes; a leading --- is fine."""
        node = parse("---\n# top\na: 1  # trailing\nb: \"x # y\"\nc: 'p # q'\nd: u#v\n# end\n")
        assert isinstance(node, Map)
        assert node.items["a"] == Scalar("1", LINE_THREE, quoted=False)
        assert node.items["b"] == Scalar("x # y", LINE_FOUR, quoted=True)
        assert _text(node.items["c"]) == "p # q"
        assert _text(node.items["d"]) == "u#v"

    def test_windows_line_endings(self) -> None:
        """Carriage returns are ignored."""
        node = parse("a: 1\r\nb: 2\r\n")
        assert isinstance(node, Map)
        assert _text(node.items["b"]) == "2"

    def test_lone_scalar_document(self) -> None:
        """A document may be a single scalar."""
        assert parse("hello\n") == Scalar("hello", 1, quoted=False)


class TestErrors:
    """Rejecting unsupported or malformed documents."""

    @pytest.mark.parametrize(
        ("text", "line", "fragment"),
        [
            ("a:\n\t- x\n", 2, "tab"),
            ("a: 1\nb: 2\na: 3\n", 3, "duplicate key 'a'"),
            ("a: &x 1\n", 1, "anchors"),
            ("a: 1\nb: *x\n", 2, "aliases"),
            ("a: !!str 1\n", 1, "tags"),
            ("&x a: 1\n", 1, "anchors"),
            ("a: 1\n---\nb: 2\n", 2, "multiple documents"),
            ("? a\n: b\n", 1, "complex keys"),
            ("a: 1\n  b: 2\n", 2, "bad indentation"),
            ("a:\n    b: 1\n  c: 2\n", 3, "bad indentation"),
            ("a: 1\nb\n", 2, "expected"),
            ('a: "oops\n', 1, "unterminated"),
            ('a: "x" y\n', 1, "unexpected text"),
            ('a: "\\q"\n', 1, "escape"),
            ("a: [1, 2\nb: 3\n", 1, "unterminated flow"),
            ("a: {x: 1\n", 1, "unterminated flow"),
            ("a: [1,,2]\n", 1, "unexpected"),
            ("a: {x: 1, x: 2}\n", 1, "duplicate key 'x'"),
            ("a: [1] 2\n", 1, "unexpected text"),
            ("a: |x\n  t\n", 1, "block scalar"),
            ("a: |\n    t\n  u\n", 3, "indentation"),
        ],
    )
    def test_errors_have_lines(self, text: str, line: int, fragment: str) -> None:
        """Each problem names the right line and says what is wrong."""
        with pytest.raises(YamlError) as info:
            parse(text)
        assert info.value.line == line
        assert fragment in info.value.message

    def test_error_text_and_attributes(self) -> None:
        """The error prints as filename:line: message."""
        with pytest.raises(YamlError) as info:
            parse("a: &x 1\n", "jobs.yaml")
        assert info.value.filename == "jobs.yaml"
        assert str(info.value) == f"jobs.yaml:1: {info.value.message}"
        assert isinstance(info.value, ValueError)

    def test_default_filename(self) -> None:
        """Strings are named <string> in errors."""
        with pytest.raises(YamlError, match=r"^<string>:1: "):
            parse("a: *x\n")
