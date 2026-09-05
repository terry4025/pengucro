from __future__ import annotations

import argparse
import os
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .explorer import SiteInspector
from .models import InspectorConfig


DEFAULT_OUTPUT = Path.home() / "Documents" / "Pengucro Site Inspector"


class SiteInspectorApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("Pengucro Site Inspector · 원클릭 사이트 분석")
        self.geometry("920x720")
        self.minsize(820, 620)
        self.configure(fg_color="#111318")

        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.result = None
        self.events: queue.Queue[tuple] = queue.Queue()
        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="#191c23", corner_radius=0, height=68)
        header.pack(fill="x")
        header.pack_propagate(False)
        ctk.CTkLabel(
            header,
            text="Pengucro Site Inspector",
            font=ctk.CTkFont(size=21, weight="bold"),
            text_color="#ffffff",
        ).pack(anchor="w", padx=24, pady=(12, 0))
        ctk.CTkLabel(
            header,
            text="URL 하나로 화면·버튼·네트워크·API 후보를 자동 분석합니다.",
            font=ctk.CTkFont(size=12),
            text_color="#9aa3b2",
        ).pack(anchor="w", padx=24, pady=(1, 10))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(4, weight=1)

        card = ctk.CTkFrame(body, fg_color="#1a1d24", corner_radius=12)
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="분석 URL", text_color="#d8dce5").grid(
            row=0, column=0, padx=(18, 10), pady=(18, 10), sticky="w"
        )
        self.url_entry = ctk.CTkEntry(
            card,
            placeholder_text="https://camfit.co.kr/camp/...",
            height=38,
        )
        self.url_entry.grid(row=0, column=1, columnspan=5, padx=(0, 18), pady=(18, 10), sticky="ew")

        ctk.CTkLabel(card, text="방문 경로", text_color="#d8dce5").grid(
            row=1, column=0, padx=(18, 8), pady=8, sticky="w"
        )
        self.pages_entry = ctk.CTkEntry(card, width=70)
        self.pages_entry.insert(0, "12")
        self.pages_entry.grid(row=1, column=1, padx=(0, 18), pady=8, sticky="w")
        ctk.CTkLabel(card, text="페이지별 동작", text_color="#d8dce5").grid(
            row=1, column=2, padx=(0, 8), pady=8, sticky="w"
        )
        self.actions_entry = ctk.CTkEntry(card, width=70)
        self.actions_entry.insert(0, "6")
        self.actions_entry.grid(row=1, column=3, padx=(0, 18), pady=8, sticky="w")
        ctk.CTkLabel(card, text="탐색 깊이", text_color="#d8dce5").grid(
            row=1, column=4, padx=(0, 8), pady=8, sticky="w"
        )
        self.depth_entry = ctk.CTkEntry(card, width=70)
        self.depth_entry.insert(0, "3")
        self.depth_entry.grid(row=1, column=5, padx=(0, 18), pady=8, sticky="w")

        ctk.CTkLabel(card, text="미오픈 날짜", text_color="#d8dce5").grid(
            row=2, column=0, padx=(18, 10), pady=8, sticky="w"
        )
        self.date_offsets_entry = ctk.CTkEntry(card, height=36)
        self.date_offsets_entry.insert(0, "30,90,180")
        self.date_offsets_entry.grid(row=2, column=1, columnspan=3, padx=(0, 18), pady=8, sticky="ew")
        ctk.CTkLabel(card, text="최대 탐색", text_color="#d8dce5").grid(
            row=2, column=4, padx=(0, 8), pady=8, sticky="w"
        )
        self.date_probe_limit_entry = ctk.CTkEntry(card, width=70)
        self.date_probe_limit_entry.insert(0, "12")
        self.date_probe_limit_entry.grid(row=2, column=5, padx=(0, 18), pady=8, sticky="w")

        ctk.CTkLabel(card, text="결과 폴더", text_color="#d8dce5").grid(
            row=3, column=0, padx=(18, 10), pady=(8, 18), sticky="w"
        )
        self.output_entry = ctk.CTkEntry(card, height=36)
        self.output_entry.insert(0, str(DEFAULT_OUTPUT))
        self.output_entry.grid(row=3, column=1, columnspan=4, padx=(0, 10), pady=(8, 18), sticky="ew")
        ctk.CTkButton(
            card,
            text="찾아보기",
            width=90,
            command=self._choose_output,
            fg_color="#313746",
            hover_color="#3b4354",
        ).grid(row=3, column=5, padx=(0, 18), pady=(8, 18))

        warning = ctk.CTkLabel(
            body,
            text=(
                "안전 모드: 조회성 요청만 허용하며 예약·결제·취소·삭제 요청은 서버 전송 전에 차단합니다. "
                "CAPTCHA·로그인이 필요하면 열린 Chrome에서 직접 완료하세요."
            ),
            text_color="#f6c85f",
            font=ctk.CTkFont(size=12),
            wraplength=850,
            justify="left",
        )
        warning.grid(row=1, column=0, sticky="ew", pady=(12, 8))

        controls = ctk.CTkFrame(body, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", pady=(2, 8))
        self.start_button = ctk.CTkButton(
            controls,
            text="자동 분석 시작",
            width=150,
            height=40,
            command=self._start,
            fg_color="#2f77f1",
            hover_color="#2465cf",
        )
        self.start_button.pack(side="left")
        self.stop_button = ctk.CTkButton(
            controls,
            text="중지",
            width=90,
            height=40,
            command=self._stop,
            state="disabled",
            fg_color="#a84545",
            hover_color="#873636",
        )
        self.stop_button.pack(side="left", padx=8)
        self.open_button = ctk.CTkButton(
            controls,
            text="결과 폴더 열기",
            width=120,
            height=40,
            command=self._open_result,
            state="disabled",
            fg_color="#313746",
            hover_color="#3b4354",
        )
        self.open_button.pack(side="right")

        self.progress = ctk.CTkProgressBar(body, height=8)
        self.progress.set(0)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        self.log_text = ctk.CTkTextbox(
            body,
            fg_color="#15171d",
            text_color="#d6dae3",
            border_width=1,
            border_color="#2a2e38",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.log_text.grid(row=4, column=0, sticky="nsew")
        self._append_log("분석할 URL을 입력한 뒤 자동 분석 시작을 누르세요.", "info")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_entry.get() or str(DEFAULT_OUTPUT))
        if selected:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, selected)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            max_pages = int(self.pages_entry.get())
            date_offsets = tuple(
                int(value.strip())
                for value in self.date_offsets_entry.get().split(",")
                if value.strip()
            )
            config = InspectorConfig(
                start_url=self.url_entry.get().strip(),
                output_root=Path(self.output_entry.get().strip() or DEFAULT_OUTPUT),
                max_pages=max_pages,
                max_states=min(300, max(36, max_pages * 3)),
                max_actions_per_page=int(self.actions_entry.get()),
                max_depth=int(self.depth_entry.get()),
                date_probe_offsets_days=date_offsets,
                max_date_probes=int(self.date_probe_limit_entry.get()),
            ).validated()
        except (ValueError, OSError) as exc:
            messagebox.showerror("설정 확인", str(exc))
            return
        config.output_root.mkdir(parents=True, exist_ok=True)
        self.result = None
        self.stop_event.clear()
        self.progress.set(0)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self._append_log(f"자동 분석을 시작합니다: {config.start_url}", "info")

        def worker() -> None:
            inspector = SiteInspector(
                config,
                log=lambda message, level="info": self.events.put(("log", message, level)),
                progress=lambda current, total, message: self.events.put(
                    ("progress", current, total, message)
                ),
                stop_event=self.stop_event,
            )
            result = inspector.run()
            self.events.put(("done", result))

        self.worker = threading.Thread(target=worker, daemon=True, name="site-inspector")
        self.worker.start()

    def _stop(self) -> None:
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self._append_log("중지 요청을 전달했습니다. 현재 브라우저 동작이 끝나면 종료합니다.", "warning")

    def _open_result(self) -> None:
        if self.result is None:
            return
        path = str(self.result.output_dir)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self._append_log(event[1], event[2])
                elif event[0] == "progress":
                    _kind, current, total, message = event
                    self.progress.set(min(1.0, current / max(1, total)))
                    self._append_log(message, "info")
                elif event[0] == "done":
                    self.result = event[1]
                    self.progress.set(1)
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.open_button.configure(state="normal")
                    self._append_log(f"완료: {self.result.output_dir}", "success")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _append_log(self, message: str, level: str) -> None:
        prefixes = {"success": "[완료]", "warning": "[주의]", "error": "[오류]", "info": "[정보]"}
        self.log_text.insert("end", f"{prefixes.get(level, '[정보]')} {message}\n")
        self.log_text.see("end")


