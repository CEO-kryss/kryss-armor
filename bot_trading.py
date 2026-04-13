import time
import json
import logging
import websocket
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from threading import Thread, Lock

# =====================================================
# 🛡️ KRYSS-ARMOR V5.1 — HYPERLIQUID PAPER TRADING
# Prix réels : Binance WebSocket (accessible partout)
# Capital    : SIMULÉ (paper trading)
# Frais sim. : Hyperliquid Maker 0.015% | Taker 0.045%
# Stratégie  : HFT Trailing p_creux / p_sommet
# =====================================================

# --- LOGGING (erreurs dans fichier + console) ---
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("kryss_armor_errors.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("KRYSS-ARMOR")

# =====================================================
# ⚙️  CONFIGURATION — modifie ici seulement
# =====================================================
CAPITAL_DEPART_USDT = Decimal('6.50')     # ≈ 4000 FCFA
LEVIER              = 2
SYMBOL              = "BNB"               # Coin sur Hyperliquid Perps

# Frais Hyperliquid tier de base (<1M$ volume)
FRAIS_MAKER         = Decimal('0.00015')  # 0.015% — entrée + sortie profit
FRAIS_TAKER         = Decimal('0.00045')  # 0.045% — sortie stop-loss (market)

# =====================================================
# 📐 STRATÉGIE HFT — TRAILING p_creux / p_sommet
# =====================================================
#
#  PHASE ACHAT — Trailing Buy
#  ─────────────────────────
#  Chaque prix reçu :
#    → si prix < p_creux    : on met à jour p_creux (nouveau bas)
#    → seuil_achat = p_creux × (1 + HAUSSE_ACHAT_CREUX)
#    → si prix >= seuil_achat : ACHAT MAKER (rebond confirmé)
#
#  PHASE VENTE — Trailing Sell
#  ───────────────────────────
#  Chaque prix reçu :
#    → si prix > p_sommet   : on met à jour p_sommet (nouveau haut)
#    → seuil_vente = p_sommet × (1 - BAISSE_VENTE_SOMMET)
#
#  SORTIE PROFIT (MAKER) si toutes ces conditions :
#    ✅ prix <= seuil_vente  (retournement depuis le sommet)
#    ✅ profit_net > frais_totaux_complets
#    ✅ profit_net >= PROFIT_MIN_NET_USDT
#
#  SORTIE STOP-LOSS (TAKER) si :
#    ✅ prix <= p_achat × (1 - STOP_LOSS_POURCENT)
#    → Exécution immédiate marché, garantit la sortie
#
# =====================================================

HAUSSE_ACHAT_CREUX  = Decimal('0.00003')  # 0.003% rebond minimum pour acheter
BAISSE_VENTE_SOMMET = Decimal('0.00003')  # 0.003% repli depuis sommet pour vendre
PROFIT_MIN_NET_USDT = Decimal('0.010')    # Profit net minimum requis
STOP_LOSS_POURCENT  = Decimal('0.006')    # Stop-loss à -0.6% du prix d'achat
DELAI_SECURITE      = 300                 # 5 min de cooldown après un stop-loss

# =====================================================
# 🔒 ÉTAT GLOBAL — thread-safe
# =====================================================
verrou              = Lock()
SOLDE_USDT          = CAPITAL_DEPART_USDT
prix_recu           = 0
_reconnexion_active = False

# Mémoire de la position ouverte
mem = {
    "qte"            : Decimal('0.0'),
    "p_achat"        : Decimal('0.0'),
    "p_sommet"       : Decimal('0.0'),
    "p_creux"        : Decimal('inf'),
    "last_crash_time": 0.0,
    "frais_entree"   : Decimal('0.0'),
    "capital_engage" : Decimal('0.0'),
}

# Stats globales
stats = {
    "victoires"   : 0,
    "defaites"    : 0,
    "total_profit": Decimal('0.0'),
    "total_frais" : Decimal('0.0'),
    "trades"      : [],
}

stats_session = {
    "debut"          : time.time(),
    "trades_gagnants": 0,
    "trades_perdants": 0,
    "profit_cumule"  : Decimal('0.0'),
    "frais_cumules"  : Decimal('0.0'),
    "volume_total"   : Decimal('0.0'),
    "plus_gros_gain" : Decimal('0.0'),
    "plus_grosse_perte": Decimal('0.0'),
}


# =====================================================
# 🔧 UTILITAIRES
# =====================================================

def sauvegarder_position():
    """Sauvegarde la position ouverte dans un fichier JSON"""
    if mem["qte"] > Decimal('0.0'):
        data = {
            "qte"            : str(mem["qte"]),
            "p_achat"        : str(mem["p_achat"]),
            "p_sommet"       : str(mem["p_sommet"]),
            "p_creux"        : str(mem["p_creux"]),
            "frais_entree"   : str(mem["frais_entree"]),
            "capital_engage" : str(mem["capital_engage"]),
            "last_crash_time": mem["last_crash_time"],
            "solde_usdt"     : str(SOLDE_USDT),
        }
        with open("position_sauvegarde.json", "w") as f:
            json.dump(data, f, indent=2)

def restaurer_position():
    """Restaure la position depuis le fichier JSON si elle existe"""
    global SOLDE_USDT
    try:
        with open("position_sauvegarde.json", "r") as f:
            data = json.load(f)
        mem["qte"]             = Decimal(data["qte"])
        mem["p_achat"]         = Decimal(data["p_achat"])
        mem["p_sommet"]        = Decimal(data["p_sommet"])
        mem["p_creux"]         = Decimal(data["p_creux"])
        mem["frais_entree"]    = Decimal(data["frais_entree"])
        mem["capital_engage"]  = Decimal(data["capital_engage"])
        mem["last_crash_time"] = data["last_crash_time"]
        SOLDE_USDT             = Decimal(data["solde_usdt"])
        if mem["qte"] > Decimal('0.0'):
            print(f"♻️  Position restaurée ! {mem['qte']} {SYMBOL} achetés à {mem['p_achat']}$")
    except FileNotFoundError:
        pass  # Pas de position sauvegardée, on repart de zéro
    except Exception as e:
        logger.error(f"Erreur restauration position : {e}")

def arrondir_qte(v):
    """3 décimales vers le bas — standard Hyperliquid"""
    return v.quantize(Decimal('0.001'), rounding=ROUND_DOWN)

def arrondir_usdt(v):
    return v.quantize(Decimal('0.00001'), rounding=ROUND_HALF_UP)

def journaliser(type_action, prix, profit_net=Decimal('0.0'), frais=Decimal('0.0')):
    global stats, stats_session

    stats["trades"].append({
        "date"  : time.strftime('%Y-%m-%d %H:%M:%S'),
        "action": type_action,
        "prix"  : float(prix),
        "profit": float(profit_net),
        "frais" : float(frais),
        "solde" : float(SOLDE_USDT),
    })

    stats["total_frais"]           += frais
    stats_session["frais_cumules"] += frais
    stats_session["profit_cumule"] += profit_net

    if "PROFIT" in type_action:
        stats["victoires"]                += 1
        stats_session["trades_gagnants"]  += 1
        stats["total_profit"]             += profit_net
        if profit_net > stats_session["plus_gros_gain"]:
            stats_session["plus_gros_gain"] = profit_net

    elif "STOP" in type_action:
        stats["defaites"]                 += 1
        stats_session["trades_perdants"]  += 1
        stats["total_profit"]             += profit_net
        if profit_net < stats_session["plus_grosse_perte"]:
            stats_session["plus_grosse_perte"] = profit_net


# =====================================================
# 💰 CALCUL PNL — FORMULE EXACTE
# =====================================================
#
#  À l'ACHAT :
#    montant_investi = capital_engage × LEVIER
#    frais_entree    = montant_investi × FRAIS_MAKER
#    qte             = (montant_investi - frais_entree) / p_achat
#
#  À la VENTE :
#    valeur_brute    = qte × p_actuel
#    frais_sortie    = valeur_brute × FRAIS_MAKER ou FRAIS_TAKER
#    valeur_nette    = valeur_brute - frais_sortie
#    cout_position   = qte × p_achat   (remboursement du "prêt" du levier)
#    profit_reel_net = valeur_nette - cout_position
#
#  ⚠️ Les frais_entree sont déjà absorbés dans la qte réduite.
#     Ne pas les soustraire une deuxième fois !
#
# =====================================================

def calculer_pnl(prix_actuel, mode="PROFIT"):
    """
    Calcule le PnL de la position ouverte.
    mode = "PROFIT" → frais MAKER (sortie limite)
    mode = "STOP"   → frais TAKER (sortie marché)
    Retourne (valeur_nette, frais_sortie, profit_reel_net)
    """
    taux         = FRAIS_MAKER if mode == "PROFIT" else FRAIS_TAKER
    valeur_brute = mem["qte"] * prix_actuel
    frais_sortie = valeur_brute * taux
    valeur_nette = valeur_brute - frais_sortie
    cout_pos     = mem["qte"] * mem["p_achat"]
    profit       = valeur_nette - cout_pos
    return arrondir_usdt(valeur_nette), arrondir_usdt(frais_sortie), arrondir_usdt(profit)


# =====================================================
# ⚙️  LOGIQUE PRINCIPALE
# =====================================================

def gerer_donnees(prix_actuel):
    global SOLDE_USDT, prix_recu

    with verrou:
        prix_recu += 1

        # ──────────────────────────────────────────
        # PHASE 1 — ACHAT (Trailing Buy sur p_creux)
        # ──────────────────────────────────────────
        if mem["qte"] == Decimal('0.0'):

            # Cooldown post stop-loss
            temps_ecoule = time.time() - mem["last_crash_time"]
            if temps_ecoule < DELAI_SECURITE:
                restant = int(DELAI_SECURITE - temps_ecoule)
                print(f"⏳ Cooldown : {restant}s | Prix: {prix_actuel}$              ", end="\r")
                return

            # Mise à jour du creux : chaque nouveau bas est retenu
            if prix_actuel < mem["p_creux"]:
                mem["p_creux"] = prix_actuel

            # Seuil de rebond = creux + HAUSSE_ACHAT_CREUX
            seuil_achat = mem["p_creux"] * (Decimal('1') + HAUSSE_ACHAT_CREUX)

            print(
                f"👁️  Surveillance | Prix: {prix_actuel}$ | "
                f"p_creux: {mem['p_creux']}$ | "
                f"Seuil achat: {seuil_achat:.5f}$      ",
                end="\r"
            )

            # ✅ Rebond confirmé → ACHAT MAKER
            if prix_actuel >= seuil_achat:
                capital_engage  = SOLDE_USDT
                montant_investi = capital_engage * LEVIER
                frais_entree    = montant_investi * FRAIS_MAKER
                qte_brute       = (montant_investi - frais_entree) / prix_actuel
                qte_valide      = arrondir_qte(qte_brute)

                if qte_valide <= Decimal('0.0'):
                    print("\n⚠️  Solde insuffisant pour ouvrir une position !")
                    return

                mem.update({
                    "qte"           : qte_valide,
                    "p_achat"       : prix_actuel,
                    "p_sommet"      : prix_actuel,
                    "frais_entree"  : frais_entree,
                    "capital_engage": capital_engage,
                })
                SOLDE_USDT = Decimal('0.0')

                journaliser("ACHAT [MAKER]", prix_actuel, frais=frais_entree)
                stats_session["volume_total"] += montant_investi
                sauvegarder_position()  # ✅ Sauvegarde immédiate après achat

                print(f"\n{'═'*64}")
                print(f"🚀 [PAPER ACHAT — MAKER x{LEVIER}]  {time.strftime('%H:%M:%S')}")
                print(f"   Prix entrée      : {prix_actuel}$")
                print(f"   p_creux retenu   : {mem['p_creux']}$")
                print(f"   Quantité         : {qte_valide} {SYMBOL}")
                print(f"   Capital engagé   : {capital_engage:.5f}$ × {LEVIER} = {montant_investi:.5f}$")
                print(f"   Frais entrée     : {frais_entree:.6f}$ (MAKER {float(FRAIS_MAKER)*100:.3f}%)")
                print(f"{'═'*64}\n")

        # ─────────────────────────────────────────────────
        # PHASE 2 — VENTE (Trailing Sell sur p_sommet)
        # ─────────────────────────────────────────────────
        elif mem["qte"] > Decimal('0.0'):

            # Mise à jour du sommet : chaque nouveau haut est retenu
            if prix_actuel > mem["p_sommet"]:
                mem["p_sommet"] = prix_actuel

            # PnL live (mode TAKER pour être conservateur à l'affichage)
            _, frais_live, pnl_live = calculer_pnl(prix_actuel, "STOP")
            frais_totaux_live = frais_live + mem["frais_entree"]

            pnl_emoji = "📈" if pnl_live > 0 else "📉"
            print(
                f"{pnl_emoji} {time.strftime('%H:%M:%S')} | "
                f"Prix: {prix_actuel}$ | "
                f"p_sommet: {mem['p_sommet']}$ | "
                f"PnL: {pnl_live:+.5f}$ | "
                f"Frais≈{frais_totaux_live:.5f}$      ",
                end="\r"
            )

            # ── Condition STOP-LOSS (TAKER — sortie marché rapide) ──
            seuil_stop = mem["p_achat"] * (Decimal('1') - STOP_LOSS_POURCENT)
            est_stop_loss = prix_actuel <= seuil_stop

            # ── Condition TRAILING PROFIT (TAKER — sortie marché garantie) ──
            seuil_vente = mem["p_sommet"] * (Decimal('1') - BAISSE_VENTE_SOMMET)
            val_nette_p, frais_sortie_p, pnl_profit = calculer_pnl(prix_actuel, "STOP")  # TAKER pour les deux sorties
            frais_totaux_profit = frais_sortie_p + mem["frais_entree"]

            est_trailing_profit = (
                prix_actuel <= seuil_vente            # repli depuis le sommet confirmé
                and pnl_profit > frais_totaux_profit  # profit couvre tous les frais
                and pnl_profit >= PROFIT_MIN_NET_USDT # seuil minimum atteint
            )

            # ── EXÉCUTION DE LA SORTIE ──
            if est_stop_loss or est_trailing_profit:

                if est_stop_loss:
                    type_vente      = "STOP-LOSS [TAKER]"
                    val_nette, frais_sortie, profit_reel = calculer_pnl(prix_actuel, "STOP")
                    frais_totaux    = frais_sortie + mem["frais_entree"]
                    emoji           = "🛑"
                else:
                    type_vente      = "PROFIT [TAKER]"   # ✅ Sortie marché garantie
                    val_nette       = val_nette_p
                    frais_sortie    = frais_sortie_p
                    profit_reel     = pnl_profit
                    frais_totaux    = frais_totaux_profit
                    emoji           = "💰"

                # Récupération du solde simulé
                # Récupération du solde simulé (Ancien solde + profit net réel)
                SOLDE_USDT = mem["capital_engage"] + profit_reel

                journaliser(f"VENTE {type_vente}", prix_actuel, profit_reel, frais_sortie)

                total   = stats['victoires'] + stats['defaites']
                winrate = (stats['victoires'] / total * 100) if total > 0 else 0

                print(f"\n{'═'*64}")
                print(f"{emoji} [PAPER {type_vente}]  {time.strftime('%H:%M:%S')}")
                print(f"   Prix sortie       : {prix_actuel}$")
                print(f"   Prix achat        : {mem['p_achat']}$")
                print(f"   p_sommet atteint  : {mem['p_sommet']}$")
                print(f"   Quantité          : {mem['qte']} {SYMBOL}")
                print(f"   Valeur brute      : {mem['qte'] * prix_actuel:.5f}$")
                print(f"   ─────────────────────────────────────────────")
                print(f"   Frais entrée      : {mem['frais_entree']:.6f}$ (MAKER)")
                print(f"   Frais sortie      : {frais_sortie:.6f}$ (TAKER — {'stop' if est_stop_loss else 'profit'})")
                print(f"   Frais totaux      : {frais_totaux:.6f}$")
                print(f"   ─────────────────────────────────────────────")
                print(f"   Profit net        : {profit_reel:+.5f}$")
                print(f"   Solde simulé      : {SOLDE_USDT:.5f}$")
                print(f"   Gain cumulé       : {stats['total_profit']:+.5f}$")
                print(f"   Win Rate          : {winrate:.1f}%  ({stats['victoires']}V / {stats['defaites']}D)")
                print(f"{'═'*64}\n")

                if est_stop_loss:
                    mem["last_crash_time"] = time.time()

                # Reset complet de la position
                mem["qte"]            = Decimal('0.0')
                mem["p_achat"]        = Decimal('0.0')
                mem["p_sommet"]       = Decimal('0.0')
                mem["p_creux"]        = Decimal('inf')
                mem["frais_entree"]   = Decimal('0.0')
                mem["capital_engage"] = Decimal('0.0')

                # ✅ Position fermée → supprimer la sauvegarde
                import os
                try:
                    os.remove("position_sauvegarde.json")
                except FileNotFoundError:
                    pass


# =====================================================
# 📡  WEBSOCKET BINANCE — Prix réels BNB/USDT
#     (Hyperliquid inaccessible depuis certaines régions)
#     Stratégie + frais Hyperliquid simulés intacts.
# =====================================================

def _ws_url():
    return f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}usdt@trade"

