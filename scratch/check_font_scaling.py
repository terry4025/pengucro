import customtkinter as ctk

app = ctk.CTk()
font = ctk.CTkFont(family="Segoe UI", size=11, weight="normal")
tb = ctk.CTkTextbox(app, font=font)
tb.pack()

print("CTkFont type:", type(font))
print("CTkFont name (used for tkinter):", font)
print("Underlying tk Font object:", font.cget("family"), font.cget("size"))

# Try configuring a tag with the CTkFont object
tb._textbox.tag_config("test_tag", font=font)
print("Tag config font:", tb._textbox.tag_cget("test_tag", "font"))
