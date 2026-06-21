import customtkinter as ctk
import tkinter.font as tkfont

app = ctk.CTk()
font_normal = ctk.CTkFont(family="Segoe UI", size=11, weight="normal")
tb = ctk.CTkTextbox(app, font=font_normal)
tb.pack()

tb._textbox.tag_config("info", font=font_normal, foreground="white")

# Apply scaling to 1.5x
ctk.set_widget_scaling(1.5)

tb_tk_font = tkfont.Font(font=tb._textbox.cget("font"))
normal_tk_font = tkfont.Font(name=str(font_normal), exists=True)

print("Textbox actual font size (pixels):", tb_tk_font.actual("size"))
print("Normal tag actual font size (pixels):", normal_tk_font.actual("size"))
