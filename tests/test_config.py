"""Tests for credential normalization in config.

These exercise the OAuth-credential cleanup that fixed the
invalid_request "invalid character at index 0" failure: env var values that
arrive wrapped in quotes or carrying invisible edge characters (BOM, zero-width
spaces, NBSP) must be normalized before they reach iRacing's token endpoint.

All values here are fake and set inside the test via monkeypatch — the real
IRACING_* env vars are never read.
"""

import pytest

from iracing_sr_optimizer.config import _clean_credential, _strip_edges


class TestStripEdges:
    def test_plain_value_unchanged(self):
        assert _strip_edges("user@example.com") == "user@example.com"

    def test_surrounding_whitespace(self):
        assert _strip_edges("  user@example.com \t\n") == "user@example.com"

    def test_leading_bom(self):
        assert _strip_edges("﻿user@example.com") == "user@example.com"

    def test_zero_width_and_nbsp_edges(self):
        assert _strip_edges("​ user@example.com‍") == "user@example.com"

    def test_does_not_touch_interior(self):
        # Only edges are stripped; an interior space must survive.
        assert _strip_edges("  two words  ") == "two words"

    def test_empty(self):
        assert _strip_edges("") == ""


class TestCleanCredential:
    def test_missing_var_is_empty(self, monkeypatch):
        monkeypatch.delenv("FAKE_CRED", raising=False)
        assert _clean_credential("FAKE_CRED") == ""

    def test_plain_value(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", "secret-value")
        assert _clean_credential("FAKE_CRED") == "secret-value"

    def test_strips_double_quotes(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", '"secret-value"')
        assert _clean_credential("FAKE_CRED") == "secret-value"

    def test_strips_single_quotes(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", "'secret-value'")
        assert _clean_credential("FAKE_CRED") == "secret-value"

    def test_strips_quotes_then_whitespace(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", '  "  secret-value  "  ')
        assert _clean_credential("FAKE_CRED") == "secret-value"

    def test_strips_bom_then_quotes(self, monkeypatch):
        # The real-world failure mode: BOM outside a quote-wrapped value.
        monkeypatch.setenv("FAKE_CRED", '﻿"secret-value"')
        assert _clean_credential("FAKE_CRED") == "secret-value"

    def test_unbalanced_quote_preserved(self, monkeypatch):
        # A leading-only quote is not a wrapping pair — keep it; stripping it
        # would corrupt a value that legitimately starts with a quote.
        monkeypatch.setenv("FAKE_CRED", '"secret-value')
        assert _clean_credential("FAKE_CRED") == '"secret-value'

    def test_mismatched_quotes_preserved(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", "'secret-value\"")
        assert _clean_credential("FAKE_CRED") == "'secret-value\""

    def test_only_outer_layer_stripped(self, monkeypatch):
        # Strip exactly one matching layer, not nested quotes.
        monkeypatch.setenv("FAKE_CRED", '""secret-value""')
        assert _clean_credential("FAKE_CRED") == '"secret-value"'

    def test_interior_quotes_preserved(self, monkeypatch):
        monkeypatch.setenv("FAKE_CRED", 'a"b"c')
        assert _clean_credential("FAKE_CRED") == 'a"b"c'