def _run_cli(args) -> int:
    date_offsets = tuple(
        int(value.strip()) for value in args.date_probe_offsets.split(",") if value.strip()
    )
    config = InspectorConfig(
        start_url=args.url,
        output_root=Path(args.output),
        max_pages=args.max_pages,
        max_states=args.max_states,
        max_actions_per_page=args.max_actions,
        max_depth=args.max_depth,
        manual_intervention_timeout_seconds=args.manual_wait,
        date_probe_offsets_days=date_offsets,
        max_date_probes=args.max_date_probes,
        follow_related_subdomains=not args.same_host_only,
    )
    inspector = SiteInspector(
        config,
        log=lambda message, level="info": print(f"[{level}] {message}"),
        progress=lambda current, total, message: print(f"[{current}/{total}] {message}"),
    )
    result = inspector.run()
    print(result.output_dir)
    return 0 if result.states else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Pengucro one-click site inspector")
    parser.add_argument("--url", help="분석할 시작 URL")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-states", type=int, default=36)
    parser.add_argument("--max-actions", type=int, default=6)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--manual-wait", type=int, default=90)
    parser.add_argument("--date-probe-offsets", default="30,90,180")
    parser.add_argument("--max-date-probes", type=int, default=12)
    parser.add_argument(
        "--same-host-only",
        action="store_true",
        help="같은 등록 도메인의 형제 서브도메인을 탐색하지 않음",
    )
    args = parser.parse_args()
    if args.url:
        return _run_cli(args)
    app = SiteInspectorApp()
    app.mainloop()
    return 0
