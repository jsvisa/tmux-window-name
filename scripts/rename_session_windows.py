#!/usr/bin/env python3

import subprocess
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple
from argparse import ArgumentParser
from contextlib import contextmanager

from path_utils import get_exclusive_paths, Pane


class CmdResult:
    """Simple result object to mimic libtmux's cmd result"""

    def __init__(self, stdout: List[str]):
        self.stdout = stdout


class Server:
    """Minimal tmux server wrapper that doesn't require libtmux"""

    def __init__(
        self, socket_name: Optional[str] = None, socket_path: Optional[str] = None
    ):
        self._socket_name = socket_name
        self._socket_path = socket_path

    def _build_base_cmd(self) -> List[str]:
        cmd = ["tmux"]
        if self._socket_path:
            cmd.extend(["-S", self._socket_path])
        elif self._socket_name:
            cmd.extend(["-L", self._socket_name])
        return cmd

    def cmd(self, *args: str) -> CmdResult:
        """Execute a tmux command and return the result"""
        cmd = self._build_base_cmd() + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            stdout = result.stdout.strip().split("\n") if result.stdout.strip() else []
            return CmdResult(stdout)
        except Exception:
            return CmdResult([])

    @property
    def windows(self) -> List[Any]:
        """Get list of windows (minimal implementation for post_restore)"""
        result = self.cmd("list-windows", "-a", "-F", "#{window_id}")
        windows = []
        for window_id in result.stdout:
            if window_id:
                windows.append(_Window(window_id, self))
        return windows


class _Window:
    """Minimal window object for compatibility"""

    def __init__(self, window_id: str, server: "Server"):
        self.window_id = window_id
        self._server = server


OPTIONS_PREFIX = "@tmux_window_name_"
HOOK_INDEX = 8921

HOME_DIR = os.path.expanduser("~")

# Cache for global options to avoid repeated queries
_global_options_cache = {}


def get_option(server: Server, option: str, default: Any) -> Any:
    out = server.cmd("show-option", "-gv", f"{OPTIONS_PREFIX}{option}").stdout
    if len(out) == 0:
        return default

    return eval(out[0])


def get_option_cached(server: Server, option: str, default: Any) -> Any:
    """Cached version of get_option for global options that don't change during execution"""
    cache_key = f"{OPTIONS_PREFIX}{option}"
    if cache_key not in _global_options_cache:
        _global_options_cache[cache_key] = get_option(server, option, default)
    return _global_options_cache[cache_key]


def set_option(server: Server, option: str, val: str):
    server.cmd("set-option", "-g", f"{OPTIONS_PREFIX}{option}", val)


def get_window_option(
    server: Server, window_id: Optional[str], option: str, default: Any
) -> Any:
    return get_window_tmux_option(
        server, window_id, f"{OPTIONS_PREFIX}{option}", default, do_eval=True
    )


def get_window_tmux_option(
    server: Server,
    window_id: Optional[str],
    option: str,
    default: Any,
    do_eval: bool = False,
) -> Any:
    arguments = ["show-option", "-wqv"]

    if window_id is not None:
        arguments.append("-t")
        arguments.append(window_id)

    arguments.append(option)
    out = server.cmd(*arguments).stdout

    if len(out) == 0:
        return default

    if do_eval:
        return eval(out[0])

    return out[0]


def set_window_tmux_option(
    server: Server, window_id: Optional[str], option: str, value: str
) -> Any:
    arguments = ["set-option", "-wq"]
    if window_id is not None:
        arguments.append("-t")
        arguments.append(window_id)

    arguments.append(option)
    arguments.append(value)

    server.cmd(*arguments)


def get_all_windows_option(server: Server, option: str, default: Any) -> dict:
    """
    Bulk fetch a window option for all windows in one tmux command.
    Returns a dict mapping window_id to option value.
    """
    out = server.cmd("list-windows", "-F", f"#{{window_id}}:#{{#{option}}}").stdout
    result = {}
    for line in out:
        if ":" in line:
            window_id, value = line.split(":", 1)
            if value:
                try:
                    result[window_id] = eval(value)
                except:
                    result[window_id] = default
            else:
                result[window_id] = default
    return result


def post_restore(server: Server):
    # Re enable tmux-window-name if `automatic-rename` is on
    for window in server.windows:
        if (
            get_window_tmux_option(server, window.window_id, "automatic-rename", "on")
            == "on"
        ):
            set_window_tmux_option(
                server, window.window_id, f"{OPTIONS_PREFIX}enabled", "1"
            )
        else:
            set_window_tmux_option(
                server, window.window_id, f"{OPTIONS_PREFIX}enabled", "0"
            )

    # Enable rename hook to enable tmux-window-name on later windows
    enable_user_rename_hook(server)


