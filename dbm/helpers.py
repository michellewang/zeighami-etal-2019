#!/usr/bin/env python
from __future__ import annotations

import os
import subprocess
import sys
import traceback

from contextlib import nullcontext
from functools import wraps
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Union, TextIO

import click
import pandas as pd

DEFAULT_VERBOSITY = 2
PREFIX_RUN = "[RUN] "
PREFIX_ERROR = "[ERROR] "
DONE_MESSAGE = "[SUCCESS]"

EXT_NIFTI = ".nii"
EXT_GZIP = ".gz"
EXT_MINC = ".mnc"
EXT_TRANSFORM = ".xfm"
EXT_TAR = ".tar"
SUFFIX_T1 = "T1w"
SEP_SUFFIX = "-"

DNAME_NIHPD = "nihpd_pipeline"

def add_suffix(
    path: Union[Path, str],
    suffix: str,
    sep: Union[str, None] = SEP_SUFFIX,
    ext: Union[str, None] = None,
) -> Path:

    path = Path(path)
    if sep is not None:
        if suffix.startswith(sep):
            suffix = suffix[len(sep) :]
    else:
        sep = ""

    if ext is not None:
        stem = str(path).removesuffix(ext)
    else:
        stem = path.stem
        ext = path.suffix

    return path.parent / f"{stem}{sep}{suffix}{ext}"


def load_list(fpath: Path | str, names=None) -> pd.DataFrame:
    return pd.read_csv(fpath, header=None, dtype=str, names=names)


def requires_program(func, program, program_name=None):

    if program_name is None:
        program_name = program

    @wraps(func)
    def _requires_program(*args, **kwargs):
        try:
            subprocess.check_output(['which', program])
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"This function requires {program_name}, "
                "which does not appear to be installed",
            )
        func(*args, **kwargs)
        
    return _requires_program


def require_minc(func):
    return requires_program(func, 'mincinfo', 'MINC tools')


def require_python2(func):
    return requires_program(func, 'python2', 'Python 2')


def check_nihpd_pipeline(dpath_pipeline):

    fpath_to_source: Path = dpath_pipeline / "init.sh"
    if not Path(fpath_to_source).exists():
        raise FileNotFoundError(fpath_to_source)
    
    pythonpath = os.environ.get('PYTHONPATH')
    if (pythonpath is None) or not DNAME_NIHPD in pythonpath:
        raise RuntimeError(
            "PYTHONPATH environment variable not set correctly. "
            f"Make sure to source {fpath_to_source} before running")

def process_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def add_options(options):
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func

    return _add_options


def add_helper_options():
    common_options = [
        click.option(
            "--logfile", "fpath_log", callback=callback_path, help="Path to log file"
        ),
        click.option(
            "--overwrite/--no-overwrite",
            default=False,
            help="Overwrite existing result files.",
        ),
        click.option(
            "--dry-run/--no-dry-run",
            default=False,
            help="Print shell commands without executing them.",
        ),
        click.option(
            "-v",
            "--verbose",
            "verbosity",
            count=True,
            default=DEFAULT_VERBOSITY,
            help="Set/increase verbosity level (cumulative). "
            f"Default level: {DEFAULT_VERBOSITY}.",
        ),
        click.option(
            "--quiet",
            is_flag=True,
            default=False,
            help="Suppress output whenever possible. "
            "Has priority over -v/--verbose flags.",
        ),
    ]
    return add_options(common_options)


def add_silent_option():
    return add_options([
        click.option(
            "--silence-commands/--no-silence-commands", "silent", 
            default=True, 
            help="Whether to silence intermediate shell command outputs")
    ])


def callback_path(ctx, param, value):
    if value is None:
        return None
    return process_path(value)


def with_helper(func):
    @wraps(func)
    def _with_helper(
        fpath_log: Path = None,
        verbosity: int = DEFAULT_VERBOSITY,
        quiet: bool = False,
        dry_run: bool = False,
        overwrite: bool = False,
        exit_on_error: bool = True,
        prefix_run: str = PREFIX_RUN,
        prefix_error: str = PREFIX_ERROR,
        **kwargs,
    ):

        with_log = fpath_log is not None
        if with_log:
            fpath_log.parent.mkdir(parents=True, exist_ok=True)

        with TemporaryDirectory() as dpath_tmp:
            with fpath_log.open("w") if with_log else nullcontext() as file_log:
                helper = ScriptHelper(
                    file_log=file_log,
                    verbosity=verbosity,
                    quiet=quiet,
                    dry_run=dry_run,
                    overwrite=overwrite,
                    prefix_run=prefix_run,
                    prefix_error=prefix_error,
                    dpath_tmp=Path(dpath_tmp),
                )
                try:
                    helper.timestamp()
                    helper.print_separation()

                    func(helper=helper, **kwargs)

                    for callback in helper.callbacks_success:
                        callback()

                    helper.done()

                except Exception as ex:

                    for callback in helper.callback_failure:
                        callback()

                    helper.print_error(traceback.format_exc(), exit=exit_on_error)
                    raise ex

                finally:

                    for callback in helper.callbacks_always:
                        callback()

                    helper.print_separation()
                    helper.timestamp()

    return _with_helper


