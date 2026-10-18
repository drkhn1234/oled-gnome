"""Configuration lifecycle tests. Never reads or writes real desktop settings."""

import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("oled_theme", Path(__file__).resolve().parents[1] / "theme.py")
theme = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(theme)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oled test ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config with spaces"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "XDG_STATE_HOME": str(self.home / "state"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def install(self, component="all", activate=False, dry=False):
        theme.install(argparse.Namespace(component=component, activate=activate, dry_run=dry))

    def uninstall(self, dry=False):
        theme.uninstall(argparse.Namespace(dry_run=dry))

    def gtk(self, version="4.0"):
        return theme.locations()["config"] / ("gtk-" + version)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def fake_settings(self):
        values = {
            (theme.INTERFACE, "gtk-theme"): "'My previous theme'",
            (theme.INTERFACE, "color-scheme"): "'default'",
            (theme.USER_THEME, "name"): "'My previous shell'",
        }

        def command(args):
            if args[0] == "gnome-extensions":
                return theme.UUID
            operation = args[1]
            if operation == "list-schemas":
                return theme.INTERFACE + "\n" + theme.USER_THEME
            if operation == "writable":
                return "true"
            if operation == "get":
                return values[(args[2], args[3])]
            if operation == "set":
                values[(args[2], args[3])] = args[4]
                return ""
            raise AssertionError(args)

        return values, command

    def test_fresh_install_and_restore(self):
        targets = [p for p, _, _ in theme.plan("all")]
        self.install()
        self.assertTrue(all(theme.exists(p) for p in targets))
        self.assertIn('oled-gnome/theme.css', (self.gtk() / "gtk.css").read_text())
        self.assertTrue((self.gtk() / "oled-gnome/colors.css").is_file())
        self.uninstall()
        self.assertTrue(all(not theme.exists(p) for p in targets))
        self.assertFalse((theme.locations()["state"] / "current.json").exists())

    def test_existing_css_and_directories_restored(self):
        for target, source, _ in theme.plan("all"):
            self.write(target / "old.txt" if source else target, "original contents")
        self.write(self.gtk() / "unrelated.css", "leave this alone")
        self.install()
        self.uninstall()
        for target, source, _ in theme.plan("all"):
            self.assertEqual((target / "old.txt" if source else target).read_text(), "original contents")
        self.assertEqual((self.gtk() / "unrelated.css").read_text(), "leave this alone")

    def test_symlinks_restored_without_changing_external_targets(self):
        external = self.home / "external.css"
        self.write(external, "external original")
        self.gtk().mkdir(parents=True)
        css = self.gtk() / "gtk.css"
        css.symlink_to(external)
        external_dir = self.home / "external assets"
        self.write(external_dir / "original.txt", "directory original")
        assets = self.gtk() / "oled-gnome"
        assets.symlink_to(external_dir, target_is_directory=True)
        self.install("gtk")
        self.assertFalse(css.is_symlink())
        self.assertEqual(external.read_text(), "external original")
        self.assertEqual((external_dir / "original.txt").read_text(), "directory original")
        self.uninstall()
        self.assertTrue(css.is_symlink())
        self.assertTrue(assets.is_symlink())
        self.assertEqual(css.readlink(), external)
        self.assertEqual(assets.readlink(), external_dir)

    def test_dangling_symlink_restored(self):
        css = self.gtk() / "gtk.css"
        css.parent.mkdir(parents=True)
        css.symlink_to("missing.css")
        self.install("gtk")
        self.uninstall()
        self.assertTrue(css.is_symlink())
        self.assertEqual(css.readlink(), Path("missing.css"))

    def test_dry_run_creates_nothing(self):
        self.install(dry=True)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_component_selection(self):
        self.install("shell")
        self.assertFalse(theme.locations()["config"].exists())
        self.assertTrue((theme.locations()["data"] / "themes" / theme.THEME).exists())
        self.uninstall()
        self.install("gtk")
        self.assertFalse((theme.locations()["data"] / "themes" / theme.THEME).exists())

    def test_second_install_refused(self):
        self.install()
        before = (self.gtk() / "gtk.css").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "already recorded"):
            self.install()
        self.assertEqual((self.gtk() / "gtk.css").read_bytes(), before)

    def test_edits_saved_during_uninstall(self):
        css = self.gtk() / "gtk.css"
        self.write(css, "previous CSS")
        self.install("gtk")
        css.write_text("my new edits")
        self.uninstall()
        self.assertEqual(css.read_text(), "previous CSS")
        recovered = list((theme.locations()["state"] / "recovered").rglob("gtk.css"))
        self.assertTrue(any(p.read_text() == "my new edits" for p in recovered))

    def test_uninstall_dry_run_keeps_installation(self):
        self.install()
        self.uninstall(dry=True)
        self.assertTrue((self.gtk() / "gtk.css").exists())
        self.assertTrue((theme.locations()["state"] / "current.json").exists())

    def test_install_failure_restores_existing_css(self):
        css = self.gtk() / "gtk.css"
        self.write(css, "previous CSS")
        real_copy = theme.copy

        def failing_copy(source, target):
            if source == theme.SOURCE / "gtk-4.0":
                raise OSError("simulated disk failure")
            return real_copy(source, target)

        with patch.object(theme, "copy", side_effect=failing_copy):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                self.install("gtk")
        self.assertEqual(css.read_text(), "previous CSS")
        self.assertFalse((self.gtk("3.0") / "gtk.css").exists())
        self.assertFalse((theme.locations()["state"] / "current.json").exists())

    def test_activation_and_previous_settings_restored(self):
        values, command = self.fake_settings()
        original = dict(values)
        with patch.object(theme, "run", side_effect=command), patch.object(theme.shutil, "which", return_value="/fake/bin/tool"):
            self.install(activate=True)
            self.assertEqual(values[(theme.USER_THEME, "name")], "'OLED-GNOME'")
            self.assertEqual(values[(theme.INTERFACE, "color-scheme")], "'prefer-dark'")
            self.uninstall()
        self.assertEqual(values, original)

    def test_newer_setting_choice_preserved(self):
        values, command = self.fake_settings()
        with patch.object(theme, "run", side_effect=command), patch.object(theme.shutil, "which", return_value="/fake/bin/tool"):
            self.install(activate=True)
            values[(theme.USER_THEME, "name")] = "'Chosen afterward'"
            self.uninstall()
        self.assertEqual(values[(theme.USER_THEME, "name")], "'Chosen afterward'")
        self.assertEqual(values[(theme.INTERFACE, "gtk-theme")], "'My previous theme'")

    def test_missing_extension_fails_before_file_changes(self):
        values, command = self.fake_settings()

        def without_extension(args):
            return "" if args[0] == "gnome-extensions" else command(args)

        with patch.object(theme, "run", side_effect=without_extension), patch.object(theme.shutil, "which", return_value="/fake/bin/tool"):
            with self.assertRaisesRegex(RuntimeError, "Enable.*User Themes"):
                self.install(activate=True)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_activation_failure_rolls_back_files_and_settings(self):
        values, command = self.fake_settings()
        original = dict(values)
        css = self.gtk() / "gtk.css"
        self.write(css, "previous CSS")

        def failing_activation(args):
            if args[0] == "gsettings" and args[1] == "set" and args[2] == theme.USER_THEME and args[4] == "'OLED-GNOME'":
                raise RuntimeError("simulated activation failure")
            return command(args)

        with patch.object(theme, "run", side_effect=failing_activation), patch.object(theme.shutil, "which", return_value="/fake/bin/tool"):
            with self.assertRaisesRegex(RuntimeError, "simulated activation failure"):
                self.install(activate=True)
        self.assertEqual(values, original)
        self.assertEqual(css.read_text(), "previous CSS")
        self.assertFalse((theme.locations()["state"] / "current.json").exists())

    def test_missing_backup_refuses_uninstall_before_removal(self):
        css = self.gtk() / "gtk.css"
        self.write(css, "previous CSS")
        self.install("gtk")
        manifest = theme.locations()["state"] / "current.json"
        state = json.loads(manifest.read_text())
        for record in state["files"]:
            if record["backup"]:
                theme.remove(Path(record["backup"]))
        with self.assertRaisesRegex(RuntimeError, "Missing backup"):
            self.uninstall()
        self.assertIn("oled-gnome/theme.css", css.read_text())


if __name__ == "__main__":
    unittest.main()
