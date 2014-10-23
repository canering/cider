# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals
from . import _osx as osx
from . import _tty as tty
from ._sh import Brew, Defaults, spawn, collapseuser, commonpath, curl, mkdir_p
from rfc3987 import parse as urlparse
from tempfile import mkdtemp
import copy
import errno
import glob
import json
import os
import platform
import re
import subprocess
import sys

JSONDecodeError = ValueError

_DEFAULTS_TRUE_RE = re.compile(r"\bY(ES)?\b", re.I)
_DEFAULTS_FALSE_RE = re.compile(r"\bN(O)?\b", re.I)


class CiderException(Exception):
    def __init__(self, message, exit_code=None):
        if exit_code is None:
            exit_code = 1
        Exception.__init__(self, message)
        self.exit_code = exit_code


class JSONError(CiderException):
    def __init__(self, message, filepath, exit_code=None):
        CiderException.__init__(self, message, exit_code)
        self.filepath = filepath


class UnsupportedOSError(CiderException):
    def __init__(self, message, macos_version, exit_code=None):
        CiderException.__init__(self, message, exit_code)
        self.macos_version = macos_version


class XcodeMissingError(CiderException):
    def __init__(self, message, url, exit_code=None):
        CiderException.__init__(self, message, exit_code)
        self.url = url


class BrewMissingError(CiderException):
    def __init__(self, message, url, exit_code=None):
        CiderException.__init__(self, message, exit_code)
        self.url = url


class BootstrapMissingError(CiderException):
    def __init__(self, message, path, exit_code=None):
        CiderException.__init__(self, message, exit_code)
        self.path = path


class SymlinkError(CiderException):
    pass


class AppMissingError(CiderException):
    pass


