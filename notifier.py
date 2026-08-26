"""Cross-platform desktop notifications for DeskGuardian.

``plyer`` is imported lazily so importing the rest of the application still
works in headless/test environments.  Delivery errors are never silently
ignored: :class:`NotificationError` is raised unless ``raise_on_error=False``
is explicitly requested.
"""

from __future__ import annotations

import sys
from typing import Any, Optional


class NotificationError(RuntimeError):
    """Raised when a desktop notification cannot be delivered."""


class DesktopNotifier:
    """Small, injectable wrapper around :mod:`plyer.notification`.

    Args:
        app_name: Name displayed by the operating system.
        timeout: Suggested display duration in seconds.
        beep: Also request an audible alert after posting the notification.
        raise_on_error: Raise :class:`NotificationError` on failure.  If false,
            :meth:`notify` returns ``False`` instead.
        backend: Optional test/custom backend exposing ``notify(**kwargs)``.
    """

    def __init__(
        self,
        app_name: str = "DeskGuardian",
        timeout: int = 10,
        beep: bool = False,
        raise_on_error: bool = True,
        backend: Optional[Any] = None,
    ) -> None:
        self.app_name = str(app_name or "DeskGuardian")
        self.timeout = int(timeout)
        self.beep = bool(beep)
        self.raise_on_error = bool(raise_on_error)
        self._backend = backend
        self.last_error: Optional[NotificationError] = None

    def _get_backend(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            from plyer import notification as plyer_notification
        except (ImportError, OSError) as exc:
            raise NotificationError(
                "无法加载桌面通知组件 plyer；请先安装 plyer，或检查当前桌面环境"
            ) from exc
        self._backend = plyer_notification
        return self._backend

    @staticmethod
    def _beep() -> None:
        """Emit a best-effort alert sound without another dependency."""
        if sys.platform == "win32":
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return
            except (ImportError, RuntimeError, OSError):
                pass
        # ASCII bell is a portable fallback.  Some headless terminals simply
        # ignore it, which is preferable to making a delivered popup fail.
        try:
            sys.stderr.write("\a")
            sys.stderr.flush()
        except (AttributeError, OSError):
            pass

    def notify(
        self,
        title: str,
        message: str,
        *,
        beep: Optional[bool] = None,
        timeout: Optional[int] = None,
    ) -> bool:
        """Post one desktop notification.

        Returns ``True`` when the backend accepted the notification.  With the
        default ``raise_on_error=True``, invalid input/backend errors raise a
        clear :class:`NotificationError`; otherwise they return ``False`` and
        are available as :attr:`last_error`.
        """
        try:
            title = str(title).strip()
            message = str(message).strip()
            if not title:
                raise ValueError("通知标题不能为空")
            if not message:
                raise ValueError("通知内容不能为空")

            backend = self._get_backend()
            backend.notify(
                title=title,
                message=message,
                app_name=self.app_name,
                timeout=self.timeout if timeout is None else int(timeout),
            )
            if self.beep if beep is None else bool(beep):
                self._beep()
            self.last_error = None
            return True
        except NotificationError as exc:
            error = exc
        except Exception as exc:
            error = NotificationError("发送桌面通知失败: %s" % exc)

        self.last_error = error
        if self.raise_on_error:
            raise error
        return False

    __call__ = notify


# Backward-friendly short name used by CLI/web integrations.
Notifier = DesktopNotifier


def notify(
    title: str,
    message: str,
    *,
    beep: bool = False,
    timeout: int = 10,
    raise_on_error: bool = True,
) -> bool:
    """Convenience function for sending a single notification."""
    return DesktopNotifier(
        timeout=timeout, beep=beep, raise_on_error=raise_on_error
    ).notify(title, message)


send_notification = notify


__all__ = [
    "DesktopNotifier",
    "Notifier",
    "NotificationError",
    "notify",
    "send_notification",
]
