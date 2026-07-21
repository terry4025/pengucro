import customtkinter as ctk
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

    # Set global CustomTkinter appearance
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")  # Using default blue theme as fallback base

    # Create and run the main window
    app = MainWindow()
    app.mainloop()

if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    main()
