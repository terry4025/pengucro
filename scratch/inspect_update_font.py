import customtkinter as ctk
import inspect

app = ctk.CTk()
tb = ctk.CTkTextbox(app)
print(inspect.getsource(tb._update_font))