class ScriptHelper:
    def __init__(
        self,
        file_log: Union[None, TextIO] = None,
        verbosity=2,
        quiet=False,
        dry_run=False,
        overwrite=False,
        dpath_tmp=None,
        prefix_run=PREFIX_RUN,
        prefix_error=PREFIX_ERROR,
        done_message=DONE_MESSAGE,
        callbacks_always=None,
        callbacks_success=None,
        callbacks_failure=None,
    ) -> None:

        # quiet overrides verbosity
        if quiet:
            verbosity = 0

        if callbacks_always is None:
            callbacks_always = []
        if callbacks_success is None:
            callbacks_success = []
        if callbacks_failure is None:
            callbacks_failure = []

        self.file_log = file_log
        self.verbosity = verbosity
        self.quiet = quiet
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.dpath_tmp: Path = dpath_tmp
        self.prefix_run = prefix_run
        self.prefix_error = prefix_error
        self.done_message = done_message
        self.callbacks_always: list = callbacks_always
        self.callbacks_success: list = callbacks_success
        self.callback_failure: list = callbacks_failure

    def verbose(self, threshold=0):
        return self.verbosity > threshold

    @property
    def verbose(self):
        return self.verbosity > 0

    def echo(
        self,
        message="",
        prefix="",
        text_color=None,
        color_prefix_only=False,
        force_color=True,
    ):
        """
        Print a message and newline to stdout or a file, similar to click.echo()
        but with some color processing.

        Parameters
        ----------
        message : str
            Message to print
        prefix : str, optional
            Prefix to prepend to message, by default ''
        text_color : str or None, optional
            Color name, by default None
        color_prefix_only : bool, optional
            Whether to only color the prefix instead of the entire text, by default False
        """

        # format text to print
        if (prefix != "") and (color_prefix_only):
            text = f"{click.style(prefix, fg=text_color)}{message}"
        else:
            text = click.style(f"{prefix}{message}", fg=text_color)

        click.echo(text, color=force_color, file=self.file_log)

    def print_separation(self, symbol="-", length=20):
        self.echo(symbol * length)

    def print_info(self, message="", text_color=None):
        if self.verbose:
            self.echo(message=message, text_color=text_color)

    def print_outcome(self, message="", text_color="blue"):
        self.echo(message=message, text_color=text_color)

    def print_error(self, message, text_color="red", exit_code=1, exit=True):
        """Print a message and exit the program.

        Parameters
        ----------
        message : str
            Error message
        text_color : str, optional
            Color name, by default 'red'
        exit_code : int, optional
            Program return code, by default 1
        """
        self.echo(message, prefix=self.prefix_error, text_color=text_color)
        if exit:
            sys.exit(exit_code)

    def run_command(
        self,
        args: list[str],
        shell=False,
        stdout=None,
        stderr=None,
        silent=False,
        force=False,
    ):
        """Run a shell command.

        Parameters
        ----------
        args : list[str]
            Command to pass to subprocess.run()
        shell : bool, optional
            Whether to execute command through the shell, by default False
        stdout : file object, int, or None, optional
            Standard output for executed program, by default None
        stderr : file object, int, or None, optional
            Standard error for execute program, by default None
        silent : bool, optional
            Whether to execute the command without printing the command or the output
        force : bool, optional
            Execute the command even if self.dry_run is True
        """
        args = [str(arg) for arg in args if arg != ""]
        args_str = " ".join(args)
        if not silent and ((self.verbosity > 0) or self.dry_run):
            self.echo(
                f"{args_str}",
                prefix=PREFIX_RUN,
                text_color="yellow",
                color_prefix_only=self.dry_run,
            )
        if force or (not self.dry_run):
            if stdout is None:
                if silent or self.verbosity < 2:
                    stdout = subprocess.DEVNULL
                    stderr = subprocess.DEVNULL
                else:
                    stdout = self.file_log
            if stderr is None:
                stderr = self.file_log
            try:
                subprocess.run(
                    args, check=True, shell=shell, stdout=stdout, stderr=stderr
                )
            except subprocess.CalledProcessError as ex:
                raise RuntimeError(
                    f"\nCommand {args_str} returned {ex.returncode}",
                )

    def timestamp(self):
        """Print the current time."""
        self.run_command(["date"], force=True)

    def done(self):
        self.echo(self.done_message, text_color="green")

    def mkdir(self, path: Union[str, Path], parents=True, exist_ok=None):
        if exist_ok is None:
            exist_ok = self.overwrite
        if not self.dry_run:
            Path(path).mkdir(parents=parents, exist_ok=exist_ok)

    def check_dir(self, dpath: Path, prefix=None):
        if dpath.exists() and (not self.overwrite):

            # get all files in directory
            files_in_dir = [p for p in dpath.rglob("*") if p.is_file()]
            # optionally keep only those with a specific prefix
            if prefix is not None:
                files_in_dir = [p for p in files_in_dir if p.name.startswith(prefix)]

            if len(files_in_dir) != 0:
                raise FileExistsError(
                    f"Directory {dpath} exists and/or contains expected result files. "
                    "Use --overwrite to overwrite."
                )

        return dpath

    def check_file(self, fpath: Path):
        if fpath.exists() and not self.overwrite:
            raise FileExistsError(f"File {fpath} exists. Use --overwrite to overwrite.")