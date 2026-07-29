import logging

import customtkinter as ctk

from pengucro import logging_setup
from ui.main_window import MainWindow

def configure_windows_app_identity():
    """Give Windows a stable identity for taskbar grouping and icon lookup."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "terry4025.pengucro"
        )
    except Exception:
        pass

def main():
    configure_windows_app_identity()

    # Install the rotating log handler before anything else so that catalog and
    # engine diagnostics emitted during start-up are captured.
    log_path = logging_setup.configure()
    logging.getLogger(__name__).info("Pengucro starting (log file: %s)", log_path)

    # Set global CustomTkinter appearance
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")  # Using default blue theme as fallback base

    # Create and run the main window
    app = MainWindow()
    try:
        app.mainloop()
    finally:
        logging.getLogger(__name__).info("Pengucro exited")
        logging.shutdown()

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
