import customtkinter as ctk

app = ctk.CTk()
tb = ctk.CTkTextbox(app)
tb.pack()

# Print all methods/attributes of tb containing "font" or "scale" or "scaling"
print("Attributes with 'font':", [x for x in dir(tb) if "font" in x.lower()])
print("Attributes with 'scale' or 'scaling':", [x for x in dir(tb) if "scale" in x.lower() or "scaling" in x.lower()])
