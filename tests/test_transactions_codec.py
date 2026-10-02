"""Phase 1 task 1.10 — canonical JSON codec.

The codec provides deterministic UTF-8 bytes, stable mapping order, generic
JSON decoding, and rejects non-finite or unsupported values.  It grants no
envelope, schema, version, path, or deletion authority.
"""
from __future__ import annotations

import inspect
import unittest

from docker.transactions import codec as codec_module
from docker.transactions.codec import decode, encode


class DeterminismTests(unittest.TestCase):
    def test_bytes_are_deterministic(self) -> None:
        self.assertEqual(
            encode({"b": 1, "a": [2, 3]}),
            b'{"a":[2,3],"b":1}',
        )

    def test_mapping_order_is_stable(self) -> None:
        first = {"z": 1, "a": 2, "m": {"y": 1, "b": 2}}
        second = {"m": {"b": 2, "y": 1}, "a": 2, "z": 1}
        self.assertEqual(encode(first), encode(second))

    def test_mapping_order_is_stable_in_nested_lists(self) -> None:
        self.assertEqual(
            encode([{"b": 1, "a": 2}]),
            b'[{"a":2,"b":1}]',
        )

    def test_float_bytes_are_deterministic(self) -> None:
        self.assertEqual(encode({"v": 0.1}), encode({"v": 0.1}))


class Utf8Tests(unittest.TestCase):
    def test_non_ascii_is_encoded_as_utf8(self) -> None:
        self.assertEqual(encode({"k": "café"}), '{"k":"café"}'.encode("utf-8"))

    def test_output_is_bytes(self) -> None:
        self.assertIsInstance(encode({"k": "v"}), bytes)


class GenericDecodingTests(unittest.TestCase):
    def test_round_trips_generic_values(self) -> None:
        value = {"a": [1, 2.5, True, None], "b": "text", "c": {"nested": []}}
        self.assertEqual(decode(encode(value)), value)

    def test_accepts_bytes_like_input(self) -> None:
        self.assertEqual(decode(bytearray(b'{"a":1}')), {"a": 1})

    def test_decodes_finite_scientific_notation(self) -> None:
        self.assertEqual(decode(b"1e300"), 1e300)
        self.assertEqual(decode(b"-1e300"), -1e300)
        self.assertEqual(decode(b'{"a":[1e300,-1e300]}'), {"a": [1e300, -1e300]})


class DuplicateKeyTests(unittest.TestCase):
    def test_rejects_duplicate_keys_with_identical_values(self) -> None:
        with self.assertRaises(ValueError):
            decode(b'{"a":1,"a":1}')

    def test_rejects_duplicate_keys_with_conflicting_values(self) -> None:
        for payload in (b'{"a":1,"a":2}', b'{"a":2,"a":1}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode(payload)

    def test_rejects_nested_duplicate_keys(self) -> None:
        for payload in (
            b'{"a":{"b":1,"b":2}}',
            b'[{"a":1,"a":2}]',
            b'{"a":[{"b":1,"c":{"d":1,"d":1}}]}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode(payload)

    def test_ordinary_json_still_decodes(self) -> None:
        value = {"a": 1, "b": {"c": [2, 3]}, "d": None}
        self.assertEqual(decode(encode(value)), value)
        self.assertEqual(
            decode(b'{"a":1,"b":{"c":[2,3]}}'),
            {"a": 1, "b": {"c": [2, 3]}},
        )


class RejectionTests(unittest.TestCase):
    def test_rejects_non_finite_floats(self) -> None:
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(ValueError):
                encode({"v": value})

    def test_rejects_non_finite_constant_tokens(self) -> None:
        for payload in (b"NaN", b"Infinity", b"-Infinity"):
            with self.assertRaises(ValueError):
                decode(payload)

    def test_rejects_nested_non_finite_constant_tokens(self) -> None:
        for payload in (
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":-Infinity}',
            b"[1,NaN]",
            b'{"a":[{"b":Infinity}]}',
        ):
            with self.assertRaises(ValueError):
                decode(payload)

    def test_rejects_float_overflow_to_infinity(self) -> None:
        for payload in (b"1e400", b"-1e400", b"1e309", b"-1e309"):
            with self.assertRaises(ValueError):
                decode(payload)

    def test_rejects_nested_float_overflow_to_infinity(self) -> None:
        for payload in (
            b"[1e400]",
            b"[1, -1e400]",
            b'{"value":1e400}',
            b'{"value":-1e400}',
            b'{"a":[{"b":1e400}]}',
            b'{"a":[1, {"b":[-1e400]}]}',
        ):
            with self.assertRaises(ValueError):
                decode(payload)

    def test_rejects_unsupported_values(self) -> None:
        for value in (object(), b"bytes", {1, 2}, {"k": object()}):
            with self.assertRaises(TypeError):
                encode(value)

    def test_rejects_non_string_mapping_keys(self) -> None:
        for key in (1, True, None, (1, 2)):
            with self.assertRaises(TypeError):
                encode({key: "value"})


class BoundedAuthorityTests(unittest.TestCase):
    def test_codec_has_no_filesystem_schema_or_deletion_authority(self) -> None:
        source = inspect.getsource(codec_module)
        for token in (
            "import os",
            "os.",
            "unlink",
            "schema",
            "version",
            "envelope",
            "Path",
            "atomic",
            "durable",
            "delete",
        ):
            self.assertNotIn(token, source, f"codec must not reference {token!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
