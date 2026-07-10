"""Serveur de chat TCP multi-clients, multi-salons, avec rôles et modération."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:
    _HAS_BCRYPT = False

# Protocole : chaque message est un objet JSON encode en UTF-8, suivi d'un '\n'.
MAX_LINE_SIZE = 8192  # taille max d'une ligne JSON (anti flood / anti memoire)

MAX_PSEUDO_LEN = 16
MIN_PSEUDO_LEN = 3
MAX_ROOM_LEN = 20
MIN_ROOM_LEN = 2
MAX_MSG_LEN = 500
PORT_DEFAULT = 5555
DEFAULT_ROOM = "general"
ROLE_LEVEL = {"user": 0, "moderator": 1, "admin": 2}


class LineTooLongError(Exception):
    pass


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
                raise LineTooLongError("ligne trop longue")
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


def valid_pseudo(name):
    if not isinstance(name, str):
        return False
    if not (MIN_PSEUDO_LEN <= len(name) <= MAX_PSEUDO_LEN):
        return False
    return all(c.isalnum() or c in "_-" for c in name)


def valid_room(name):
    if not isinstance(name, str):
        return False
    if not (MIN_ROOM_LEN <= len(name) <= MAX_ROOM_LEN):
        return False
    return all(c.isalnum() or c in "_-" for c in name)


def clean_text(text, max_len=MAX_MSG_LEN):
    if not isinstance(text, str):
        return ""
    text = "".join(c for c in text if c == "\n" or c == "\t" or ord(c) >= 32)
    text = text.strip()
    return text[:max_len]


def hash_password(password):
    """Hash un mot de passe de salon avec bcrypt (repli PBKDF2-SHA256 si bcrypt indisponible)."""
    if _HAS_BCRYPT:
        return "bcrypt$" + bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, hashed):
    try:
        scheme, rest = hashed.split("$", 1)
    except ValueError:
        return False
    if scheme == "bcrypt":
        if not _HAS_BCRYPT:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), rest.encode("ascii"))
        except ValueError:
            return False
    if scheme == "pbkdf2":
        try:
            salt_b64, digest_b64 = rest.split("$", 1)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, base64.binascii.Error):
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(actual, expected)
    return False


HOST = "0.0.0.0"
PORT = PORT_DEFAULT
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

IDLE_TIMEOUT = 300          # secondes d'inactivité avant déconnexion automatique
SOCKET_POLL_TIMEOUT = 1.0   # pour pouvoir vérifier périodiquement l'inactivité
RATE_LIMIT_COUNT = 6        # messages...
RATE_LIMIT_WINDOW = 4       # ...par fenêtre de N secondes
MAX_FLOOD_STRIKES = 3       # kick après ce nombre d'avertissements de flood
MAX_ADMIN_ATTEMPTS = 3      # déconnexion après ce nombre d'essais /admin ratés

# Cles reservees dans users.json (caractere '*' interdit dans un pseudo : aucune collision possible).
ADMIN_KEY = "*admin*"
ROOMS_KEY = "*rooms*"

HELP_TEXT = (
    "Commandes disponibles :\n"
    "/nick <pseudo> - changer de pseudo\n"
    "/msg <pseudo> <texte> - message privé\n"
    "/join <salon> [mdp] - rejoindre un salon existant (mdp si protégé)\n"
    "/create <salon> [mdp] - créer et rejoindre un salon (mdp optionnel, hashé bcrypt)\n"
    "/leave - quitter le salon courant (retour à general)\n"
    "/rooms - lister les salons\n"
    "/users - lister les utilisateurs du salon courant\n"
    "/time - heure du serveur\n"
    "/ping - latence avec le serveur\n"
    "/clear - effacer l'affichage local\n"
    "/admin <mot_de_passe> - devenir admin (bootstrap, déconnexion après 3 échecs)\n"
    "Modération : /kick /mute /unmute <pseudo> (modérateur+)\n"
    "             /ban /unban /setmodo /remmodo /setadmin /remadmin <pseudo> (admin)\n"
    "/help - afficher cette aide"
)


def now_str():
    return datetime.now().strftime("%H:%M:%S")


class UserStore:
    """Gère la persistance des comptes (rôle, bannissement) dans un fichier JSON."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {}
        else:
            self.data = {}

    def _save(self):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def get_or_create(self, pseudo):
        with self.lock:
            if pseudo not in self.data:
                self.data[pseudo] = {"role": "user", "banned": False}
                self._save()
            return dict(self.data[pseudo])

    def get(self, pseudo):
        with self.lock:
            entry = self.data.get(pseudo)
            return dict(entry) if entry else None

    def set_role(self, pseudo, role):
        with self.lock:
            entry = self.data.setdefault(pseudo, {"role": "user", "banned": False})
            entry["role"] = role
            self._save()

    def set_banned(self, pseudo, banned):
        with self.lock:
            entry = self.data.setdefault(pseudo, {"role": "user", "banned": False})
            entry["banned"] = banned
            self._save()

    def rename(self, old_pseudo, new_pseudo):
        with self.lock:
            entry = self.data.pop(old_pseudo, {"role": "user", "banned": False})
            self.data[new_pseudo] = entry
            self._save()

    def is_banned(self, pseudo):
        with self.lock:
            entry = self.data.get(pseudo)
            return bool(entry and entry.get("banned"))

    def role_of(self, pseudo):
        with self.lock:
            entry = self.data.get(pseudo)
            return entry["role"] if entry else "user"

    def get_admin_hash(self):
        with self.lock:
            return self.data.get(ADMIN_KEY, {}).get("password_hash")

    def set_admin_hash(self, password_hash):
        with self.lock:
            self.data[ADMIN_KEY] = {"password_hash": password_hash}
            self._save()

    def get_rooms(self):
        with self.lock:
            return dict(self.data.get(ROOMS_KEY, {}))

    def save_room(self, name, password_hash):
        with self.lock:
            rooms = self.data.setdefault(ROOMS_KEY, {})
            rooms[name] = {"password_hash": password_hash} if password_hash else {}
            self._save()


