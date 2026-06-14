"""Fixture tests for cc_token_tracker.parser (Ticket 1).

Each fixture line is an inline string constant defined right next to the
assertion that exercises it. Fixtures are authoritative: if a fixture and the
parser disagree, the parser is wrong -- do not edit the fixtures here.
"""

import unittest

from cc_token_tracker.parser import TranscriptRecord, Usage, parse_line


class ValidLines(unittest.TestCase):
    """Each valid line must parse to a populated record."""

    def test_real_prompt(self):
        real_prompt = (
            '{"type":"user","message":{"role":"user",'
            '"content":"fix the failing test in auth.py"}}'
        )
        rec = parse_line(real_prompt)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="user",
                role="user",
                usage=None,
                stop_reason=None,
                is_meta=False,
                is_sidechain=False,
                text="fix the failing test in auth.py",  # string content retained
            ),
        )

    def test_cwd_captured_from_top_level(self):
        line = (
            '{"type":"user","cwd":"/home/me/cc tracker","message":'
            '{"role":"user","content":"hi"}}'
        )
        self.assertEqual(parse_line(line).cwd, "/home/me/cc tracker")

    def test_cwd_absent_is_none(self):
        line = '{"type":"user","message":{"role":"user","content":"hi"}}'
        self.assertIsNone(parse_line(line).cwd)

    def test_assistant_interior(self):
        assistant_interior = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"tool_use","id":"toolu_01","name":"Read","input":{}}],'
            '"stop_reason":"tool_use","usage":{"input_tokens":1200,"output_tokens":85,'
            '"cache_creation_input_tokens":4000,"cache_read_input_tokens":0}}}'
        )
        rec = parse_line(assistant_interior)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="assistant",
                role="assistant",
                usage=Usage(
                    input_tokens=1200,
                    output_tokens=85,
                    cache_creation_input_tokens=4000,
                    cache_read_input_tokens=0,
                ),
                stop_reason="tool_use",
                is_meta=False,
                is_sidechain=False,
            ),
        )

    def test_assistant_final(self):
        assistant_final = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"Done."}],'
            '"stop_reason":"end_turn","usage":{"input_tokens":300,"output_tokens":120,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":8100}}}'
        )
        rec = parse_line(assistant_final)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="assistant",
                role="assistant",
                usage=Usage(
                    input_tokens=300,
                    output_tokens=120,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=8100,
                ),
                stop_reason="end_turn",
                is_meta=False,
                is_sidechain=False,
            ),
        )

    def test_tool_result_user(self):
        # Decoy: type "user" but content is a tool_result. Ticket 2 will reject
        # it; Ticket 1 just parses it into a record.
        tool_result_user = (
            '{"type":"user","message":{"role":"user",'
            '"content":[{"type":"tool_result","tool_use_id":"toolu_01",'
            '"content":"file contents"}]}}'
        )
        rec = parse_line(tool_result_user)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="user",
                role="user",
                usage=None,
                stop_reason=None,
                is_meta=False,
                is_sidechain=False,
                is_tool_result=True,  # content is a tool_result block
            ),
        )

    def test_meta_line(self):
        meta_line = (
            '{"type":"user","isMeta":true,"message":{"role":"user",'
            '"content":"local command output"}}'
        )
        rec = parse_line(meta_line)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="user",
                role="user",
                usage=None,
                stop_reason=None,
                is_meta=True,
                is_sidechain=False,
                text="local command output",  # string content retained
            ),
        )

    def test_usage_partial(self):
        # Pins the absent-subfield rule: counts the line omits stay None, not 0.
        usage_partial = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"stop_reason":"end_turn","usage":{"input_tokens":100,"output_tokens":50}}}'
        )
        rec = parse_line(usage_partial)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.usage.input_tokens, 100)
        self.assertEqual(rec.usage.output_tokens, 50)
        self.assertIsNone(rec.usage.cache_creation_input_tokens)
        self.assertIsNone(rec.usage.cache_read_input_tokens)

    def test_sidechain_line(self):
        sidechain_line = (
            '{"type":"assistant","isSidechain":true,"message":{"role":"assistant",'
            '"content":[{"type":"text","text":"subagent reply"}],'
            '"stop_reason":"end_turn","usage":{"input_tokens":500,"output_tokens":40,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}'
        )
        rec = parse_line(sidechain_line)
        self.assertEqual(
            rec,
            TranscriptRecord(
                type="assistant",
                role="assistant",
                usage=Usage(
                    input_tokens=500,
                    output_tokens=40,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                stop_reason="end_turn",
                is_meta=False,
                is_sidechain=True,
            ),
        )


class TextCapture(unittest.TestCase):
    """text: verbatim message.content when it is a string, else None."""

    def test_text_retained_for_typed_prompt(self):
        # String content is kept raw -- no collapse/truncate here (display's job).
        line = (
            '{"type":"user","message":{"role":"user",'
            '"content":"fix the   failing\\ntest"}}'
        )
        rec = parse_line(line)
        self.assertEqual(rec.text, "fix the   failing\ntest")

    def test_text_none_for_block_content(self):
        # A content block-list (here tool_use) is not a typed string -> None.
        line = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"tool_use","id":"t1","name":"Read","input":{}}],'
            '"stop_reason":"tool_use"}}'
        )
        rec = parse_line(line)
        self.assertIsNone(rec.text)


