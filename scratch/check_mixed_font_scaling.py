import customtkinter as ctk

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")

# Textbox with tuple font (passes verify_ui.py test)
tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

# Tags with CTkFont objects (enables auto scaling)
log_font_normal = ctk.CTkFont(family=FONT_MONO[0], size=FONT_MONO[1], weight="normal")
log_font_bold = ctk.CTkFont(family=FONT_MONO[0], size=FONT_MONO[1], weight="bold")

tb._textbox.tag_config("info", font=log_font_normal, foreground="white")
tb._textbox.tag_config("cat_default", font=log_font_normal, foreground="gray")
tb._textbox.tag_config("cat_bold", font=log_font_bold, foreground="blue")

tb.insert("end", "[00:41:31] ", "cat_default")
tb.insert("end", "[YesCaptcha] ", "cat_bold")
tb.insert("end", "This is normal body text\n", "info")

# Apply scaling to 1.5x
ctk.set_widget_scaling(1.5)

# Verify scaled font properties
print("tb.cget('font'):", tb.cget("font"))
print("Underlying textbox tk font size:", tb._textbox.cget("font"))
print("CTkFont normal size in pixels (should be ~-16):", log_font_normal.cget("size"))
print("CTkFont bold size in pixels (should be ~-16):", log_font_bold.cget("size"))
