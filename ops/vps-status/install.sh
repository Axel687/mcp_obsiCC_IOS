#!/usr/bin/env bash
# Installe le relevé d'état du VPS (lecture seule) et sa clé SSH bridée.
#
# À lancer une fois, en root, sur le VPS :
#   curl -fsSL https://raw.githubusercontent.com/Axel687/mcp_obsiCC_IOS/<commit>/ops/vps-status/install.sh | bash -s <commit>
#
# Ce que fait le script :
#   1. copie vps-status.py (version du <commit> donné) dans /usr/local/sbin/vps-status ;
#   2. le lance une fois pour vérifier qu'il marche ;
#   3. crée une nouvelle clé SSH et l'ajoute à /root/.ssh/authorized_keys avec
#      restrict,command="/usr/local/sbin/vps-status" : cette clé ne peut rien faire
#      d'autre que lancer le relevé (pas de terminal, pas de redirection) ;
#   4. affiche le bloc à coller dans le secret VPS_STATUS_SSH du dépôt du vault
#      (ligne known_hosts + clé privée), puis efface la clé privée du VPS.
# Relancer le script remplace l'ancienne clé (même commentaire) par une nouvelle.
set -euo pipefail

REF="${1:-}"
[ -n "$REF" ] || { echo "Usage : install.sh <commit>" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "À lancer en root." >&2; exit 1; }

COMMENT="vps-status-github"
TARGET="/usr/local/sbin/vps-status"
AUTH="/root/.ssh/authorized_keys"
URL="https://raw.githubusercontent.com/Axel687/mcp_obsiCC_IOS/${REF}/ops/vps-status/vps-status.py"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

echo "1/4 Téléchargement du script de relevé (${REF})"
curl -fsSL "$URL" -o "$work/vps-status.py"
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$work/vps-status.py"
install -o root -g root -m 755 "$work/vps-status.py" "$TARGET"

echo "2/4 Essai du relevé"
"$TARGET" | python3 -c '
import json, sys
d = json.load(sys.stdin)
c = d.get("docker", {}).get("containers") or []
print("    OK : %s, %d conteneurs, %d ports en écoute" % (
    d["system"].get("hostname"), len(c), len(d.get("listening") or [])))
'

echo "3/4 Création de la clé SSH bridée"
ssh-keygen -q -t ed25519 -N "" -C "$COMMENT" -f "$work/key"
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch "$AUTH"
cp -p "$AUTH" "$AUTH.bak-vps-status"
{
  grep -v " ${COMMENT}\$" "$AUTH" || true
  printf 'restrict,command="%s" %s\n' "$TARGET" "$(cat "$work/key.pub")"
} > "$work/authorized_keys"
install -o root -g root -m 600 "$work/authorized_keys" "$AUTH"

echo "4/4 Secret à copier"
ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
[ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
[ -n "$ip" ] || { echo "Adresse IP du VPS introuvable." >&2; exit 1; }
hostkey=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)

cat <<EOF

Copie TOUT le bloc entre les deux lignes de tirets (sans les tirets), et colle-le
dans un nouveau secret nommé VPS_STATUS_SSH du dépôt Axel687/Obsidian_vault :
https://github.com/Axel687/Obsidian_vault/settings/secrets/actions/new
--------------------------------------------------------------------------------
${ip} ${hostkey}
$(cat "$work/key")
--------------------------------------------------------------------------------
La clé privée est effacée du VPS à la fin du script. Si tu perds ce bloc, relance
simplement la même commande : elle remplace la clé par une nouvelle.
Sauvegarde de l'ancien fichier : ${AUTH}.bak-vps-status
EOF
