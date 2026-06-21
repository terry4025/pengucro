import customtkinter as ctk

app = ctk.CTk()
font_normal = ctk.CTkFont(family="Segoe UI", size=11, weight="normal")
tb = ctk.CTkTextbox(app, font=font_normal)
tb.pack()

print("tb._textbox cget font:", tb._textbox.cget("font"))
print("str(font_normal):", str(font_normal))
