"""用户级开机自启配置（Windows、macOS 与 Linux 桌面）。"""

import sys
from pathlib import Path
from typing import List


APP_ID = "DeskGuardian"


def _launch_arguments() -> List[str]:
    """返回启动 Web 守护所需参数，不依赖当前工作目录。"""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), "web", "--no-browser"]
    python = Path(sys.executable).resolve()
    if sys.platform == "win32":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    main_file = Path(__file__).resolve().with_name("main.py")
    return [str(python), str(main_file), "web", "--no-browser"]


def _windows_command(arguments: List[str]) -> str:
    return " ".join('"%s"' % item.replace('"', '\\"') for item in arguments)


def _linux_desktop_path() -> Path:
    return Path.home() / ".config" / "autostart" / "deskguardian.desktop"


def _mac_plist_path() -> Path:
    # A stable project-local identifier that does not claim ownership of a
    # maintainer's domain. It can be migrated once a permanent project URL is set.
    return Path.home() / "Library" / "LaunchAgents" / "DeskGuardian.plist"


def is_autostart_enabled() -> bool:
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                value, _ = winreg.QueryValueEx(key, APP_ID)
            return bool(value)
        except (FileNotFoundError, OSError):
            return False
    if sys.platform == "darwin":
        return _mac_plist_path().is_file()
    return _linux_desktop_path().is_file()


def configure_autostart(enabled: bool) -> bool:
    """创建或删除当前用户的启动项；不要求管理员权限。"""
    enabled = bool(enabled)
    arguments = _launch_arguments()
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    APP_ID,
                    0,
                    winreg.REG_SZ,
                    _windows_command(arguments),
                )
            else:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass
        return is_autostart_enabled()

    if sys.platform == "darwin":
        target = _mac_plist_path()
        if enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            escaped = "".join(
                "<string>%s</string>"
                % (item.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
                for item in arguments
            )
            target.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>'
                "<key>Label</key><string>DeskGuardian</string>"
                "<key>ProgramArguments</key><array>%s</array>"
                "<key>RunAtLoad</key><true/>"
                "</dict></plist>\n" % escaped,
                encoding="utf-8",
            )
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        return is_autostart_enabled()

    target = _linux_desktop_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Desktop Entry 的 Exec 字段不是 shell 命令；按 freedesktop 规则
        # 使用双引号保护每个参数，而不是使用 shell 的单引号语法。
        command = " ".join(
            '"%s"' % item.replace("\\", "\\\\").replace('"', '\\"')
            for item in arguments
        )
        target.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=DeskGuardian\n"
            "Comment=本地课业提醒守护助手\n"
            "Exec=%s\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n" % command,
            encoding="utf-8",
        )
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    return is_autostart_enabled()


__all__ = ["configure_autostart", "is_autostart_enabled"]
