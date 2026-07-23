"""Tests for wherewent.callsite: resolve_call_site() and is_library_file()."""

import os

import wherewent
from wherewent.callsite import is_library_file, resolve_call_site


def _helper_that_calls_resolve_call_site():
    """A plain user-code helper — resolve_call_site() should attribute here."""
    return resolve_call_site()


def test_resolve_call_site_attributes_to_helper():
    result = _helper_that_calls_resolve_call_site()
    assert result is not None
    file, line, func = result
    assert os.path.basename(file) == os.path.basename(__file__)
    assert func == "_helper_that_calls_resolve_call_site"
    assert isinstance(line, int)
    assert line > 0


def test_resolve_call_site_line_number_is_reasonable():
    file, line, func = _helper_that_calls_resolve_call_site()
    # The call to resolve_call_site() lives inside the helper above; sanity
    # check the reported line is within this file's bounds.
    with open(__file__) as f:
        total_lines = sum(1 for _ in f)
    assert 0 < line <= total_lines


def test_is_library_file_sqlalchemy_path():
    path = "/usr/lib/python3.10/site-packages/sqlalchemy/engine/base.py"
    assert is_library_file(path) is True


def test_is_library_file_frozen_importlib():
    assert is_library_file("<frozen importlib._bootstrap>") is True


def test_is_library_file_wherewent_package_itself():
    # wherewent's own source must be treated as library, not user code.
    assert is_library_file(wherewent.__file__) is True


def test_is_library_file_demo_job_is_not_library():
    path = "/Users/mac/Documents/PersonalProjects/wherewent/demo/naive_job.py"
    assert is_library_file(path) is False


def test_is_library_file_memoized_stable_result():
    path = "/usr/lib/python3.10/site-packages/sqlalchemy/engine/base.py"
    first = is_library_file(path)
    second = is_library_file(path)
    assert first == second is True