class Cider(object):
    def __init__(self, cask=None, debug=None, verbose=None, cider_dir=None):
        self.cask = cask if cask is not None else False
        self.debug = debug if debug is not None else False
        self.verbose = verbose if verbose is not None else False
        self.brew = Brew(cask, debug, verbose)
        self.defaults = Defaults(debug)
        self.cider_dir = cider_dir if cider_dir is not None else os.path.join(
            os.path.expanduser("~"),
            ".cider"
        )

    @property
    def symlink_dir(self):
        return os.path.join(self.cider_dir, "symlinks")

    @property
    def bootstrap_file(self):
        return os.path.join(self.cider_dir, "bootstrap.json")

    @property
    def defaults_file(self):
        return os.path.join(self.cider_dir, "defaults.json")

    @property
    def cache_dir(self):
        return os.path.join(self.cider_dir, ".cache")

    @property
    def symlink_targets_file(self):
        return os.path.join(self.cache_dir, "symlink_targets.json")

    def read_bootstrap(self):
        try:
            return _read_json(self.bootstrap_file)
        except IOError as e:
            if e.errno == errno.ENOENT:
                raise BootstrapMissingError(
                    "Bootstrap file not found. Expected at {0}".format(
                        collapseuser(self.bootstrap_file)
                    ),
                    self.bootstrap_file
                )

            raise e

    def read_defaults(self):
        return _read_json(self.defaults_file, {})

    def _modify_bootstrap(self, key, transform=None):
        if transform is None:
            transform = lambda x: x

        def outer_transform(bootstrap):
            bootstrap[key] = sorted(transform(bootstrap.get(key, [])))
            return bootstrap

        return _modify_json(self.bootstrap_file, outer_transform)

    def _modify_defaults(self, domain, transform):
        def outer_transform(defaults):
            defaults[domain] = transform(defaults.get(domain, {}))
            return defaults

        return _modify_json(self.defaults_file, outer_transform)

    def _remove_dead_targets(self, targets):
        for target in targets:
            if os.path.islink(target) and os.path.samefile(
                self.cider_dir,
                commonpath([
                    self.cider_dir,
                    os.path.realpath(target)
                ]),
            ):
                os.remove(target)
                print(tty.progress("Removed dead symlink: {0}".format(
                    collapseuser(target))
                ))

    def restore(self):
        macos_version = platform.mac_ver()[0]

        if int(macos_version.split(".")[1]) < 9:
            raise UnsupportedOSError(
                "Unsupported OS version; please upgrade to 10.9 or later " +
                "and try again.",
                macos_version
            )
        elif not os.path.isdir("/Applications/Xcode.app"):
            raise XcodeMissingError(
                "Xcode not installed",
                "https://itunes.apple.com/us/app/xcode/id497799835?mt=12"
            )
        elif spawn(["which", "brew"], check_call=False, debug=self.debug,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE):
            raise BrewMissingError(
                "Homebrew not installed",
                "http://brew.sh/#install"
            )

        bootstrap = self.read_bootstrap()
        casks = bootstrap.get("casks", [])
        formulas = bootstrap.get("formulas", [])
        dependencies = bootstrap.get("dependencies", {})

        for script in bootstrap.get("before-scripts", []):
            spawn([script], shell=True, debug=self.debug, cwd=self.cider_dir)

        for tap in bootstrap.get("taps", []):
            self.brew.tap(tap)

        for formula in formulas:
            if formula in dependencies:
                deps = dependencies[formula]
                deps = deps if isinstance(deps, list) else [deps]

                # Currently only cask dependencies are supported.
                deps = (dep for dep in deps if dep.startswith("cask/"))

                for dep in (deps):
                    cask = dep.split("/")[1]
                    Brew(cask=True).safe_install(cask)
                    del casks[casks.index(cask)]

            self.brew.safe_install(formula)

        for cask in casks:
            Brew(cask=True).safe_install(cask)

        self.relink()
        self.apply_defaults()
        self.apply_icons()

        for script in bootstrap.get("after-scripts", []):
            spawn([script], shell=True, debug=self.debug, cwd=self.cider_dir)

    def install(self, *formulas, **kwargs):
        # Avoid pylint scoping warning W0640
        def transform(formula):
            return lambda x: x + [formula] if formula not in x else x

        formulas = list(formulas) or []
        force = kwargs.get("force", False)

        self.brew.install(*formulas, force=force)
        for formula in formulas:
            if self._modify_bootstrap(
                "casks" if self.cask else "formulas",
                transform=transform(formula)
            ):
                tty.puts("Added {0} to bootstrap".format(formula))
            else:
                tty.puterr("{0} already bootstrapped; skipping install".format(
                    formula
                ), warning=True)

    def rm(self, *formulas):
        def transform(formula):
            return lambda xs: [x for x in xs if x != formula]

        formulas = list(formulas) or []
        self.brew.rm(*formulas)

        for formula in formulas:
            if self._modify_bootstrap(
                "casks" if self.cask else "formulas",
                transform=transform(formula)
            ):
                tty.puts("Removed {0} from bootstrap".format(formula))
            else:
                tty.puterr("{0} not found in bootstrap".format(formula))

    def tap(self, tap):
        self.brew.tap(tap)
        if tap is not None:
            if self._modify_bootstrap(
                "taps",
                transform=lambda x: x + [tap] if tap not in x else x
            ):
                tty.puts("Added {0} tap to bootstrap".format(tap))
            else:
                tty.puterr("{0} tap already bootstrapped".format(tap))

    def untap(self, tap):
        self.brew.untap(tap)
        if self._modify_bootstrap(
            "taps",
            transform=lambda xs: [x for x in xs if x != tap]
        ):
            tty.puts("Removed {0} tap from bootstrap".format(tap))
        else:
            tty.puterr("{0} tap not found in bootstrapped".format(tap))

    def relink(self, force=None):
        force = force if force is not None else False
        symlinks = self.read_bootstrap().get("symlinks", {})
        previous_targets = _read_json(self.symlink_targets_file, [])
        new_targets = []

        for source_glob, target in symlinks.items():
            mkdir_p(os.path.dirname(os.path.expanduser(target)))
            sources = glob.iglob(os.path.join(self.symlink_dir, source_glob))
            for source in sources:
                source = os.path.join(self.cider_dir, source)
                source_target = os.path.expanduser(target)
                if target.endswith(os.path.sep) or target == "~":
                    source_target = os.path.join(
                        source_target,
                        os.path.basename(source)
                    )

                _make_symlink(source, source_target, self.debug, force)
                new_targets.append(source_target)

        self._remove_dead_targets(set(previous_targets) - set(new_targets))
        mkdir_p(os.path.dirname(self.symlink_targets_file))
        _write_json(self.symlink_targets_file, sorted(new_targets))

    def installed(self):
        bootstrap = self.read_bootstrap()
        key = "casks" if self.cask else "formulas"
        return bootstrap.get(key, [])

    def missing(self):
        formulas = [item.split()[0].strip() for item in self.installed()]
        brewed = self.brew.ls()

        def brew_orphan(formula):
            uses = self.brew.uses(formula)
            return len(set(formulas).intersection(set(uses))) == 0

        return sorted(filter(brew_orphan, set(brewed).difference(formulas)))

    def ls(self, formula):
        formulas = self.installed()
        if formula:
            formulas = (x for x in formulas if x.startswith(formula))
        if formulas:
            print("\n".join(formulas))
        else:
            tty.puterr("nothing to list", prefix="Error")

    def list_missing(self):
        missing_items = self.missing()
        if missing_items:
            suffix = "s" if len(missing_items) != 1 else ""
            fmt = "{0} missing formula{1} (tip: try `brew uses --installed` " + \
                  "to see what's using it)"
            tty.puterr(fmt.format(len(missing_items), suffix), warning=True)

            print("\n".join(missing_items) + "\n")
            sys.stdout.write("Add missing items to bootstrap? [y/N] ")

            if sys.stdin.read(1).lower() == "y":
                for formula in missing_items:
                    self.install(formula)
        else:
            print("Everything up to date.")

        return missing_items

    def set_default(self, domain, key, value, force=None):
        if isinstance(value, str):
            try:
                json_value = json.loads(_DEFAULTS_FALSE_RE.sub(
                    "false",
                    _DEFAULTS_TRUE_RE.sub("true", str(value))
                ))
            except ValueError:
                json_value = str(value)
        else:
            json_value = value

        self.defaults.write(domain, key, json_value, force)

        def transform(defaults):
            defaults[key] = json_value
            return defaults

        if self._modify_defaults(domain, transform):
            tty.puts("Updated defaults")

    def remove_default(self, domain, key):
        self.defaults.delete(domain, key)

        def transform(defaults):
            defaults.pop(key, None)
            return defaults

        if self._modify_defaults(domain, transform):
            tty.puts("Updated defaults")

    def apply_defaults(self):
        defaults = self.read_defaults()
        for domain in defaults:
            options = defaults[domain]
            for key, value in options.items():
                self.defaults.write(domain, key, value)

        tty.puts("Applied defaults")

    def run_scripts(self):
        bootstrap = self.read_bootstrap()
        scripts = bootstrap.get("before-scripts", []) + \
            bootstrap.get("after-scripts", [])
        for script in scripts:
            spawn([script], shell=True, debug=self.debug, cwd=self.cider_dir)

    def set_icon(self, app, icon):
        def transform(bootstrap):
            icons = bootstrap.get("icons", {})
            icons[app] = icon
            return bootstrap

        _modify_json(self.bootstrap_file, transform)
        _apply_icon(app, icon)

    def remove_icon(self, app):
        def transform(bootstrap):
            icons = bootstrap.get("icons", {})
            del icons[app]
            return bootstrap

        app_path = osx.path_for_app(app)
        if not app_path:
            raise AppMissingError("Application not found: '{0}'".format(app))

        _modify_json(self.bootstrap_file, transform)
        osx.remove_icon(app_path)

    def apply_icons(self):
        bootstrap = _read_json(self.bootstrap_file)
        icons = bootstrap.get("icons", {})
        for app, icon in icons.items():
            _apply_icon(app, icon)

        tty.puts("Applied icons")


