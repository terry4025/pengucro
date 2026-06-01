import customtkinter as ctk
from ui.main_window import MainWindow

def main():
    # Set global CustomTkinter appearance
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")  # Using default blue theme as fallback base

    # Create and run the main window
    app = MainWindow()
    app.mainloop()

if __name__ == '__main__':
    main()
