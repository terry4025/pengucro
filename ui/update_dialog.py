"""Update prompt UI.

The dialog deliberately knows nothing about networking or file replacement.
It renders update state supplied by :class:`ui.main_window.MainWindow` and
reports a small set of user intents through ``on_action``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import customtkinter as ctk

import ui.theme as theme


UpdateAction = Callable[[str], Any]


class UpdateDialog(ctk.CTkToplevel):
    """Modal update details and confirmation dialog."""

    WIDTH = 440
    HEIGHT = 430

    _STATE_COPY = {
        "available": (
            theme.ACCENT_BLUE,
            "업데이트 준비됨",
            "새 버전을 내려받아 설치할 수 있습니다.",
            "업데이트 받기",
            "download",
        ),
        "downloading": (
            theme.ACCENT_BLUE,
            "업데이트 다운로드 중",
            "파일을 안전하게 확인하고 있습니다. 창을 닫아도 다운로드는 계속됩니다.",
            "다운로드 중",
            "",
        ),
        "ready": (
            theme.ACCENT_GREEN,
            "업데이트 준비 완료",
            "재시작하면 현재 실행 파일에 업데이트를 적용합니다.",
            "재시작 및 적용",
            "restart",
        ),
        "deferred": (
            theme.ACCENT_YELLOW,
            "예약 종료 후 업데이트",
            "진행 중인 예약을 방해하지 않도록 종료 후 재시작할 수 있습니다.",
            "예약 종료 후 적용",
            "",
        ),
        "error": (
            theme.TINT_ERROR_FG,
            "업데이트 실패",
            "업데이트를 완료하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.",
            "다시 시도",
            "retry",
        ),
    }

    def __init__(
        self,
        parent,
        *,
        on_action: UpdateAction | None = None,
        state: str = "available",
        version: str = "",
        notes: Iterable[str] = (),
        size_bytes: int | None = None,
        progress: float | int | None = None,
        message: str = "",
    ):
        super().__init__(parent)
        self.parent = parent
        self._on_action = on_action
        self._action = ""

        self.title("프로그램 업데이트")
        self.resizable(False, False)
        self.configure(fg_color=theme.CANVAS_COLOR)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)

        parent.update_idletasks()
        physical_width = parent._to_physical(self.WIDTH)
        physical_height = parent._to_physical(self.HEIGHT)
        x = parent.winfo_x() + max(0, (parent.winfo_width() - physical_width) // 2)
        y = parent.winfo_y() + max(0, (parent.winfo_height() - physical_height) // 2)
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(
            fill="both",
            expand=True,
            padx=theme.SPACE_5,
            pady=theme.SPACE_4,
        )

        title_row = ctk.CTkFrame(content, fg_color="transparent")
        title_row.pack(fill="x")
        self.status_dot = ctk.CTkFrame(
            title_row,
            width=8,
            height=8,
            fg_color=theme.ACCENT_BLUE,
            corner_radius=4,
        )
        self.status_dot.pack(side="left", padx=(0, theme.SPACE_2))
        self.status_dot.pack_propagate(False)
        self.title_label = ctk.CTkLabel(
            title_row,
            text="업데이트 준비됨",
            font=theme.FONT_DISPLAY,
            text_color=theme.TEXT_PRIMARY,
            anchor="w",
        )
        self.title_label.pack(side="left", fill="x", expand=True)

        self.version_label = ctk.CTkLabel(
            content,
            text="",
            font=theme.FONT_HEADING,
            text_color=theme.TINT_INFO_FG,
            anchor="w",
        )
        self.version_label.pack(fill="x", pady=(theme.SPACE_3, theme.SPACE_1))

        self.message_label = ctk.CTkLabel(
            content,
            text="",
            font=theme.FONT_BODY_MD,
            text_color=theme.TEXT_BODY,
            justify="left",
            anchor="w",
            wraplength=390,
        )
        self.message_label.pack(fill="x", pady=(0, theme.SPACE_3))

        self.notes_card = ctk.CTkFrame(
            content,
            fg_color=theme.SURFACE_COLOR,
            border_color=theme.HAIRLINE_COLOR,
            border_width=1,
            corner_radius=theme.ROUNDED_LG,
        )
        self.notes_card.pack(fill="both", expand=True)
        ctk.CTkLabel(
            self.notes_card,
            text="이번 업데이트",
            font=theme.FONT_HEADING,
            text_color=theme.TEXT_PRIMARY,
            anchor="w",
        ).pack(fill="x", padx=theme.SPACE_3, pady=(theme.SPACE_3, theme.SPACE_2))
        self.notes_label = ctk.CTkLabel(
            self.notes_card,
            text="",
            font=theme.FONT_BODY_MD,
            text_color=theme.TEXT_BODY,
            justify="left",
            anchor="nw",
            wraplength=360,
        )
        self.notes_label.pack(
            fill="both",
            expand=True,
            padx=theme.SPACE_3,
            pady=(0, theme.SPACE_3),
        )

        self.progress_bar = ctk.CTkProgressBar(
            content,
            height=4,
            corner_radius=2,
            fg_color=theme.ELEVATED_COLOR,
            progress_color=theme.ACCENT_BLUE,
        )
        self.progress_bar.set(0)

        self.footer = ctk.CTkFrame(content, fg_color="transparent")
        self.footer.pack(fill="x", pady=(theme.SPACE_3, 0))
        self.size_label = ctk.CTkLabel(
            self.footer,
            text="",
            font=theme.FONT_CAPTION,
            text_color=theme.TEXT_MUTE,
            anchor="w",
        )
        self.size_label.pack(side="left", fill="x", expand=True)

        self.close_button = ctk.CTkButton(
            self.footer,
            text="나중에",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.TEXT_BODY,
            fg_color=theme.CONTROL_COLOR,
            hover_color=theme.CONTROL_HOVER,
            border_width=1,
            border_color=theme.CONTROL_BORDER,
            corner_radius=theme.ROUNDED_MD,
            command=self._close,
            height=theme.H_BUTTON,
            width=82,
        )
        self.close_button.pack(side="right")
        self.action_button = ctk.CTkButton(
            self.footer,
            text="업데이트 받기",
            font=(theme.FONT_FAMILY, 12, "bold"),
            text_color=theme.TEXT_DARK,
            fg_color=theme.ACCENT_WHITE,
            hover_color=theme.TEXT_BODY,
            corner_radius=theme.ROUNDED_MD,
            command=self._run_action,
            height=theme.H_BUTTON,
            width=132,
        )
        self.action_button.pack(side="right", padx=(0, theme.SPACE_2))

        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Return>", lambda _event: self._run_action())
        self.update_state(
            state,
            version=version,
            notes=notes,
            size_bytes=size_bytes,
            progress=progress,
            message=message,
        )
        self.after(50, self._grab_focus)

    @staticmethod
    def _format_size(size_bytes: int | None) -> str:
        if not size_bytes or size_bytes < 0:
            return ""
        size = float(size_bytes)
        if size >= 1024 * 1024:
            return f"다운로드 크기 {size / (1024 * 1024):.1f} MB"
        if size >= 1024:
            return f"다운로드 크기 {size / 1024:.0f} KB"
        return f"다운로드 크기 {int(size)} B"

    @staticmethod
    def _normalise_notes(notes: Iterable[str] | str) -> tuple[str, ...]:
        if isinstance(notes, str):
            notes = (notes,)
        return tuple(str(note).strip() for note in notes if str(note).strip())

    def update_state(
        self,
        state: str,
        *,
        version: str = "",
        notes: Iterable[str] | str = (),
        size_bytes: int | None = None,
        progress: float | int | None = None,
        message: str = "",
    ) -> None:
        state = state if state in self._STATE_COPY else "available"
        color, title, default_message, action_text, action = self._STATE_COPY[state]
        self._action = action

        # Release metadata is numeric (for example ``6.04``), but accepting a
        # legacy ``v6.04`` value here keeps every update dialog consistent.
        version_text = str(version).strip().lstrip("vV")
        self.status_dot.configure(fg_color=color)
        self.title_label.configure(text=title)
        self.version_label.configure(
            text=f"새 버전 {version_text}" if version_text else "새 버전"
        )
        self.message_label.configure(text=str(message).strip() or default_message)

        note_items = self._normalise_notes(notes)
        self.notes_label.configure(
            text="\n".join(f"• {note}" for note in note_items)
            if note_items
            else "업데이트 세부 내역을 불러오는 중입니다."
        )
        self.size_label.configure(text=self._format_size(size_bytes))

        if state == "downloading":
            try:
                percent = max(0.0, min(100.0, float(progress or 0)))
            except (TypeError, ValueError):
                percent = 0.0
            self.progress_bar.set(percent / 100.0)
            if not self.progress_bar.winfo_manager():
                self.progress_bar.pack(
                    fill="x",
                    pady=(theme.SPACE_3, 0),
                    before=self.footer,
                )
            self.action_button.configure(
                text=f"다운로드 중 {percent:.0f}%",
                state="disabled",
            )
        else:
            self.progress_bar.pack_forget()
            self.action_button.configure(
                text=action_text,
                state="normal" if action else "disabled",
            )

    def _grab_focus(self) -> None:
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _run_action(self) -> None:
        if not self._action or not callable(self._on_action):
            return
        action = self._action
        try:
            self._on_action(action)
        except Exception as exc:
            self.update_state(
                "error",
                message=f"업데이트 작업을 시작하지 못했습니다: {exc}",
            )

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        if getattr(self.parent, "_update_dialog", None) is self:
            self.parent._update_dialog = None
        self.destroy()
