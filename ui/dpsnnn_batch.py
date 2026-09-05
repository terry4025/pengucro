"""One process owns up to four independent non-member DPSNNN reservations."""
from __future__ import annotations
import queue
from tkinter import messagebox

import customtkinter as ctk

from engines.dpsnnn_engine import DpsnnnEngine, calculate_dpsnnn_open_datetime
from pengucro import __version__, logging_setup
from pengucro.dpsnnn_plan import load_plan


def run_plan_window(path):
    ctk.set_appearance_mode("dark")
    app = ctk.CTk()
    app.title(f"펭크로 v{__version__} · 단편선 동시 예약")
    app.geometry("940x640")
    try:
        rows = load_plan(path)
    except (OSError, ValueError) as exc:
        messagebox.showerror("예약 목록 확인", str(exc), parent=app)
        app.destroy()
        return 2
    for row in rows:
        logging_setup.register_sensitive_mapping(row)
    messages = queue.Queue()
    engines = []
    statuses = []
    started = False
    ctk.CTkLabel(app, text="단편선 비회원 · 무통장입금 예약", font=("", 22, "bold")).pack(pady=18)
    for index, row in enumerate(rows):
        frame = ctk.CTkFrame(app)
        frame.pack(fill="x", padx=20, pady=4)
        branch = "강남" if row["branch"] == "gangnam" else "성수"
        label = (f"{index + 1}. {branch} {row['themePK']}  |  {row['reservationDate']} "
                 f"{row['reservationTime'][:5]}  |  {row['name']} · 끝 {row['phone'][-4:]}  |  {row['people']}인")
        ctk.CTkLabel(frame, text=label).pack(side="left", padx=12, pady=10)
        status = ctk.CTkLabel(frame, text="대기")
        status.pack(side="right", padx=12)
        statuses.append(status)
    opens = sorted({calculate_dpsnnn_open_datetime(row["reservationDate"]).strftime("%m/%d %H:%M") for row in rows})
    ctk.CTkLabel(app, text=f"오픈 예정: {', '.join(opens)} 한국시간 · 시작 후 미오픈 날짜도 계속 감시합니다.").pack(pady=8)
    ctk.CTkLabel(app, text="접수 알림톡 확인 후 입금: 강남 30분 / 성수 1시간 · 입금자명은 예약자와 동일하게").pack()
    log = ctk.CTkTextbox(app)
    log.pack(fill="both", expand=True, padx=20, pady=12)
    log.configure(state="disabled")

    def start():
        nonlocal started
        if started:
            return
        started = True
        start_button.configure(state="disabled")
        for index, row in enumerate(rows):
            engine = DpsnnnEngine(
                lambda text, level, i=index: messages.put((i, text, level)),
                lambda i=index: messages.put((i, "예약 접수 완료 · 입금 대기", "received")))
            engines.append(engine)
            statuses[index].configure(text="감시 중")
            try:
                # Four bookings share the process-wide request budget, with one
                # HTTP observer and one warmed checkout context per booking.
                engine.start_reservation(row, 1)
            except Exception as exc:
                statuses[index].configure(text="시작 실패")
                messages.put((index, f"시작 실패: {type(exc).__name__}", "error"))

    controls = ctk.CTkFrame(app, fg_color="transparent")
    controls.pack(pady=(0, 14))
    start_button = ctk.CTkButton(controls, text=f"{len(rows)}건 예약 감시 시작", command=start)
    start_button.pack(side="left", padx=8)
    def stop():
        for engine in engines:
            engine.stop_reservation()
    ctk.CTkButton(controls, text="감시 중지", command=stop).pack(side="left", padx=8)
    def poll():
        while True:
            try:
                index, text, level = messages.get_nowait()
            except queue.Empty:
                break
            log.configure(state="normal")
            log.insert("end", f"[{index + 1}번] {text}\n")
            log.see("end")
            log.configure(state="disabled")
            if level == "received":
                statuses[index].configure(text="접수 · 입금 대기", text_color="#75d69c")
        for index, engine in enumerate(engines):
            if not engine.is_running and not engine._success_fired:
                statuses[index].configure(text="확인 필요", text_color="#efbc67")
        app.after(100, poll)
    def close():
        stop()
        app.destroy()
    app.protocol("WM_DELETE_WINDOW", close)
    app.after(100, poll)
    app.mainloop()
    return 0