def _read_json(path, fallback=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except IOError as e:
        if fallback is not None and e.errno == errno.ENOENT:
            return fallback

        raise e
    except JSONDecodeError as e:
        raise JSONError(e, path)


def _modify_json(path, transform):
    try:
        f = open(path, "r+")
        contents = json.load(f)
    except IOError as e:
        if e.errno == errno.ENOENT:
            f = open(path, "w")
            contents = {}
        else:
            raise e
    except JSONDecodeError as e:
        raise JSONError(e, path)

    old_contents = contents
    contents = transform(copy.deepcopy(contents))
    changed = bool(old_contents != contents)

    if changed:
        f.seek(0)
        f.write(json.dumps(
            contents,
            indent=4,
            sort_keys=True,
            separators=(',', ': ')
        ))
        f.truncate()

    f.close()
    return changed


def _write_json(path, contents):
    with open(path, "w") as f:
        f.write(json.dumps(
            contents,
            indent=4,
            sort_keys=True,
            separators=(',', ': ')
        ))


def _make_symlink(source, target, debug=None, force=None):
    linked = False

    if not os.path.exists(source):
        raise SymlinkError(
            "symlink source \"{0}\" does not exist".format(
                collapseuser(source)
            )
        )

    try:
        os.symlink(source, target)
        linked = True
        tty.puts("symlinked {0} -> {1}".format(
            tty.color(collapseuser(target), tty.MAGENTA),
            collapseuser(source)
        ))
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

        if os.path.islink(target):
            if os.path.samefile(
                os.path.realpath(target),
                os.path.realpath(source)
            ):
                linked = True
                tty.putdebug("Already linked: {0} -> {1}".format(
                    tty.color(collapseuser(target), tty.MAGENTA),
                    collapseuser(source)
                ), debug)
            else:
                fmt = "Linked to wrong target: {0} -> {1} (instead of {2})"
                tty.puterr(fmt.format(
                    tty.color(target, tty.MAGENTA),
                    os.path.realpath(collapseuser(target)),
                    os.path.realpath(collapseuser(source))
                ), warning=force)
        else:
            tty.puterr("{0} symlink target already exists at: {1}".format(
                collapseuser(source),
                collapseuser(target)
            ), warning=force)

    if not linked and force:
        try:
            osx.move_to_trash(target)
            print(tty.progress("Moved {0} to trash").format(target))
        except OSError as e:
            tty.puterr("Error moving {0} to trash: {1}".format(target, str(e)))
            return False
        return _make_symlink(source, target, debug, force)

    return linked


def _apply_icon(app, icon):
    app_path = osx.path_for_app(app)
    if not app_path:
        raise AppMissingError("Application not found: '{0}'".format(app))

    try:
        components = urlparse(icon)
        tmpdir = mkdtemp()
        icon_path = os.path.join(tmpdir, os.path.basename(components["path"]))
        print(tty.progress("Downloading {0} icon: {1}".format(app, icon)))
        curl(icon, icon_path)
    except ValueError:
        icon_path = icon

    osx.set_icon(app_path, icon_path)