class MalformedLines(unittest.TestCase):
    """Each malformed/partial line must return None and never raise.

    (If parse_line raised, these calls would error out of the test, so a clean
    `assertIsNone` also proves the no-raise half of the DoD.)
    """

    def test_truncated_json(self):
        truncated_json = '{"type":"assistant","message":{"role":"assi'
        self.assertIsNone(parse_line(truncated_json))

    def test_empty_string(self):
        empty_string = ""
        self.assertIsNone(parse_line(empty_string))

    def test_whitespace_only(self):
        whitespace_only = "   "
        self.assertIsNone(parse_line(whitespace_only))

    def test_json_not_object(self):
        json_not_object = "[1, 2, 3]"
        self.assertIsNone(parse_line(json_not_object))

    def test_json_number(self):
        json_number = "42"
        self.assertIsNone(parse_line(json_number))

    def test_object_no_type(self):
        object_no_type = '{"message":{"role":"user","content":"hi"}}'
        self.assertIsNone(parse_line(object_no_type))

    def test_type_null(self):
        # A null type is malformed, same as absent type.
        type_null = '{"type":null,"message":{"role":"user","content":"x"}}'
        self.assertIsNone(parse_line(type_null))

    def test_type_nonstring(self):
        # A non-string type (here an int) is malformed.
        type_nonstring = '{"type":42,"message":{"role":"assistant","content":[]}}'
        self.assertIsNone(parse_line(type_nonstring))


class ToolResultFlag(unittest.TestCase):
    """is_tool_result: True for a user tool_result line, False otherwise."""

    def test_is_tool_result_true_when_content_is_tool_result(self):
        # Present true-case: user line whose content is a tool_result block.
        line = (
            '{"type":"user","message":{"role":"user",'
            '"content":[{"type":"tool_result","tool_use_id":"toolu_01",'
            '"content":"file contents"}]}}'
        )
        rec = parse_line(line)
        self.assertTrue(rec.is_tool_result)

    def test_is_tool_result_false_for_typed_prompt(self):
        # Absent default: a real typed prompt (string content) is not a result.
        line = (
            '{"type":"user","message":{"role":"user",'
            '"content":"fix the failing test in auth.py"}}'
        )
        rec = parse_line(line)
        self.assertFalse(rec.is_tool_result)

    def test_is_tool_result_false_for_assistant_line(self):
        # Non-user lines are never tool_result.
        line = (
            '{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"Done."}],"stop_reason":"end_turn"}}'
        )
        rec = parse_line(line)
        self.assertFalse(rec.is_tool_result)


if __name__ == "__main__":
    unittest.main()
