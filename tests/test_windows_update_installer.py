"""Unit tests for the generated Windows update installer script.

The script itself can only run on Windows, but the interesting part is pure
text generation: the properties tested here are exactly the ones whose absence
produced a corrupted installation in the field (in-place ``Expand-Archive
-Force`` over DLLs the exiting process still held, giving
``libshiboken: could not import module 'PySide6.QtGui'`` on the next launch).
"""

from pathlib import PureWindowsPath

from src.updater import WindowsUpdateInstaller, _ps_literal


def _make_installer():
    return WindowsUpdateInstaller(exe=PureWindowsPath(r"C:\Apps\SoftEdIBO\SoftEdIBO.exe"),
                                  pid=4242)


def _script():
    return _make_installer().build_script(
        PureWindowsPath(r"C:\Apps\SoftEdIBO\SoftEdIBO-update.zip"))


def test_staging_and_backup_are_siblings_of_the_install():
    # Renaming across directories only stays cheap and atomic on one volume.
    inst = _make_installer()
    assert inst.install_dir == PureWindowsPath(r"C:\Apps\SoftEdIBO")
    assert inst.staging_dir.parent == inst.install_dir.parent
    assert inst.backup_dir.parent == inst.install_dir.parent
    assert inst.staging_dir != inst.backup_dir != inst.install_dir


def test_no_placeholder_survives_substitution():
    assert "@@" not in _script()


def test_never_extracts_over_the_live_installation():
    script = _script()
    assert "-Force" not in script.split("Expand-Archive")[1].splitlines()[0]
    assert "'C:\\Apps\\SoftEdIBO'" not in script.split("Expand-Archive")[1]
    # Extraction targets the staging directory only.
    assert "-DestinationPath $stage" in script
    assert "-C $stage" in script


def test_prefers_tar_over_expand_archive():
    # Expand-Archive is the slow fallback for hosts without bsdtar, never the
    # first choice: it is what made the update take minutes.
    script = _script()
    assert "System32\\tar.exe" in script
    assert "if (Test-Path -LiteralPath $tar) {" in script
    assert script.index("$tar -x -f $zip") < script.index("Expand-Archive -LiteralPath")


def test_waits_for_the_app_process_and_gives_up_rather_than_corrupting():
    script = _script()
    assert "$appPid  = 4242" in script
    assert "Get-Process -Id $appPid" in script
    assert "still running" in script


def test_swap_rolls_back_when_the_new_version_cannot_be_moved_in():
    script = _script()
    swap = script.split("Move-Item -LiteralPath $stage")[1]
    assert "Move-Item -LiteralPath $backup -Destination $install" in swap


def test_paths_are_quoted_as_powershell_literals():
    assert _ps_literal(PureWindowsPath(r"C:\Apps\It's Here")) == "'C:\\Apps\\It''s Here'"