def enable_user_rename_hook(server: Server):
    """
    The hook:
        if window has name:
            set @tmux_window_name_enabled to 1
        else:
            set @tmux_window_name_enabled to 0

    @tmux_window_name_enabled (window option):
        Indicator if we should rename the window or not
    """
    current_file = Path(__file__).absolute()
    server.cmd(
        "set-hook",
        "-g",
        f"after-rename-window[{HOOK_INDEX}]",
        f"""
if-shell "[ #{{n:window_name}} -gt 0 ]"
    "set -w @tmux_window_name_enabled 0"
    "set -w @tmux_window_name_enabled 1;
run-shell "{current_file}"
""",
    )


def disable_user_rename_hook(server: Server):
    server.cmd("set-hook", "-ug", f"after-rename-window[{HOOK_INDEX}]")


@contextmanager
def tmux_guard(server: Server) -> Iterator[bool]:
    already_running = bool(get_option(server, "running", 0))

    try:
        if not already_running:
            set_option(server, "running", "1")
            # Must disable hook so our automated renames don't trigger it
            disable_user_rename_hook(server)

        yield already_running
    finally:
        if not already_running:
            enable_user_rename_hook(server)
            set_option(server, "running", "0")


@dataclass
class Options:
    shells: List[str] = field(default_factory=lambda: ["bash", "fish", "sh", "zsh"])
    dir_programs: List[str] = field(
        default_factory=lambda: ["nvim", "vim", "vi", "git", "codex", "claude"]
    )
    ignored_programs: List[str] = field(default_factory=lambda: [])
    max_name_len: int = 20
    use_tilde: bool = False
    substitute_sets: List[Tuple] = field(
        default_factory=lambda: [
            (".*python([0-9.]+)? (.*)/([^/].*)", r"\g<3>"),
            (".+ipython([0-9.]+)?", r"ipython\g<1>"),
            (r"^(/usr)?/bin/(.+)", r"\g<2>"),
            ("(bash) (.+)/(.+[ $])(.+)", r"\g<3>\g<4>"),
        ]
    )
    dir_substitute_sets: List[Tuple] = field(default_factory=lambda: [])
    program_map: dict = field(default_factory=lambda: {})

    @staticmethod
    def from_options(server: Server):
        fields = Options.__dataclass_fields__

        def default_field_value(f: field):
            if callable(f.default_factory):
                return f.default_factory()
            return f.default

        # Bulk fetch all options in one call for performance
        cache_key = "all_options_cached"
        if cache_key not in _global_options_cache:
            out = server.cmd("show-options", "-g").stdout
            options_dict = {}
            for line in out:
                # Format: @tmux_window_name_option_name value
                if line.startswith(OPTIONS_PREFIX):
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        key = parts[0].replace(OPTIONS_PREFIX, "")
                        try:
                            value = eval(parts[1])
                            if isinstance(value, str):
                                value = eval(value)
                            options_dict[key] = value
                        except:
                            pass
            _global_options_cache[cache_key] = options_dict

        options_dict = _global_options_cache[cache_key]

        fields_values = {
            field.name: options_dict.get(field.name, default_field_value(field))
            for field in fields.values()
        }

        return Options(**fields_values)


def parse_shell_command(shell_cmd: List[bytes]) -> Optional[str]:
    # Only shell
    if len(shell_cmd) == 1:
        return None

    shell_cmd_str = [x.decode() for x in shell_cmd]
    # Get base filename
    shell_cmd_str[1] = Path(shell_cmd_str[1]).name
    return " ".join(shell_cmd_str[1:])


def get_current_program(
    running_programs: List[bytes], pane: dict, options: Options
) -> Optional[str]:
    pane_pid = pane.get("pane_pid")
    if pane_pid is None:
        raise ValueError(f"Pane id is none, pane: {pane}")

    for program in running_programs:
        program = program.split()

        # if pid matches parse program
        if int(program[0]) == int(pane_pid):
            program = program[1:]
            program_name = program[0].decode()

            if (
                len(program) > 1
                and "scripts/rename_session_windows.py" in program[1].decode()
            ):
                continue

            if program_name in options.ignored_programs:
                continue

            # Ignore shells
            if program_name in options.shells:
                return parse_shell_command(program)

            return b" ".join(program).decode()

    return None


def get_program_if_dir(program_line: str, dir_programs: List[str]) -> Optional[str]:
    program = program_line.split()

    for p in dir_programs:
        if p == program[0]:
            return p

    return None


def get_session_active_panes(server: Server, session_id: str) -> List[dict]:
    """Get active panes for a session using direct tmux query (faster than libtmux)"""
    # Use format strings to get all needed pane info in one call
    out = server.cmd(
        "list-panes",
        "-s",
        "-t",
        session_id,
        "-F",
        "#{pane_id}:#{pane_active}:#{pane_pid}:#{pane_current_path}:#{window_id}",
    ).stdout

    panes = []
    for line in out:
        parts = line.split(":", 4)
        if len(parts) == 5 and parts[1] == "1":  # only active panes
            panes.append(
                {
                    "pane_id": parts[0],
                    "pane_pid": parts[2],
                    "pane_current_path": parts[3],
                    "window_id": parts[4],
                }
            )
    return panes


