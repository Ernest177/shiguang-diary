"""Installer scenarios use a fake desktop and never change the real user's links."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows desktop shortcuts")
class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="diary shortcut ")
        self.root = Path(self.temp.name).resolve() / "项目 with spaces"
        (self.root / "tools").mkdir(parents=True)
        self.desktop = self.root / "fake desktop"
        self.desktop.mkdir()
        self.shortcut = self.desktop / "拾光日记.lnk"
        shutil.copyfile(ROOT / "tools" / "install_shortcut.ps1", self.root / "tools" / "install_shortcut.ps1")
        shutil.copyfile(ROOT / "tools" / "shortcut.ps1", self.root / "tools" / "shortcut.ps1")
        (self.root / "launch.ps1").write_text("# isolated launcher fixture", encoding="ascii")
        self.helper = self.root / "fixture.ps1"
        self.helper.write_text('''param([string]$Mode,[string]$Shortcut,[string]$Target)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'tools\\shortcut.ps1')
$link = [Shiguang.DesktopShortcut]::Load($Shortcut)
if ($Mode -eq 'create') { $link.TargetPath = $Target; $link.Save($Shortcut) }
@{ target = $link.TargetPath; arguments = $link.Arguments; directory = $link.WorkingDirectory } | ConvertTo-Json -Compress
''', encoding="ascii")

    def tearDown(self):
        self.temp.cleanup()

    def powershell(self, script, *args):
        return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)],
                              capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)

    def install(self):
        return self.powershell(self.root / "tools" / "install_shortcut.ps1", "-DesktopDirectory", self.desktop)

    def test_migrate_owned_legacy_shortcut_backup_and_repeat_install(self):
        legacy = self.desktop / "启动拾光.vbs - 快捷方式.lnk"
        created = self.powershell(self.helper, "-Mode", "create", "-Shortcut", legacy, "-Target", self.root / "启动拾光.vbs")
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors="replace"))
        original = legacy.read_bytes()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(json.loads(result.stdout)["installed"])
        self.assertTrue(self.shortcut.is_file())
        self.assertFalse(legacy.exists())
        backups = list((self.root / "data" / "launcher-backups").rglob("*.lnk"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        result = self.powershell(self.helper, "-Mode", "read", "-Shortcut", self.shortcut)
        self.assertEqual(result.returncode, 0)
        info = json.loads(result.stdout)
        self.assertEqual(Path(info["target"]).name.lower(), "powershell.exe")
        self.assertIn('-WindowStyle Hidden -File "' + str(self.root / "launch.ps1") + '"', info["arguments"])
        self.assertEqual(Path(info["directory"]), self.root)
        unchanged = self.shortcut.read_bytes()
        repeated = self.install()
        self.assertEqual(repeated.returncode, 0, repeated.stderr.decode(errors="replace"))
        self.assertEqual(self.shortcut.read_bytes(), unchanged)
        self.assertEqual(json.loads(repeated.stdout)["replaced"], 0)

    def test_same_name_from_another_app_is_not_replaced(self):
        created = self.powershell(self.helper, "-Mode", "create", "-Shortcut", self.shortcut,
                                  "-Target", Path(os.environ["SystemRoot"]) / "notepad.exe")
        self.assertEqual(created.returncode, 0)
        original = self.shortcut.read_bytes()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shortcut.read_bytes(), original)
        self.assertFalse((self.root / "data" / "launcher-backups").exists())

    def test_project_identity_allows_reinstalling_after_a_move(self):
        installed = self.install()
        self.assertEqual(installed.returncode, 0)
        moved = self.root.parent / "moved project"
        (moved / "tools").mkdir(parents=True)
        (moved / "data").mkdir()
        for source, target in ((self.root / "tools" / "install_shortcut.ps1", moved / "tools" / "install_shortcut.ps1"),
                               (self.root / "tools" / "shortcut.ps1", moved / "tools" / "shortcut.ps1"),
                               (self.root / "launch.ps1", moved / "launch.ps1"),
                               (self.root / "data" / "desktop-entry-id.json", moved / "data" / "desktop-entry-id.json")):
            shutil.copyfile(source, target)
        reinstalled = self.powershell(moved / "tools" / "install_shortcut.ps1", "-DesktopDirectory", self.desktop)
        self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr.decode(errors="replace"))
        result = self.powershell(self.helper, "-Mode", "read", "-Shortcut", self.shortcut)
        info = json.loads(result.stdout)
        self.assertIn(str(moved / "launch.ps1"), info["arguments"])
        self.assertEqual(Path(info["directory"]), moved)


if __name__ == "__main__":
    unittest.main()
