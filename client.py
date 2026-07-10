"""Client de chat graphique (Tkinter), style terminal."""
import json
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from tkinter import simpledialog

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

PORT_DEFAULT = 5555
MAX_LINE_SIZE = 8192  # taille max d'une ligne JSON (doit correspondre a server.py)


class JsonSocket:
    """Enveloppe un socket TCP pour envoyer/recevoir des objets JSON ligne par ligne."""

    def __init__(self, sock):
        self.sock = sock
        self._buffer = b""

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.sock.sendall(data)

    def recv(self):
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buffer += chunk
            if len(self._buffer) > MAX_LINE_SIZE:
                raise ValueError("ligne trop longue")
        line, self._buffer = self._buffer.split(b"\n", 1)
        if not line.strip():
            return {}
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- Palette (style terminal)
BG = "#0a0e0a"
FG = "#33ff33"
FG_DIM = "#1f8f2a"
BORDER = "#1f5c22"
ACCENT = "#33ff33"
COL_ADMIN = "#ff5555"
COL_MODO = "#55ffff"
COL_USER = "#33ff33"
COL_SYSTEM = "#ffff55"
COL_ERROR = "#ff5555"
COL_PRIVATE = "#ff55ff"

ROLE_COLOR = {"admin": COL_ADMIN, "moderator": COL_MODO, "user": COL_USER}

FONT = ("Consolas", 10)
FONT_BOLD = ("Consolas", 10, "bold")
FONT_TITLE = ("Consolas", 15, "bold")

# Liste des commandes affichee dans le panneau de gauche.
COMMANDS_HELP = [
    ("/nick <pseudo>", "changer de pseudo"),
    ("/msg <pseudo> <texte>", "message prive"),
    ("/join <salon> [mdp]", "rejoindre un salon"),
    ("/create <salon> [mdp]", "creer un salon"),
    ("/leave", "quitter -> general"),
    ("/rooms", "lister les salons"),
    ("/users", "lister les utilisateurs"),
    ("/time", "heure du serveur"),
    ("/ping", "mesurer la latence"),
    ("/clear", "effacer l'ecran"),
    ("/admin <mot_de_passe>", "devenir admin (3 essais max)"),
    ("/kick <pseudo>", "expulser (modo+)"),
    ("/mute <pseudo>", "rendre muet (modo+)"),
    ("/unmute <pseudo>", "reparler (modo+)"),
    ("/ban <pseudo>", "bannir (admin)"),
    ("/unban <pseudo>", "debannir (admin)"),
    ("/setmodo <pseudo>", "promouvoir modo (admin)"),
    ("/remmodo <pseudo>", "retrograder modo (admin)"),
    ("/setadmin <pseudo>", "promouvoir admin (admin)"),
    ("/remadmin <pseudo>", "retrograder admin (admin)"),
    ("/help", "aide serveur"),
]


def ts_now():
    return time.strftime("%H:%M:%S")


def flat_button(parent, text, command, font=FONT, padx=10, pady=4):
    """Bouton rectangulaire plat, bordure verte, fidele a l'esthetique terminal."""
    return tk.Button(
        parent, text=text, command=command, font=font,
        bg=BG, fg=FG, activebackground="#0f1f0f", activeforeground=FG,
        relief="solid", bd=1, highlightbackground=BORDER,
        padx=padx, pady=pady, cursor="hand2",
    )


class ChatGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Chat")
        self.root.geometry("980x640")
        self.root.minsize(760, 480)
        self.root.configure(bg=BG)

        self.jsock = None
        self.sock = None
        self.queue = queue.Queue()
        self.pseudo = None
        self.room = "general"
        self.last_ping_sent = None
        self.connected = False
        self.my_role = "user"

        self._build_login_screen()
        self.root.after(80, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- LOGIN
    def _build_login_screen(self):
        self.login_frame = tk.Frame(self.root, bg=BG)
        self.login_frame.place(relx=0.5, rely=0.5, anchor="center")

        card = tk.Frame(self.login_frame, bg=BG, padx=40, pady=32,
                         highlightbackground=BORDER, highlightthickness=1)
        card.pack()

        tk.Label(card, text="=== CHAT TERMINAL ===", font=FONT_TITLE, bg=BG, fg=FG).pack()
        tk.Label(card, text="entrez un pseudo pour vous connecter", font=FONT, bg=BG, fg=FG_DIM).pack(pady=(4, 16))

        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value=str(PORT_DEFAULT))
        self.pseudo_var = tk.StringVar()

        prompt_row = tk.Frame(card, bg=BG)
        prompt_row.pack(fill="x")
        tk.Label(prompt_row, text="login>", font=FONT_BOLD, bg=BG, fg=FG).pack(side="left")
        pseudo_entry = tk.Entry(
            prompt_row, textvariable=self.pseudo_var, font=FONT, bg=BG, fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        pseudo_entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(6, 0))
        pseudo_entry.focus_set()
        pseudo_entry.bind("<Return>", lambda e: self._on_connect_click())

        self.login_error = tk.Label(card, text="", fg=COL_ERROR, bg=BG, font=FONT, wraplength=280)
        self.login_error.pack(pady=(8, 6))

        self.connect_btn = flat_button(card, "[ connecter ]", self._on_connect_click, font=FONT_BOLD, pady=8)
        self.connect_btn.pack(fill="x", pady=(6, 0))

    def _on_connect_click(self):
        if self.connected:
            return
        host = self.host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            self.login_error.config(text="Port invalide.")
            return
        pseudo = self.pseudo_var.get().strip()
        if not pseudo:
            self.login_error.config(text="Choisis un pseudo.")
            return
        self.login_error.config(text="")
        self.connect_btn.config(state="disabled", text="[ connexion... ]")
        threading.Thread(target=self._connect_worker, args=(host, port, pseudo), daemon=True).start()

    def _connect_worker(self, host, port, pseudo):
        try:
            sock = socket.create_connection((host, port), timeout=8)
        except OSError as exc:
            self.queue.put(("login_error", f"Connexion impossible : {exc}"))
            return
        sock.settimeout(None)
        jsock = JsonSocket(sock)
        try:
            jsock.send({"cmd": "hello", "pseudo": pseudo})
        except OSError as exc:
            self.queue.put(("login_error", f"Erreur d'envoi : {exc}"))
            return

        while True:
            try:
                obj = jsock.recv()
            except OSError as exc:
                self.queue.put(("login_error", f"Connexion perdue : {exc}"))
                return
            if obj is None:
                self.queue.put(("login_error", "Connexion fermée par le serveur."))
                return
            if obj.get("type") == "error":
                self.queue.put(("login_error", obj.get("text", "Erreur inconnue.")))
                return
            if obj.get("type") == "welcome":
                self.sock = sock
                self.jsock = jsock
                self.queue.put(("login_success", obj))
                break

        while True:
            try:
                obj = jsock.recv()
            except OSError:
                self.queue.put(("disconnected", None))
                return
            if obj is None:
                self.queue.put(("disconnected", None))
                return
            self.queue.put(("message", obj))

    # ---------------------------------------------------------------- COMMANDES (panneau gauche)
    def _build_commands_panel(self, root_frame):
        panel = tk.Frame(root_frame, bg=BG, width=230)
        panel.pack(side="left", fill="y")
        panel.pack_propagate(False)
        tk.Frame(root_frame, bg=BORDER, width=1).pack(side="left", fill="y")

        tk.Label(panel, text="-- commandes --", font=FONT, bg=BG, fg=FG_DIM).pack(
            anchor="w", padx=10, pady=(12, 0)
        )
        tk.Label(
            panel, text="(a taper dans le champ '>' en bas)", font=("Consolas", 8),
            bg=BG, fg=FG_DIM, wraplength=200, justify="left",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        list_frame = tk.Frame(panel, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=10)
        canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        for cmd, desc in COMMANDS_HELP:
            tk.Label(inner, text=cmd, font=("Consolas", 9, "bold"), bg=BG, fg=FG, anchor="w").pack(
                fill="x", pady=(6, 0)
            )
            tk.Label(inner, text=desc, font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w").pack(
                fill="x", pady=(0, 2)
            )

    # ---------------------------------------------------------------- MAIN UI
    def _build_main_ui(self):
        self.login_frame.destroy()

        root_frame = tk.Frame(self.root, bg=BG)
        root_frame.pack(fill="both", expand=True)

        self._build_commands_panel(root_frame)

        main = tk.Frame(root_frame, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=BG, height=32)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(main, bg=BORDER, height=1).pack(fill="x")
        self.room_label = tk.Label(header, text=f"room: {self.room}", font=FONT_BOLD, bg=BG, fg=FG)
        self.room_label.pack(side="left", padx=(10, 0))
        self.pseudo_label = tk.Label(header, text=f"user: {self.pseudo}", font=FONT, bg=BG, fg=FG_DIM)
        self.pseudo_label.pack(side="right", padx=10)

        self.chat_text = tk.Text(
            main, bg=BG, fg=FG, font=FONT, wrap="word", relief="flat",
            state="disabled", padx=10, pady=8, borderwidth=0, insertbackground=FG,
        )
        self.chat_text.pack(fill="both", expand=True)
        self._configure_tags()

        toolbar = tk.Frame(main, bg=BG)
        toolbar.pack(fill="x", padx=8, pady=(2, 4))
        for label, cmd in [
            ("rooms", "/rooms"), ("users", "/users"), ("time", "/time"),
            ("ping", "/ping"), ("clear", "/clear"), ("help", "/help"),
        ]:
            flat_button(toolbar, f"[{label}]", lambda c=cmd: self._run_local_command(c),
                        font=("Consolas", 9), padx=6, pady=2).pack(side="left", padx=2)

        self.admin_toolbar = tk.Frame(main, bg=BG)
        self.admin_toolbar.pack(fill="x", padx=8, pady=(0, 4))

        input_bar = tk.Frame(main, bg=BG)
        input_bar.pack(fill="x", padx=8, pady=(0, 10))
        tk.Label(input_bar, text=">", font=FONT_BOLD, bg=BG, fg=FG).pack(side="left", padx=(2, 4))
        self.msg_var = tk.StringVar()
        entry = tk.Entry(
            input_bar, textvariable=self.msg_var, font=FONT, bg=BG, fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        entry.bind("<Return>", lambda e: self._send_current_input())
        entry.focus_set()
        flat_button(input_bar, "[ send ]", self._send_current_input, font=FONT_BOLD).pack(side="left")

        sidebar = tk.Frame(root_frame, bg=BG, width=220)
        sidebar.pack(side="right", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(root_frame, bg=BORDER, width=1).pack(side="right", fill="y")

        rooms_header = tk.Frame(sidebar, bg=BG)
        rooms_header.pack(fill="x", padx=10, pady=(12, 0))
        tk.Label(rooms_header, text="-- rooms --", font=FONT, bg=BG, fg=FG_DIM).pack(side="left")
        flat_button(rooms_header, "[+ creer]", self._open_create_room_dialog, font=("Consolas", 8), padx=4, pady=0).pack(side="right")
        tk.Label(
            sidebar, text="(1 clic pour rejoindre)", font=("Consolas", 8), bg=BG, fg=FG_DIM,
        ).pack(anchor="w", padx=10, pady=(0, 4))

        self.room_listbox = tk.Listbox(
            sidebar, bg=BG, fg=FG, relief="flat", font=FONT, height=8,
            highlightthickness=1, highlightbackground=BORDER,
            selectbackground=FG, selectforeground=BG, activestyle="none",
        )
        self.room_listbox.pack(fill="x", padx=10)
        self.room_listbox.bind("<Button-1>", self._on_room_click)
        self._room_entries = []

        tk.Label(sidebar, text="-- users du salon --", font=FONT, bg=BG, fg=FG_DIM).pack(anchor="w", padx=10, pady=(16, 2))
        self.user_listbox = tk.Listbox(
            sidebar, bg=BG, fg=FG, relief="flat", font=FONT,
            highlightthickness=1, highlightbackground=BORDER, activestyle="none",
        )
        self.user_listbox.pack(fill="both", expand=True, padx=10, pady=(0, 12))

        self._send({"cmd": "rooms"})
        self._send({"cmd": "users"})

    def _configure_tags(self):
        self.chat_text.tag_config("ts", foreground=FG_DIM, font=("Consolas", 8))
        self.chat_text.tag_config("system", foreground=COL_SYSTEM)
        self.chat_text.tag_config("error", foreground=COL_ERROR, font=FONT_BOLD)
        self.chat_text.tag_config("private", foreground=COL_PRIVATE)
        self.chat_text.tag_config("room", foreground=FG_DIM, font=("Consolas", 9))
        self.chat_text.tag_config("role_admin", foreground=COL_ADMIN, font=FONT_BOLD)
        self.chat_text.tag_config("role_moderator", foreground=COL_MODO, font=FONT_BOLD)
        self.chat_text.tag_config("role_user", foreground=COL_USER, font=FONT_BOLD)
        self.chat_text.tag_config("body", foreground=FG)

    # ---------------------------------------------------------------- RENDER
    def _append(self, segments):
        self.chat_text.config(state="normal")
        for text, tag in segments:
            self.chat_text.insert("end", text, tag)
        self.chat_text.insert("end", "\n")
        self.chat_text.config(state="disabled")
        self.chat_text.see("end")

    def _render_message(self, obj):
        t = obj.get("type")
        ts = obj.get("ts") or ts_now()

        if t == "chat":
            role = obj.get("role", "user")
            self._append([
                (f"[{ts}] ", "ts"),
                (f"#{obj.get('room', '')} ", "room"),
                (f"<{obj.get('from', '?')}>", f"role_{role}"),
                (f" {obj.get('text', '')}", "body"),
            ])
        elif t == "private":
            who = obj.get("from")
            to = obj.get("to")
            label = f"(MP -> {to}) " if who == self.pseudo else f"(MP de {who}) "
            self._append([(f"[{ts}] ", "ts"), (label + obj.get("text", ""), "private")])
        elif t == "system":
            self._append([(f"[{ts}] ", "ts"), ("*** " + obj.get("text", ""), "system")])
            self._send({"cmd": "users"})
        elif t == "error":
            self._append([(f"[{ts}] ", "ts"), ("!!! " + obj.get("text", ""), "error")])
        elif t == "welcome":
            self._append([(f"[{ts}] ", "ts"), (obj.get("text", ""), "system")])
            for tip in (
                "astuce : double-clique un salon a droite pour le rejoindre",
                "astuce : /create <salon> [mdp] pour creer un salon",
                "astuce : /admin <mot_de_passe> pour devenir administrateur",
                "astuce : la liste complete des commandes est dans le panneau de gauche",
            ):
                self._append([(f"[{ts_now()}] ", "ts"), ("*** " + tip, "system")])
        elif t == "joined":
            self.room = obj.get("room")
            self.room_label.config(text=f"room: {self.room}")
            self._append([(f"[{ts_now()}] ", "ts"), (f"*** tu es maintenant dans #{self.room}", "system")])
            self._send({"cmd": "users"})
            self._send({"cmd": "rooms"})
        elif t == "nick_changed":
            self.pseudo = obj.get("new")
            self.pseudo_label.config(text=f"user: {self.pseudo}")
            self._append([(f"[{ts_now()}] ", "ts"), (f"*** pseudo change en {self.pseudo}", "system")])
        elif t == "roomlist":
            self._update_room_list(obj.get("rooms", []))
        elif t == "userlist":
            self._update_user_list(obj.get("users", []))
        elif t == "time":
            self._append([(f"[{ts}] ", "ts"), (f"heure serveur : {obj.get('text', '')}", "system")])
        elif t == "pong":
            rtt = f"{(time.time() - self.last_ping_sent) * 1000:.1f} ms" if self.last_ping_sent else "?"
            self._append([(f"[{ts}] ", "ts"), (f"pong ! latence : {rtt}", "system")])

    def _update_room_list(self, rooms):
        self._room_entries = rooms
        self.room_listbox.delete(0, "end")
        for r in rooms:
            lock = "[protege] " if r.get("locked") else ""
            self.room_listbox.insert("end", f"{lock}#{r['name']}  ({r['count']})")

    def _update_user_list(self, users):
        self.user_listbox.delete(0, "end")
        for u in users:
            self.user_listbox.insert("end", f"<{u['pseudo']}>")
            idx = self.user_listbox.size() - 1
            self.user_listbox.itemconfig(idx, fg=ROLE_COLOR.get(u["role"], FG))
            if u["pseudo"] == self.pseudo and u["role"] != self.my_role:
                self.my_role = u["role"]
                self._refresh_admin_toolbar()

    def _prompt_target_command(self, action, title, prompt):
        target = simpledialog.askstring(title, prompt, parent=self.root)
        if not target:
            return
        self._send({"cmd": action, "target": target.strip()})

    def _refresh_admin_toolbar(self):
        for widget in self.admin_toolbar.winfo_children():
            widget.destroy()

        actions = []
        if self.my_role in ("moderator", "admin"):
            actions += [
                ("kick", "kick", "Kick", "Pseudo a expulser :"),
                ("mute", "mute", "Mute", "Pseudo a rendre muet :"),
                ("unmute", "unmute", "Unmute", "Pseudo a qui redonner la parole :"),
            ]
        if self.my_role == "admin":
            actions += [
                ("ban", "ban", "Ban", "Pseudo a bannir :"),
                ("unban", "unban", "Unban", "Pseudo a debannir :"),
                ("setmodo", "+modo", "Promouvoir moderateur", "Pseudo a promouvoir moderateur :"),
                ("remmodo", "-modo", "Retrograder moderateur", "Pseudo a retrograder :"),
                ("setadmin", "+admin", "Promouvoir admin", "Pseudo a promouvoir admin :"),
                ("remadmin", "-admin", "Retrograder admin", "Pseudo a retrograder :"),
            ]
        if not actions:
            return

        tk.Label(self.admin_toolbar, text=f"[{self.my_role}]", font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(
            side="left", padx=(2, 6)
        )
        for action, label, title, prompt in actions:
            flat_button(
                self.admin_toolbar, f"[{label}]",
                lambda a=action, t=title, p=prompt: self._prompt_target_command(a, t, p),
                font=("Consolas", 9), padx=6, pady=2,
            ).pack(side="left", padx=2)

    def _on_room_click(self, event):
        index = self.room_listbox.nearest(event.y)
        if index < 0 or index >= len(self._room_entries):
            return
        room = self._room_entries[index]
        password = ""
        if room.get("locked"):
            password = simpledialog.askstring(
                "Salon protégé", f"Mot de passe pour '{room['name']}' :", show="*", parent=self.root
            ) or ""
        self._send({"cmd": "join", "room": room["name"], "password": password})

    def _open_create_room_dialog(self):
        room = simpledialog.askstring("Créer un salon", "Nom du salon :", parent=self.root)
        if not room:
            return
        password = simpledialog.askstring(
            "Créer un salon", "Mot de passe (vide = public) :", show="*", parent=self.root
        ) or ""
        self._send({"cmd": "create", "room": room.strip(), "password": password.strip()})

    # ---------------------------------------------------------------- INPUT / COMMANDES
    def _send(self, obj):
        if self.jsock is None:
            return
        try:
            self.jsock.send(obj)
        except OSError:
            pass

    def _send_current_input(self):
        line = self.msg_var.get()
        if not line:
            return
        self.msg_var.set("")
        if line.startswith("/"):
            self._run_local_command(line)
        else:
            self._send({"cmd": "say", "text": line})

    def _run_local_command(self, line):
        parts = line[1:].split(" ", 1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "clear":
            self.chat_text.config(state="normal")
            self.chat_text.delete("1.0", "end")
            self.chat_text.config(state="disabled")
            return
        if cmd == "nick":
            self._send({"cmd": "nick", "arg": rest})
            return
        if cmd == "msg":
            sub = rest.split(" ", 1)
            target = sub[0] if sub else ""
            text = sub[1] if len(sub) > 1 else ""
            self._send({"cmd": "msg", "target": target, "text": text})
            return
        if cmd == "join":
            sub = rest.split(" ", 1)
            room = sub[0] if sub else ""
            password = sub[1] if len(sub) > 1 else ""
            self._send({"cmd": "join", "room": room, "password": password})
            return
        if cmd == "create":
            sub = rest.split(" ", 1)
            room = sub[0] if sub else ""
            password = sub[1] if len(sub) > 1 else ""
            self._send({"cmd": "create", "room": room, "password": password})
            return
        if cmd == "leave":
            self._send({"cmd": "leave"})
            return
        if cmd == "rooms":
            self._send({"cmd": "rooms"})
            return
        if cmd == "users":
            self._send({"cmd": "users"})
            return
        if cmd == "time":
            self._send({"cmd": "time"})
            return
        if cmd == "ping":
            self.last_ping_sent = time.time()
            self._send({"cmd": "ping", "ts": self.last_ping_sent})
            return
        if cmd == "admin":
            self._send({"cmd": "admin", "arg": rest})
            return
        if cmd == "help":
            self._send({"cmd": "help"})
            return
        if cmd in ("kick", "mute", "unmute", "ban", "unban", "setmodo", "remmodo", "setadmin", "remadmin"):
            self._send({"cmd": cmd, "target": rest})
            return
        self._append([(f"[{ts_now()}] ", "ts"), (f"!!! commande locale inconnue : /{cmd}", "error")])

    # ---------------------------------------------------------------- QUEUE / LIFECYCLE
    def _poll_queue(self):
        try:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "login_error":
                        self.connect_btn.config(state="normal", text="[ connecter ]")
                        self.login_error.config(text=payload)
                    elif kind == "login_success":
                        self.connected = True
                        self.pseudo = payload.get("pseudo")
                        self.room = payload.get("room", "general")
                        self._build_main_ui()
                        self._render_message(payload)
                    elif kind == "message":
                        self._render_message(payload)
                    elif kind == "disconnected":
                        if self.connected:
                            self._append([(f"[{ts_now()}] ", "ts"), ("!!! connexion fermée par le serveur", "error")])
                        self.connected = False
                except Exception as exc:
                    print(f"Erreur de rendu (ignoree) : {exc}", flush=True)
        finally:
            self.root.update_idletasks()
            self.root.after(80, self._poll_queue)

    def _on_close(self):
        if self.jsock is not None:
            self.jsock.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ChatGUI().run()