def on_message(ws, message):
    """
    Format Binance @trade :
    { "e": "trade", "p": "612.50", "q": "0.01", ... }
    On extrait simplement le prix 'p'.
    """
    try:
        data = json.loads(message)
        px   = data.get("p")
        if px is None:
            return
        gerer_donnees(Decimal(str(px)))
    except (KeyError, ValueError) as e:
        logger.warning(f"Message invalide : {e} | raw: {message[:100]}")
    except Exception as e:
        logger.error(f"Erreur on_message : {e}", exc_info=True)

def on_error(ws, error):
    logger.error(f"WebSocket erreur : {error}")

def on_close(ws, code, msg):
    """Reconnexion via thread dédié — pas de récursion infinie"""
    global _reconnexion_active
    print(f"\n🔌 WebSocket fermé (code={code}). Reconnexion dans 5s...")
    if not _reconnexion_active:
        _reconnexion_active = True
        Thread(target=_reconnexion_thread, daemon=True).start()

def _reconnexion_thread():
    global _reconnexion_active
    time.sleep(5)
    _reconnexion_active = False
    lancer_websocket()

def on_open(ws):
    print(f"✅ Connecté à Binance WebSocket !")
    print(f"📡 Flux trades {SYMBOL}/USDT en temps réel (prix Binance)...\n")

