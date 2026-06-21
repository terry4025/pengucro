import customtkinter as ctk

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")
tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

# Override all methods that might be called during scaling changes
methods_to_trace = [
    "_apply_font_scaling",
    "_apply_widget_scaling",
    "_apply_window_scaling",
    "_update_font",
    "_set_scaling",
    "_draw"
]

for name in methods_to_trace:
    orig = getattr(tb, name)
    def make_wrapper(n, o):
        def wrapper(*args, **kwargs):
            print(f"{n} called with args={args} kwargs={kwargs}")
            return o(*args, **kwargs)
        return wrapper
    setattr(tb, name, make_wrapper(name, orig))

print("--- Calling set_widget_scaling(1.5) ---")
ctk.set_widget_scaling(1.5)
app.update()
print("--- Done ---")
