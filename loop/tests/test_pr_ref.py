"""Tests for ``extract_pr_ref`` — free-text GitHub PR-reference extraction.

This is the pure helper the Adjudicator runs over a candidate's message text to
discover the PR a loop is about, so the Verifier / Action auto-close beat can ground
it in real GitHub state (Req 4, 8). Both example-based and property-based tests are
included: the examples pin the exact forms a human would type; the properties assert
universal round-trip / robustness behaviour over many generated inputs.

The normalized output shape is exactly what :meth:`loop.verifier.verifier.PrRef.parse`
consumes — these tests assert that compatibility directly.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loop.adjudicator.pr_ref import extract_pr_ref
from loop.verifier.verifier import PrRef


# --------------------------------------------------------------------------- #
# Example-based unit tests
# --------------------------------------------------------------------------- #
def test_shorthand_form_is_extracted_and_normalized() -> None:
    assert extract_pr_ref("owner/repo#123") == "owner/repo#123"


def test_url_form_is_extracted_and_normalized() -> None:
    assert (
        extract_pr_ref("https://github.com/owner/repo/pull/123") == "owner/repo#123"
    )


def test_url_without_scheme_is_extracted() -> None:
    assert extract_pr_ref("github.com/owner/repo/pull/7") == "owner/repo#7"


def test_url_with_trailing_path_and_query_is_extracted() -> None:
    text = "see https://github.com/rajj28/loop-demo/pull/2/files?w=1 please"
    assert extract_pr_ref(text) == "rajj28/loop-demo#2"


def test_no_ref_returns_none() -> None:
    assert extract_pr_ref("just a normal message with no pull request") is None


def test_empty_text_returns_none() -> None:
    assert extract_pr_ref("") is None


def test_picks_valid_ref_out_of_a_sentence() -> None:
    text = "Hey, gentle nudge on PR acme/widgets#42 — can't merge without you!"
    assert extract_pr_ref(text) == "acme/widgets#42"


def test_ignores_non_pr_text_that_looks_pathlike() -> None:
    # A bare path with no '#<number>' and no /pull/ is not a PR reference.
    assert extract_pr_ref("check the file src/main/app.py for details") is None


def test_first_ref_in_document_order_wins() -> None:
    text = "first owner1/repo1#1 then https://github.com/owner2/repo2/pull/2"
    assert extract_pr_ref(text) == "owner1/repo1#1"


def test_owner_repo_with_dots_dashes_underscores() -> None:
    assert extract_pr_ref("my.org/cool-repo_v2#99") == "my.org/cool-repo_v2#99"


def test_real_demo_pr_url_normalizes_to_demo_ref() -> None:
    # The exact form the seed_sandbox Bob message uses for the live demo.
    text = (
        "gentle nudge on my PR https://github.com/rajj28/loop-demo/pull/2 — "
        "can't merge without your review."
    )
    assert extract_pr_ref(text) == "rajj28/loop-demo#2"


def test_extracted_ref_is_parseable_by_verifier_prref() -> None:
    # The normalized output is exactly what the Verifier consumes downstream.
    ref = extract_pr_ref("https://github.com/rajj28/loop-demo/pull/2")
    assert ref is not None
    parsed = PrRef.parse(ref)
    assert parsed.owner == "rajj28"
    assert parsed.repo == "loop-demo"
    assert parsed.number == 2


# --------------------------------------------------------------------------- #
# Property-based tests (>=100 examples, enforced by the workspace conftest)
# --------------------------------------------------------------------------- #
# GitHub owner/repo names: alnum plus '-', '_', '.'. We keep them non-empty and
# avoid a leading/trailing shape that would let surrounding generated text bleed in.
_names = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
    min_size=1,
    max_size=20,
)
_numbers = st.integers(min_value=1, max_value=10_000_000)
# Filler text that contains no '/', '#', or 'github.com' so it can never itself
# look like a PR reference.
_filler = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz ,.!?",
    max_size=40,
)


# Feature: loop-obligation-agent, PR-reference extraction round-trips to a parseable ref.
# For any valid owner/repo/number rendered in shorthand form, extract_pr_ref returns
# the normalized "owner/repo#number" and the Verifier's PrRef.parse recovers the parts.
# Validates: Requirements 4, 8
@given(owner=_names, repo=_names, number=_numbers, pre=_filler, post=_filler)
def test_shorthand_roundtrips_through_extract_and_parse(
    owner: str, repo: str, number: int, pre: str, post: str
) -> None:
    text = f"{pre} {owner}/{repo}#{number} {post}"
    ref = extract_pr_ref(text)
    assert ref is not None
    parsed = PrRef.parse(ref)
    assert parsed.number == number
    # The recovered ref re-normalizes to itself (idempotent normalization).
    assert f"{parsed.owner}/{parsed.repo}#{parsed.number}" == ref


# Feature: loop-obligation-agent, PR-URL extraction normalizes to the shorthand ref.
# For any valid owner/repo/number rendered as a github.com /pull/ URL, extract_pr_ref
# returns the normalized shorthand and PrRef.parse recovers the same number.
# Validates: Requirements 4, 8
@given(owner=_names, repo=_names, number=_numbers, pre=_filler, post=_filler)
def test_url_form_normalizes_to_shorthand(
    owner: str, repo: str, number: int, pre: str, post: str
) -> None:
    text = f"{pre} https://github.com/{owner}/{repo}/pull/{number} {post}"
    ref = extract_pr_ref(text)
    assert ref is not None
    parsed = PrRef.parse(ref)
    assert parsed.number == number
    assert f"{parsed.owner}/{parsed.repo}#{parsed.number}" == ref


# Feature: loop-obligation-agent, text with no PR reference yields None.
# For any filler text containing no '/', '#', or a github pull URL, extraction is None.
# Validates: Requirements 4, 8
@given(text=_filler)
def test_no_ref_text_always_returns_none(text: str) -> None:
    assert extract_pr_ref(text) is None
