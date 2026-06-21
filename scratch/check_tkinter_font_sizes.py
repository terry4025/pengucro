import customtkinter as ctk
import tkinter.font as tkfont

app = ctk.CTk()
FONT_MONO = ("Segoe UI", 11, "normal")

tb = ctk.CTkTextbox(app, font=FONT_MONO)
tb.pack()

log_font_normal = ctk.CTkFont(family=FONT_MONO[0], size=FONT_MONO[1], weight="normal")
log_font_bold = ctk.CTkFont(family=FONT_MONO[0], size=FONT_MONO[1], weight="bold")

tb._textbox.tag_config("info", font=log_font_normal, foreground="white")
tb._textbox.tag_config("cat_bold", font=log_font_bold, foreground="blue")

# Apply scaling to 1.5x
ctk.set_widget_scaling(1.5)

# Inspect actual sizes in Tkinter engine
tb_tk_font = tkfont.Font(font=tb._textbox.cget("font"))
normal_tk_font = tkfont.Font(name=str(log_font_normal), exists=True)
bold_tk_font = tkfont.Font(name=str(log_font_bold), exists=True)

print("Textbox actual font size (pixels):", tb_tk_font.actual("size"))
print("Normal tag actual font size (pixels):", normal_tk_font.actual("size"))
print("Bold tag actual font size (pixels):", bold_tk_font.actual("size"))
