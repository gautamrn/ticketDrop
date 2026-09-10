"""Argument parsing only.

These assert the shape of the CLI rather than what it does, because the failure
being guarded against is not a wrong result — it is `invalid choice: 'run'`,
which kills the process before any of the tested code runs. Adding `prefs` once
turned the previously argument-less worker into a subcommand parser, and the
default invocation silently stopped having a name.
"""

from __future__ import annotations

import sys

import pytest

from harness.__main__ import _parse_args


def _args(*argv: str):
    sys.argv = ["harness", *argv]
    return _parse_args()


def test_bare_invocation_still_means_run_the_worker():
    assert _args().command is None


def test_run_is_an_explicit_alias_for_the_default():
    assert _args("run").command == "run"


def test_prefs_add_requires_a_label():
    with pytest.raises(SystemExit):
        _args("prefs", "add")


def test_prefs_add_defaults_to_artist_at_unit_weight():
    args = _args("prefs", "add", "--label", "Prospa")
    assert (args.command, args.action, args.kind, args.weight) == ("prefs", "add", "artist", 1.0)


def test_prefs_rejects_a_kind_the_embedder_cannot_produce():
    # sound_signature is a 12-d audio vector, not an embedded string, so 'sound'
    # is deliberately absent from TEXT_KINDS.
    with pytest.raises(SystemExit):
        _args("prefs", "add", "--label", "warehouse", "--kind", "sound")


def test_prefs_requires_an_action():
    with pytest.raises(SystemExit):
        _args("prefs")
