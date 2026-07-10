# 💬 Chat TCP — Projet Python

Chat multi-clients en Python pur : sockets TCP + `threading`, interface graphique
Tkinter (style terminal 🖥️🟢). Aucune dépendance externe obligatoire (bcrypt est
utilisé si disponible, sinon un repli PBKDF2-SHA256 intégré prend le relais
automatiquement).

## 🔑 Mot de passe admin

```
/admin admin123
```
À taper une fois connecté, dans le champ `>` du client. Stocké **hashé** (bcrypt)
dans `users.json`, jamais en clair dans le code. Modifiable avant de lancer le
serveur :
```
set CHAT_ADMIN_PASSWORD=monmotdepasse
python server.py
```
⚠️ 3 tentatives ratées = déconnexion automatique (anti-bruteforce).

## 📁 Fichiers du projet

Seulement **2 fichiers** de code, totalement autonomes (aucun ne dépend de l'autre) :

| Fichier | Rôle |
|---|---|
| 🖧 `server.py` | Le serveur : connexions, salons, rôles, modération, sécurité. |
| 🪟 `client.py` | Le client graphique (Tkinter, style terminal noir/vert). |
| 🗃️ `users.json` | Généré automatiquement (pseudos, rôles, salons, mdp admin). Pas besoin d'y toucher. |

## 🚀 Lancer le projet

**1. Démarrer le serveur** (une seule fois, dans un terminal) :
```
python server.py
```
La console affiche le port d'écoute.

**2. Démarrer un ou plusieurs clients** (autant de fois que de personnes voulues,
dans d'autres terminaux) :
```
python client.py
```
Une fenêtre s'ouvre : tape un pseudo, clique `[ connecter ]`.

Options possibles pour le serveur :
```
python server.py --host 0.0.0.0 --port 5555
```

## ✅ Guide de test — à suivre dans l'ordre

Ouvre 2 ou 3 fenêtres `client.py` en parallèle pour tester le multi-clients.

| # | Étape | Action | Résultat attendu |
|---|---|---|---|
| 1 | 🔌 Connexion | Ouvre un client, pseudo `Alice` | Message de bienvenue + astuces affichées |
| 2 | 👥 Multi-clients | Ouvre un 2e client, pseudo `Bob` | Alice voit "Bob a rejoint general" |
| 3 | 💬 Message public | Bob tape `salut` | Alice le voit apparaître dans le chat |
| 4 | ✏️ Changer de pseudo | Bob tape `/nick Bobby` | Les deux voient le changement en direct |
| 5 | ✉️ Message privé | Alice tape `/msg Bobby coucou` | Seul Bob reçoit le message (couleur différente) |
| 6 | 🕒 Heure | `/time` | Affiche la date/heure du serveur |
| 7 | 📶 Ping | `/ping` | Affiche la latence en ms |
| 8 | 🔒 Créer un salon | Alice tape `/create secret motdepasse` | Salon créé, marqué `[protege]`, Alice dedans |
| 9 | ❌ Mauvais mdp | Bob clique sur `#secret` puis mauvais mdp | Refusé : "Mot de passe du salon incorrect" |
| 10 | ✅ Bon mdp | Bob clique sur `#secret`, bon mdp | Accepté, Bob rejoint le salon |
| 11 | 🚪 Quitter un salon | `/leave` | Retour au salon `general` |
| 12 | 🔌❌ Déconnexion propre | Ferme la fenêtre de Bob | Alice voit "Bob a quitté le chat", pas de crash serveur |
| 13 | 👑 Devenir admin | Alice tape `/admin admin123` | "Tu es maintenant administrateur." + barre d'outils admin apparaît |
| 14 | 👢 Kick | Alice clique `[kick]` (ou tape `/kick Bob`) | Bob est déconnecté de force |
| 15 | 🚫 Ban | `[ban]` sur Bob, puis Bob retente de se connecter | Refusé : "Ce pseudo est banni" |
| 16 | 🔇 Mute | `[mute]` puis la personne tape un message | Reçoit "Tu es muet(te) sur ce serveur." |
| 17 | ⭐ Promotion | `[+modo]` ou `[+admin]` sur un pseudo | La personne reçoit la confirmation de son nouveau rôle |
| 18 | 🧹 `/clear` | N'importe qui tape `/clear` | Efface l'écran **local uniquement** |
| 19 | ⏳ Timeout | Laisse un client inactif 5 minutes | Déconnexion automatique avec message explicite |
| 20 | 💾 Persistance | Redémarre le serveur | Rôles, bans **et salons créés** sont conservés |
| 21 | 🌊 Anti-flood | Envoie rapidement 8-10 messages d'affilée | Avertissement, puis déconnexion si ça continue |
| 22 | 🛡️ Anti-bruteforce | Tape 3x un mauvais mdp `/admin` | Déconnexion automatique |
| 23 | 🧪 Robustesse | Envoie un message vide, ou un pseudo invalide (1 caractère) | Erreur claire, pas de crash serveur |

## ✨ Fonctionnalités couvertes (cahier des charges)

- ✅ Connexion client/serveur, échange de messages
- ✅ Plusieurs clients simultanés (un thread par client)
- ✅ Déconnexion d'un client sans crash des autres (`try/except` isolé par thread)
- ✅ Choix du pseudo, stocké côté serveur dans `users.json`
- ✅ Changement de pseudo (`/nick`)
- ✅ Message privé (`/msg`)
- ✅ `/time` et `/ping` (latence réelle mesurée en aller-retour)
- ✅ Déconnexion automatique après 5 minutes d'inactivité
- ✅ `/clear` (local au client)
- ✅ Rôles `user` / `moderator` / `admin`, persistés par pseudo, avec **barre
  d'outils dédiée dans l'interface** qui apparaît selon le rôle
- ✅ Commandes de rôles : `kick`, `mute`/`unmute`, `ban`/`unban`,
  `setmodo`/`remmodo`, `setadmin`/`remadmin` (boutons + commandes texte)
- 🔐 Cybersécurité : validation stricte des entrées, anti-flood, mots de passe de
  salon **et** mot de passe admin hashés en **bcrypt** (jamais stockés en clair),
  verrouillage anti-bruteforce sur `/admin` (déconnexion après 3 échecs),
  écritures JSON atomiques, aucune exception client ne peut planter le serveur
- ✅ Salons : créer (`/create`), rejoindre (1 clic ou `/join`), quitter (`/leave`),
  **persistés dans `users.json`** (survivent aux redémarrages du serveur)
- 🎨 Bonus qualité : couleurs par rôle/type de message, horodatage de chaque
  ligne, messages d'erreur explicites, panneau de commandes toujours visible,
  astuces affichées à la connexion

## 📌 Limites connues

- Les mutes sont en mémoire (remis à zéro si le serveur redémarre) ; rôles, bans
  et salons, eux, sont persistés dans `users.json`.
- Pas de suppression de salon (une fois créé, il reste tant qu'il n'est pas
  effacé manuellement de `users.json`).
- Le mot de passe de salon protège l'accès mais le transport réseau n'est pas
  chiffré (pas de TLS) — à ajouter avec le module `ssl` pour aller plus loin.
