import customtkinter as ctk

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")

tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

log_font_normal = tb._font
font_family = log_font_normal.cget("family")
font_size = log_font_normal.cget("size")
log_font_bold = ctk.CTkFont(family=font_family, size=font_size, weight="bold")

tb._textbox.tag_config("info", font=log_font_normal, foreground="white")
tb._textbox.tag_config("cat_default", font=log_font_normal, foreground="gray")
tb._textbox.tag_config("cat_bold", font=log_font_bold, foreground="blue")

tb.insert("end", "[00:41:31] ", "cat_default")
tb.insert("end", "[YesCaptcha] ", "cat_bold")
tb.insert("end", "This is normal body text\n", "info")

print("Base font:", tb.cget("font"))
print("Is base font equal to FONT_MONO?", tb.cget("font") == FONT_MONO)
print("info tag font:", tb._textbox.tag_cget("info", "font"))
print("cat_bold tag font:", tb._textbox.tag_cget("cat_bold", "font"))