def rename_window(
    server: Server, window_id: str, window_name: str, max_name_len: int, use_tilde: bool
):
    if use_tilde:
        window_name = window_name.replace(HOME_DIR, "~")

    # use the basename of the process
    parts = window_name.split(" ", 1)
    process = parts[0].split("/")[-1]
    window_name = process
    if len(parts) > 1:
        window_name += " " + parts[1]
    window_name = window_name[:max_name_len]

    # Batch all three operations into a single tmux command for performance
    server.cmd(
        "rename-window",
        "-t",
        window_id,
        window_name,
        ";",
        "set-option",
        "-wq",
        "-t",
        window_id,
        "automatic-rename-format",
        window_name,
        ";",
        "set-option",
        "-wq",
        "-t",
        window_id,
        "automatic-rename",
        "on",
    )


def get_panes_programs(server: Server, session_id: str, options: Options):
    session_active_panes = get_session_active_panes(server, session_id)
    try:
        running_programs = subprocess.check_output(
            ["ps", "-a", "-oppid,command"]
        ).splitlines()[1:]
    # can occur if ps has empty output
    except subprocess.CalledProcessError:
        running_programs = []

    return [
        Pane(p, get_current_program(running_programs, p, options))
        for p in session_active_panes
    ]


def rename_windows(server: Server):
    with tmux_guard(server) as already_running:
        if already_running:
            return

        session_id = get_current_session(server)
        if not session_id:
            return
        options = Options.from_options(server)

        # Bulk fetch window enabled status for all windows (single tmux call)
        enabled_map = get_all_windows_option(server, f"{OPTIONS_PREFIX}enabled", 1)

        panes_programs = get_panes_programs(server, session_id, options)
        panes_with_programs = [p for p in panes_programs if p.program is not None]
        panes_with_dir = [p for p in panes_programs if p.program is None]

        for pane in panes_with_programs:
            enabled_in_window = enabled_map.get(pane.info["window_id"], 1)
            if not enabled_in_window:
                continue

            program_name = get_program_if_dir(str(pane.program), options.dir_programs)
            if program_name is not None:
                pane.program = program_name
                panes_with_dir.append(pane)
                continue

            pane.program = substitute_name(str(pane.program), options.substitute_sets)
            pane.program = options.program_map.get(pane.program, pane.program)
            rename_window(
                server,
                str(pane.info["window_id"]),
                pane.program,
                options.max_name_len,
                options.use_tilde,
            )

        exclusive_paths = get_exclusive_paths(panes_with_dir)

        for p, display_path in exclusive_paths:
            enabled_in_window = enabled_map.get(p.info["window_id"], 1)
            if not enabled_in_window:
                continue

            display_path = substitute_name(
                str(display_path), options.dir_substitute_sets
            )
            if p.program is not None:
                p.program = substitute_name(p.program, options.substitute_sets)
                p.program = options.program_map.get(p.program, p.program)
                display_path = f"{p.program}:{display_path}"

            rename_window(
                server,
                str(p.info["window_id"]),
                str(display_path),
                options.max_name_len,
                options.use_tilde,
            )


def get_current_session(server: Server) -> Optional[str]:
    """Get current session ID directly (returns string instead of Session object for performance)"""
    result = server.cmd("display-message", "-p", "#{session_id}").stdout
    if not result:
        return None
    return result[0]


def substitute_name(name: str, substitute_sets: List[Tuple]) -> str:
    for pattern, replacement in substitute_sets:
        name = re.sub(pattern, replacement, name)

    return name


def print_programs(server: Server):
    session_id = get_current_session(server)
    if not session_id:
        return
    options = Options.from_options(server)

    panes_programs = get_panes_programs(server, session_id, options)

    for pane in panes_programs:
        if pane.program:
            print(
                f"{pane.program} -> {substitute_name(pane.program, options.substitute_sets)}"
            )


def main():
    server = Server()

    parser = ArgumentParser("Renames tmux session windows")
    parser.add_argument(
        "--print_programs",
        action="store_true",
        help="Prints full name of the programs in the session",
    )
    parser.add_argument(
        "--enable_rename_hook",
        action="store_true",
        help="Enables rename hook, for internal use",
    )
    parser.add_argument(
        "--disable_rename_hook",
        action="store_true",
        help="Enables rename hook, for internal use",
    )
    parser.add_argument(
        "--post_restore",
        action="store_true",
        help="Restore tmux enabled option from automatic-rename, "
        "for internal use, enables rename hook too",
    )

    args = parser.parse_args()
    if args.print_programs:
        print_programs(server)
    elif args.enable_rename_hook:
        enable_user_rename_hook(server)
    elif args.disable_rename_hook:
        disable_user_rename_hook(server)
    elif args.post_restore:
        post_restore(server)
    else:
        rename_windows(server)


if __name__ == "__main__":
    main()