def lancer_websocket():
    ws = websocket.WebSocketApp(
        _ws_url(),
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        on_open=on_open,
    )
    ws.run_forever(
        ping_interval=20,  # ping toutes les 20s au lieu de 30s
        ping_timeout=15,   # timeout plus généreux pour connexion lente
    )


# =====================================================
# 📜  RAPPORT FINAL
# =====================================================

def afficher_rapport():
    profit_total = SOLDE_USDT - CAPITAL_DEPART_USDT
    total        = stats['victoires'] + stats['defaites']
    winrate      = (stats['victoires'] / total * 100) if total > 0 else 0
    duree        = int(time.time() - stats_session["debut"])
    h, m, s      = duree // 3600, (duree % 3600) // 60, duree % 60

    print(f"\n{'═'*64}")
    print(f"       📜  RAPPORT FINAL — KRYSS-ARMOR V5.1 HYPERLIQUID")
    print(f"{'═'*64}")
    print(f"⏱️   Durée session       : {h:02d}h {m:02d}m {s:02d}s")
    print(f"💰  Capital départ       : {CAPITAL_DEPART_USDT}$  (≈{float(CAPITAL_DEPART_USDT)*600:.0f} FCFA)")
    print(f"💰  Solde final          : {SOLDE_USDT:.5f}$")
    print(f"📊  Profit total         : {profit_total:+.5f}$")
    print(f"📊  Profit en FCFA       : {float(profit_total)*600:+.1f} FCFA")
    print(f"💸  Frais payés (sim.)   : {stats['total_frais']:.6f}$")
    print(f"─────────────────────────────────────────────────────────────")
    print(f"🏆  Victoires            : {stats['victoires']}")
    print(f"❌  Défaites             : {stats['defaites']}")
    print(f"🎯  Win Rate             : {winrate:.1f}%")
    print(f"💚  Plus gros gain       : {stats_session['plus_gros_gain']:+.5f}$")
    print(f"🔴  Plus grosse perte    : {stats_session['plus_grosse_perte']:+.5f}$")
    print(f"📦  Volume total simulé  : {stats_session['volume_total']:.3f}$")
    print(f"⚡  Prix analysés        : {prix_recu}")
    print(f"{'═'*64}")

    # Sauvegarde JSON des trades
    if stats["trades"]:
        fname = f"paper_trades_{time.strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, "w") as f:
            json.dump(stats["trades"], f, indent=2)
        print(f"💾  Trades sauvegardés  → {fname}")
    print()


