import customtkinter as ctk
import inspect

app = ctk.CTk()
tb = ctk.CTkTextbox(app)
# Print the source of _set_scaling from its parent class if not in tb
try:
    print(inspect.getsource(tb._set_scaling))
except Exception as e:
    print(e)