class ClientHandler(threading.Thread):
    def __init__(self, server, conn, addr):
        super().__init__(daemon=True)
        self.server = server
        self.conn = conn
        self.addr = addr
        self.conn.settimeout(SOCKET_POLL_TIMEOUT)
        self.jsock = JsonSocket(conn)
        self.pseudo = None
        self.room = None
        self.last_activity = time.time()
        self._msg_times = deque()
        self._flood_strikes = 0
        self._admin_attempts = 0
        self.alive = True
        self.muted = False

    # ---------- Aide envoi ----------
    def send(self, obj):
        try:
            self.jsock.send(obj)
        except OSError:
            self.alive = False

    def send_system(self, text):
        self.send({"type": "system", "text": text, "ts": now_str()})

    def send_error(self, text):
        self.send({"type": "error", "text": text, "ts": now_str()})

    # ---------- Cycle de vie ----------
    def run(self):
        try:
            if not self._handshake():
                return
            self._loop()
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self._cleanup()

    def _handshake(self):
        """Attend le premier message du client qui doit contenir le pseudo souhaité."""
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                obj = self.jsock.recv()
            except socket.timeout:
                continue
            except LineTooLongError:
                self.send_error("Message trop long.")
                return False
            if obj is None:
                return False
            if obj.get("cmd") != "hello":
                self.send_error("Handshake invalide : pseudo attendu.")
                continue
            pseudo = obj.get("pseudo", "")
            if not valid_pseudo(pseudo):
                self.send_error(
                    "Pseudo invalide (3-16 caractères alphanumériques, '_' ou '-')."
                )
                continue
            if self.server.is_online(pseudo):
                self.send_error("Ce pseudo est déjà connecté.")
                continue
            if self.server.users.is_banned(pseudo):
                self.send_error("Ce pseudo est banni de ce serveur.")
                return False
            self.pseudo = pseudo
            self.server.users.get_or_create(pseudo)
            self.server.register_client(self)
            self.room = DEFAULT_ROOM
            self.server.join_room(self, DEFAULT_ROOM, announce=True, notify_self=False)
            self.send(
                {
                    "type": "welcome",
                    "pseudo": pseudo,
                    "role": self.server.users.role_of(pseudo),
                    "room": self.room,
                    "text": f"Bienvenue {pseudo} ! Tape /help pour la liste des commandes.",
                }
            )
            return True
        return False

    def _loop(self):
        while self.alive:
            try:
                obj = self.jsock.recv()
            except socket.timeout:
                if time.time() - self.last_activity > IDLE_TIMEOUT:
                    self.send_system("Déconnecté pour inactivité (timeout).")
                    break
                continue
            except LineTooLongError:
                self.send_error("Message trop long, déconnexion.")
                break
            if obj is None:
                break
            self.last_activity = time.time()
            if not self._check_rate_limit():
                continue
            try:
                self._handle(obj)
            except Exception as exc:  # un client ne doit jamais faire planter le serveur
                self.send_error(f"Erreur interne : {exc}")

    def _check_rate_limit(self):
        t = time.time()
        self._msg_times.append(t)
        while self._msg_times and t - self._msg_times[0] > RATE_LIMIT_WINDOW:
            self._msg_times.popleft()
        if len(self._msg_times) > RATE_LIMIT_COUNT:
            self._flood_strikes += 1
            if self._flood_strikes >= MAX_FLOOD_STRIKES:
                self.send_system("Déconnecté pour flood.")
                self.alive = False
                return False
            self.send_error("Tu envoies trop de messages, ralentis un peu.")
            return False
        return True

    def _cleanup(self):
        self.alive = False
        self.server.unregister_client(self)
        self.jsock.close()

    def role(self):
        return self.server.users.role_of(self.pseudo)

    def has_role(self, min_role):
        return ROLE_LEVEL[self.role()] >= ROLE_LEVEL[min_role]

    # ---------- Traitement des commandes ----------
    def _handle(self, obj):
        cmd = obj.get("cmd")
        handler = {
            "say": self._cmd_say,
            "nick": self._cmd_nick,
            "msg": self._cmd_msg,
            "join": self._cmd_join,
            "create": self._cmd_create,
            "leave": self._cmd_leave,
            "rooms": self._cmd_rooms,
            "users": self._cmd_users,
            "time": self._cmd_time,
            "ping": self._cmd_ping,
            "help": self._cmd_help,
            "admin": self._cmd_admin_bootstrap,
            "kick": lambda o: self._cmd_moderation(o, "kick"),
            "mute": lambda o: self._cmd_moderation(o, "mute"),
            "unmute": lambda o: self._cmd_moderation(o, "unmute"),
            "ban": lambda o: self._cmd_moderation(o, "ban"),
            "unban": lambda o: self._cmd_moderation(o, "unban"),
            "setmodo": lambda o: self._cmd_moderation(o, "setmodo"),
            "remmodo": lambda o: self._cmd_moderation(o, "remmodo"),
            "setadmin": lambda o: self._cmd_moderation(o, "setadmin"),
            "remadmin": lambda o: self._cmd_moderation(o, "remadmin"),
        }.get(cmd)
        if handler is None:
            self.send_error(f"Commande inconnue : {cmd}")
            return
        handler(obj)

    def _cmd_say(self, obj):
        if self.muted:
            self.send_error("Tu es muet(te) sur ce serveur.")
            return
        text = clean_text(obj.get("text", ""))
        if not text:
            return
        self.server.broadcast_room(
            self.room,
            {
                "type": "chat",
                "room": self.room,
                "from": self.pseudo,
                "role": self.role(),
                "text": text,
                "ts": now_str(),
            },
        )

    def _cmd_nick(self, obj):
        new_pseudo = obj.get("arg", "")
        if not valid_pseudo(new_pseudo):
            self.send_error("Pseudo invalide (3-16 caractères alphanumériques, '_' ou '-').")
            return
        if new_pseudo == self.pseudo:
            return
        if self.server.is_online(new_pseudo):
            self.send_error("Ce pseudo est déjà utilisé.")
            return
        if self.server.users.is_banned(new_pseudo):
            self.send_error("Ce pseudo est banni.")
            return
        old_pseudo = self.pseudo
        self.server.rename_client(self, old_pseudo, new_pseudo)
        self.send({"type": "nick_changed", "old": old_pseudo, "new": new_pseudo})
        self.server.broadcast_room(
            self.room,
            {"type": "system", "text": f"{old_pseudo} s'appelle désormais {new_pseudo}.", "ts": now_str()},
        )

    def _cmd_msg(self, obj):
        target = obj.get("target", "")
        text = clean_text(obj.get("text", ""))
        if not text:
            return
        target_client = self.server.get_client(target)
        if target_client is None:
            self.send_error(f"Utilisateur '{target}' introuvable ou hors ligne.")
            return
        payload = {"type": "private", "from": self.pseudo, "to": target, "text": text, "ts": now_str()}
        target_client.send(payload)
        self.send(payload)

    def _cmd_join(self, obj):
        room = obj.get("room", "")
        password = obj.get("password", "") or ""
        if not valid_room(room):
            self.send_error("Nom de salon invalide (2-20 caractères alphanumériques, '_' ou '-').")
            return
        if not self.server.room_exists(room):
            self.send_error(f"Le salon '{room}' n'existe pas. Utilise /create pour le créer.")
            return
        if not self.server.check_room_password(room, password):
            self.send_error("Mot de passe du salon incorrect.")
            return
        self.server.join_room(self, room, announce=True)

    def _cmd_create(self, obj):
        room = obj.get("room", "")
        password = obj.get("password", "") or ""
        if not valid_room(room):
            self.send_error("Nom de salon invalide (2-20 caractères alphanumériques, '_' ou '-').")
            return
        created = self.server.create_room(room, password)
        if not created:
            self.send_error(f"Le salon '{room}' existe déjà.")
            return
        self.server.join_room(self, room, announce=True)

    def _cmd_leave(self, obj):
        if self.room == DEFAULT_ROOM:
            self.send_error(f"Tu es déjà dans le salon par défaut '{DEFAULT_ROOM}'.")
            return
        self.server.join_room(self, DEFAULT_ROOM, announce=True)

    def _cmd_rooms(self, obj):
        rooms = self.server.list_rooms()
        self.send({"type": "roomlist", "rooms": rooms})

    def _cmd_users(self, obj):
        users = self.server.list_users_in_room(self.room)
        self.send({"type": "userlist", "room": self.room, "users": users})

    def _cmd_time(self, obj):
        self.send({"type": "time", "text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    def _cmd_ping(self, obj):
        self.send({"type": "pong", "client_ts": obj.get("ts"), "ts": now_str()})

    def _cmd_help(self, obj):
        self.send_system(HELP_TEXT)

    def _cmd_admin_bootstrap(self, obj):
        password = obj.get("arg", "")
        admin_hash = self.server.users.get_admin_hash()
        if admin_hash and verify_password(password, admin_hash):
            self._admin_attempts = 0
            self.server.users.set_role(self.pseudo, "admin")
            self.send_system("Tu es maintenant administrateur.")
            return
        self._admin_attempts += 1
        if self._admin_attempts >= MAX_ADMIN_ATTEMPTS:
            self.send_system("Trop de tentatives /admin ratées, déconnexion.")
            self.alive = False
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        self.send_error(
            f"Mot de passe incorrect ({self._admin_attempts}/{MAX_ADMIN_ATTEMPTS} tentatives)."
        )

    def _cmd_moderation(self, obj, action):
        target_name = obj.get("target", "")
        required_role = "admin" if action in (
            "ban", "unban", "setmodo", "remmodo", "setadmin", "remadmin",
        ) else "moderator"
        if not self.has_role(required_role):
            self.send_error("Tu n'as pas la permission d'utiliser cette commande.")
            return
        if target_name == self.pseudo and action in ("kick", "ban", "mute"):
            self.send_error("Tu ne peux pas t'appliquer cette action à toi-même.")
            return
        target_client = self.server.get_client(target_name)

        if action == "kick":
            if target_client is None:
                self.send_error("Utilisateur introuvable ou hors ligne.")
                return
            target_client.send_system(f"Tu as été expulsé par {self.pseudo}.")
            target_client.alive = False
            try:
                target_client.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.server.broadcast_all({"type": "system", "text": f"{target_name} a été kick par {self.pseudo}.", "ts": now_str()})

        elif action == "mute":
            if target_client is None:
                self.send_error("Utilisateur introuvable ou hors ligne.")
                return
            target_client.muted = True
            target_client.send_system(f"Tu as été rendu muet par {self.pseudo}.")
            self.send_system(f"{target_name} est maintenant muet.")

        elif action == "unmute":
            if target_client is None:
                self.send_error("Utilisateur introuvable ou hors ligne.")
                return
            target_client.muted = False
            target_client.send_system(f"Tu peux de nouveau parler.")
            self.send_system(f"{target_name} peut de nouveau parler.")

        elif action == "ban":
            self.server.users.get_or_create(target_name)
            self.server.users.set_banned(target_name, True)
            if target_client is not None:
                target_client.send_system(f"Tu as été banni par {self.pseudo}.")
                target_client.alive = False
                try:
                    target_client.conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.send_system(f"{target_name} a été banni.")

        elif action == "unban":
            self.server.users.set_banned(target_name, False)
            self.send_system(f"{target_name} n'est plus banni.")

        elif action == "setmodo":
            self.server.users.set_role(target_name, "moderator")
            self.send_system(f"{target_name} est maintenant modérateur.")
            if target_client is not None:
                target_client.send_system(f"Tu es maintenant modérateur (promu par {self.pseudo}).")

        elif action == "remmodo":
            self.server.users.set_role(target_name, "user")
            self.send_system(f"{target_name} n'est plus modérateur.")
            if target_client is not None:
                target_client.send_system(f"Tu n'es plus modérateur.")

        elif action == "setadmin":
            self.server.users.set_role(target_name, "admin")
            self.send_system(f"{target_name} est maintenant administrateur.")
            if target_client is not None:
                target_client.send_system(f"Tu es maintenant administrateur (promu par {self.pseudo}).")

        elif action == "remadmin":
            self.server.users.set_role(target_name, "user")
            self.send_system(f"{target_name} n'est plus administrateur.")
            if target_client is not None:
                target_client.send_system("Tu n'es plus administrateur.")


class ChatServer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.users = UserStore(USERS_FILE)
        self.clients = {}       # pseudo -> ClientHandler
        self.rooms = {DEFAULT_ROOM: set()}  # room -> set(pseudo)
        self.room_passwords = {}  # room -> hash bcrypt (absent si salon public)
        self.lock = threading.Lock()
        for name, info in self.users.get_rooms().items():
            self.rooms.setdefault(name, set())
            password_hash = info.get("password_hash")
            if password_hash:
                self.room_passwords[name] = password_hash

    # ---------- Gestion des clients ----------
    def is_online(self, pseudo):
        with self.lock:
            return pseudo in self.clients

    def get_client(self, pseudo):
        with self.lock:
            return self.clients.get(pseudo)

    def register_client(self, client):
        with self.lock:
            self.clients[client.pseudo] = client

    def unregister_client(self, client):
        with self.lock:
            if self.clients.get(client.pseudo) is client:
                del self.clients[client.pseudo]
            if client.room and client.room in self.rooms:
                self.rooms[client.room].discard(client.pseudo)
        if client.pseudo and client.room:
            self.broadcast_room(
                client.room,
                {"type": "system", "text": f"{client.pseudo} a quitté le chat.", "ts": now_str()},
                exclude=client,
            )

    def rename_client(self, client, old_pseudo, new_pseudo):
        with self.lock:
            del self.clients[old_pseudo]
            self.clients[new_pseudo] = client
            if client.room in self.rooms:
                self.rooms[client.room].discard(old_pseudo)
                self.rooms[client.room].add(new_pseudo)
        self.users.rename(old_pseudo, new_pseudo)
        client.pseudo = new_pseudo

    # ---------- Gestion des salons ----------
    def room_exists(self, room):
        with self.lock:
            return room in self.rooms

    def create_room(self, room, password=""):
        with self.lock:
            if room in self.rooms:
                return False
            self.rooms[room] = set()
            password_hash = hash_password(password) if password else None
            if password_hash:
                self.room_passwords[room] = password_hash
        self.users.save_room(room, password_hash)
        return True

    def check_room_password(self, room, password):
        with self.lock:
            room_hash = self.room_passwords.get(room)
        if not room_hash:
            return True
        return verify_password(password, room_hash)

    def join_room(self, client, room, announce=False, notify_self=True):
        with self.lock:
            old_room = client.room
            if old_room and old_room in self.rooms:
                self.rooms[old_room].discard(client.pseudo)
            self.rooms.setdefault(room, set()).add(client.pseudo)
            client.room = room
        if notify_self:
            client.send({"type": "joined", "room": room})
        if announce:
            if old_room and old_room != room:
                self.broadcast_room(
                    old_room,
                    {"type": "system", "text": f"{client.pseudo} a quitté {old_room}.", "ts": now_str()},
                    exclude=client,
                )
            self.broadcast_room(
                room,
                {"type": "system", "text": f"{client.pseudo} a rejoint {room}.", "ts": now_str()},
                exclude=client,
            )

    def list_rooms(self):
        with self.lock:
            return [
                {"name": r, "count": len(members), "locked": r in self.room_passwords}
                for r, members in self.rooms.items()
            ]

    def list_users_in_room(self, room):
        with self.lock:
            members = list(self.rooms.get(room, set()))
        return [{"pseudo": p, "role": self.users.role_of(p)} for p in sorted(members)]

    # ---------- Diffusion ----------
    def broadcast_room(self, room, payload, exclude=None):
        with self.lock:
            members = list(self.rooms.get(room, set()))
        for pseudo in members:
            client = self.clients.get(pseudo)
            if client and client is not exclude:
                client.send(payload)

    def broadcast_all(self, payload):
        with self.lock:
            clients = list(self.clients.values())
        for client in clients:
            client.send(payload)

    def _setup_admin_password(self):
        """Definit le mot de passe admin bootstrap : via variable d'environnement,
        ou genere aleatoirement au tout premier lancement et stocke (hashe) dans
        users.json. Le mot de passe en clair n'est affiche qu'une seule fois."""
        env_password = os.environ.get("CHAT_ADMIN_PASSWORD")
        if env_password:
            self.users.set_admin_hash(hash_password(env_password))
            print("[Serveur] Mot de passe admin defini via CHAT_ADMIN_PASSWORD (non affiche).")
            return
        if self.users.get_admin_hash() is None:
            generated = secrets.token_urlsafe(9)
            self.users.set_admin_hash(hash_password(generated))
            print(f"[Serveur] Mot de passe admin genere : {generated}")
            print("[Serveur] Note-le : il ne sera plus jamais affiche (stocke hashe dans users.json).")
        else:
            print("[Serveur] Mot de passe admin deja configure (voir le premier lancement, ou definis CHAT_ADMIN_PASSWORD pour le changer).")

    # ---------- Boucle principale ----------
    def serve_forever(self):
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((self.host, self.port))
        server_sock.listen(50)
        print(f"[Serveur] En écoute sur {self.host}:{self.port}")
        self._setup_admin_password()
        try:
            while True:
                conn, addr = server_sock.accept()
                ClientHandler(self, conn, addr).start()
        except KeyboardInterrupt:
            print("\n[Serveur] Arrêt demandé.")
        finally:
            server_sock.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serveur de chat TCP")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    ChatServer(args.host, args.port).serve_forever()
