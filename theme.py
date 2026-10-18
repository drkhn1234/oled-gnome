#!/usr/bin/env python3
"""Install and restore OLED GNOME using only the Python standard library."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

THEME = "OLED-GNOME"
UUID = "user-theme@gnome-shell-extensions.gcampax.github.com"
INTERFACE = "org.gnome.desktop.interface"
USER_THEME = "org.gnome.shell.extensions.user-theme"
SOURCE = Path(__file__).resolve().parent / "themes"


def locations():
    home = Path.home()
    return {
        "config": Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config"),
        "data": Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share"),
        "state": Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state") / "oled-gnome",
    }


def exists(path):
    return path.exists() or path.is_symlink()


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def settings_command(schema):
    if not shutil.which("gsettings"):
        raise RuntimeError("gsettings is required for --activate. Install without --activate to copy files only.")
    command = ["gsettings"]
    if schema == USER_THEME:
        search = [locations()["data"], Path.home() / ".local/share"]
        search += [Path(p) for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":") if p]
        for directory in search:
            schema_dir = directory / "gnome-shell/extensions" / UUID / "schemas"
            if (schema_dir / "gschemas.compiled").is_file():
                command += ["--schemadir", str(schema_dir)]
                break
    schemas = run(command + ["list-schemas"]).splitlines()
    if schema not in schemas:
        raise RuntimeError(f"Missing settings schema: {schema}. For Shell activation, install the User Themes extension first.")
    return command


def run(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Command failed: {command[0]}")
    return result.stdout.strip()


def activation_plan(component):
    changes = []
    if component in ("gtk", "all"):
        command = settings_command(INTERFACE)
        for key, value in [("gtk-theme", "'Adwaita-dark'"), ("color-scheme", "'prefer-dark'")]:
            old = run(command + ["get", INTERFACE, key])
            if run(command + ["writable", INTERFACE, key]) != "true":
                raise RuntimeError(f"Setting is locked: {INTERFACE} {key}")
            changes.append({"schema": INTERFACE, "key": key, "before": old, "after": value})
    if component in ("shell", "all"):
        if not shutil.which("gnome-extensions") or UUID not in run(["gnome-extensions", "list", "--enabled"]).splitlines():
            raise RuntimeError("Enable the GNOME Shell User Themes extension before using --activate, or install without --activate.")
        command = settings_command(USER_THEME)
        old = run(command + ["get", USER_THEME, "name"])
        if run(command + ["writable", USER_THEME, "name"]) != "true":
            raise RuntimeError("The Shell theme setting is locked.")
        changes.append({"schema": USER_THEME, "key": "name", "before": old, "after": f"'{THEME}'"})
    return changes


def plan(component):
    paths = locations()
    targets = []
    if component in ("gtk", "all"):
        for version in ("3.0", "4.0"):
            gtk = paths["config"] / f"gtk-{version}"
            targets += [(gtk / "oled-gnome", SOURCE / f"gtk-{version}", None),
                        (gtk / "gtk.css", None, '@import url("oled-gnome/theme.css");\n')]
    if component in ("shell", "all"):
        targets.append((paths["data"] / "themes" / THEME, SOURCE / THEME, None))
    return targets


def restore_files(records, recovery=None):
    for index, record in enumerate(reversed(records)):
        target = Path(record["target"])
        if recovery is not None and exists(target):
            copy(target, recovery / str(index) / target.name)
        remove(target)
        if record["backup"]:
            copy(Path(record["backup"]), target)


def restore_settings(changes):
    warnings = []
    for change in reversed(changes):
        try:
            command = settings_command(change["schema"])
            current = run(command + ["get", change["schema"], change["key"]])
            if current == change["after"]:
                run(command + ["set", change["schema"], change["key"], change["before"]])
            else:
                print(f"Kept your newer setting: {change['schema']} {change['key']}")
        except RuntimeError as error:
            warnings.append(str(error))
    return warnings


def install(args):
    paths = locations()
    manifest = paths["state"] / "current.json"
    if manifest.exists():
        raise RuntimeError("An installation is already recorded. Run uninstall before installing again or changing components.")
    targets = plan(args.component)
    for target, source, _ in targets:
        if source is not None and not source.is_dir():
            raise RuntimeError(f"Missing bundled styles: {source}")
        if target.name == "gtk.css" and target.is_dir() and not target.is_symlink():
            raise RuntimeError(f"Expected a file, found a directory: {target}")
    changes = activation_plan(args.component) if args.activate else []
    for target, _, _ in targets:
        print(f"{'Replace (back up first)' if exists(target) else 'Create'}: {target}")
    for change in changes:
        print(f"Set: {change['schema']} {change['key']} = {change['after']}")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return
    paths["state"].mkdir(parents=True, exist_ok=True)
    os.chmod(paths["state"], 0o700)
    backups = paths["state"] / "backups"
    backups.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="install-", dir=backups))
    records = []
    try:
        for index, (target, _, _) in enumerate(targets):
            saved = backup / str(index) if exists(target) else None
            if saved is not None:
                copy(target, saved)
            records.append({"target": str(target), "backup": str(saved) if saved is not None else None})
    except BaseException:
        shutil.rmtree(backup)
        raise
    state = {"component": args.component, "files": records,
             "settings": changes, "status": "installing", "backup": str(backup)}
    write_json(manifest, state)
    try:
        for target, source, contents in targets:
            remove(target)  # Replace symlinks themselves, never write through them.
            if source is not None:
                copy(source, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding="utf-8")
        for change in changes:
            run(settings_command(change["schema"]) + ["set", change["schema"], change["key"], change["after"]])
        state["status"] = "installed"
        write_json(manifest, state)
    except BaseException:
        restore_files(records)
        warnings = restore_settings(changes)
        if warnings:
            print("Settings restore needs attention; install record retained:", *warnings, sep="\n", file=sys.stderr)
        else:
            manifest.unlink(missing_ok=True)
        raise
    print(f"Installed {args.component}. Backups: {backup}")
    print("Fully close and reopen GTK apps. For Shell, select OLED-GNOME in Tweaks if you did not use --activate.")


def uninstall(args):
    paths = locations()
    manifest = paths["state"] / "current.json"
    if not manifest.exists():
        raise RuntimeError("No recorded installation found for these HOME/XDG paths.")
    state = json.loads(manifest.read_text(encoding="utf-8"))
    expected = [str(item[0]) for item in plan(state["component"])]
    if [record["target"] for record in state["files"]] != expected:
        raise RuntimeError("Install paths differ from this environment. Use the same HOME and XDG variables as at installation.")
    for record in state["files"]:
        if record["backup"] and not exists(Path(record["backup"])):
            raise RuntimeError(f"Missing backup; nothing restored: {record['backup']}")
        print(f"Restore previous contents (or remove newly created path): {record['target']}")
    if args.dry_run:
        print("Dry run: nothing changed.")
        return
    recovery_root = paths["state"] / "recovered"
    recovery_root.mkdir(exist_ok=True)
    recovery = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=recovery_root))
    restore_files(state["files"], recovery)
    warnings = restore_settings(state["settings"])
    write_json(paths["state"] / "last-uninstall.json", {**state, "recovery": str(recovery), "warnings": warnings})
    manifest.unlink()
    print(f"Previous files restored. Your installed files and any edits were saved in: {recovery}")
    for warning in warnings:
        print(f"Settings warning: {warning}", file=sys.stderr)
    print("Reopen GTK apps. If you selected the Shell theme manually, select your previous theme in Tweaks.")


def main():
    parser = argparse.ArgumentParser(description="OLED GNOME: per-user installation with backups; never use sudo.")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("install", help="Back up current files, then install the requested components")
    add.add_argument("--component", choices=("all", "gtk", "shell"), default="all")
    add.add_argument("--activate", action="store_true", help="Also set dark GTK preferences and/or select the Shell theme; previous values are saved")
    add.add_argument("--dry-run", action="store_true")
    undo = sub.add_parser("uninstall", help="Restore files and settings recorded by install")
    undo.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        parser.error("Run as your regular desktop user, without sudo.")
    try:
        (install if args.action == "install" else uninstall)(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