# =====================================================
# ▶️   LANCEMENT
# =====================================================

if __name__ == "__main__":
    print(f"{'═'*64}")
    print(f"  🛡️  KRYSS-ARMOR V5.1 — HYPERLIQUID PAPER TRADING")
    print(f"{'═'*64}")
    print(f"💰  Capital simulé       : {CAPITAL_DEPART_USDT}$  (≈{float(CAPITAL_DEPART_USDT)*600:.0f} FCFA)")
    print(f"📈  Symbole              : {SYMBOL}/USDT")
    print(f"⚡  Levier               : x{LEVIER}")
    print(f"📥  Entrée               : MAKER  ({float(FRAIS_MAKER)*100:.3f}%)  — ordre limite")
    print(f"📤  Sortie profit        : TAKER  ({float(FRAIS_TAKER)*100:.3f}%)  — ordre marché garanti")
    print(f"🚨  Sortie stop-loss     : TAKER  ({float(FRAIS_TAKER)*100:.3f}%)  — ordre marché garanti")
    print(f"🎯  Frais total/trade    : {float(FRAIS_MAKER+FRAIS_TAKER)*100:.3f}% (Maker entrée + Taker sortie)")
    print(f"📐  Stratégie           : Trailing p_creux / p_sommet")
    print(f"🔄  Mode                 : PAPER TRADING — prix réels Binance WS")
    print(f"💡  Frais simulés        : Hyperliquid (paper uniquement)")
    print(f"{'═'*64}")
    print(f"▶️   Connexion à Hyperliquid WebSocket...\n")

    # ✅ Restaurer position si bot redémarré pendant un trade
    restaurer_position()

    ws_thread = Thread(target=lancer_websocket, daemon=True)
    ws_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        afficher_rapport()
        print("✅  Bot arrêté proprement !")
