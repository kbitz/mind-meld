"""Installer progress is derived from observed counters, never elapsed time."""

import pytest

from mind_meld.updateprogress import PipxProgressParser, UpdateProgress


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            "133.4/784.7 kB 12.0 MB/s eta 0:00:01",
            UpdateProgress("Downloading", 133.4, 784.7, "133.4/784.7 kB"),
        ),
        ("1.5/6.0 MB", UpdateProgress("Downloading", 1.5, 6.0, "1.5/6.0 MB")),
        ("1/3 [hypothesis]", UpdateProgress("Installing", 1, 3, "1/3 packages")),
        (
            "Receiving objects: 42% (42/100), 1.2 MiB",
            UpdateProgress("Receiving objects", 42, 100, "42/100 objects"),
        ),
        ("Resolving deltas: 10% (4/40)", UpdateProgress("Resolving deltas", 4, 40, "4/40 deltas")),
        (
            "Cloning https://github.com/kbitz/mind-meld.git to /private/tmp/build",
            UpdateProgress("Fetching source"),
        ),
        ("Installing build dependencies ... started", UpdateProgress("Preparing build")),
        ("Preparing metadata (pyproject.toml) ... done", UpdateProgress("Preparing package")),
        (
            "Building wheel for mind-meld (pyproject.toml) ... started",
            UpdateProgress("Building wheel"),
        ),
        ("Installing collected packages: mind-meld", UpdateProgress("Installing")),
    ],
)
def test_recognized_installer_output(line, expected):
    assert PipxProgressParser().feed(line + "\r") == [expected]


def test_partial_terminal_frames_and_ansi_are_not_display_text():
    parser = PipxProgressParser()
    assert parser.feed("\x1b[?25l\r\x1b[32") == []
    assert parser.feed("m133.4/784.") == []
    assert parser.feed("7 kB\x1b[0m\r") == [
        UpdateProgress("Downloading", 133.4, 784.7, "133.4/784.7 kB")
    ]
    assert parser.feed("\x1b[2K532.5/784.7 kB\r") == [
        UpdateProgress("Downloading", 532.5, 784.7, "532.5/784.7 kB")
    ]
    assert parser.feed("133.4/784.7 kB\r") == [
        UpdateProgress("Downloading", 133.4, 784.7, "133.4/784.7 kB")
    ]  # A new download can start; this is not an overall monotonic percentage.


@pytest.mark.parametrize(
    "line",
    [
        "1/0 kB",
        "5/3 [pkg]",
        "NaN/20 MB",
        "100000000001/10 kB",
        "100000001/10 [pkg]",
        "0.00001/10 MB",
        "Receiving objects: 100% (5/3)",
        "Resolving deltas: 0% (0/0)",
        "unrecognized output",
    ],
)
def test_unknown_or_invalid_output_never_advances_progress(line):
    parser = PipxProgressParser()
    first = parser.feed("1/3 [pkg]\r")
    assert first == [UpdateProgress("Installing", 1, 3, "1/3 packages")]
    assert parser.feed(line + "\r") == []
    assert parser.feed("1/3 [pkg]\r") == []


def test_build_stages_have_no_estimated_fraction_and_finish_flushes_once():
    parser = PipxProgressParser()
    assert parser.feed("Building wheel for mind-meld\r\n") == [UpdateProgress("Building wheel")]
    assert parser.feed("2/3 [pkg]") == []  # A counter waits for its line to end.
    assert parser.finish() == [UpdateProgress("Installing", 2, 3, "2/3 packages")]
    assert parser.finish() == []


def test_unterminated_spinner_step_shows_its_label_when_it_starts():
    # pip's interactive spinner ends a step's line only when the step finishes.
    parser = PipxProgressParser()
    started = parser.feed("  Installing build dependencies ... ")
    assert started == [UpdateProgress("Preparing build")]
    assert parser.feed("\b|\b \b\b/") == []
    assert parser.feed("\b \bdone\r\n") == []
    assert parser.feed("  Building wheel for pkg (pyproject.toml) ... ") == [
        UpdateProgress("Building wheel")
    ]


def test_uninstall_messages_preserve_the_current_package_count():
    parser = PipxProgressParser()
    assert parser.feed("1/3 [hypothesis]\r") == [UpdateProgress("Installing", 1, 3, "1/3 packages")]
    assert parser.feed("Attempting uninstall: old-package\nUninstalling old-package:\n") == []
    assert parser.feed("1/3 [hypothesis]\r2/3 [mind-meld]\r") == [
        UpdateProgress("Installing", 2, 3, "2/3 packages")
    ]
    assert parser.feed("Downloading next-package\n") == [UpdateProgress("Downloading")]
    assert parser.feed("1.0/1.0 MB\rDownloading another-package\n") == [
        UpdateProgress("Downloading", 1, 1, "1.0/1.0 MB"),
        UpdateProgress("Downloading"),
    ]


def test_oversize_unknown_line_does_not_grow_the_parser_buffer():
    parser = PipxProgressParser()
    # The bound keeps only the tail of an oversize line, so this label is never read.
    assert parser.feed("Building wheel for mind-meld" + "y" * 9000) == []
    assert parser.finish() == []
    assert parser.feed("x" * 100_000 + "\r2/3 [pkg]\r") == [
        UpdateProgress("Installing", 2, 3, "2/3 packages")
    ]
