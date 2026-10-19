"""Ligne à vue (profil du terrain) entre un point et les répéteurs.

Altitudes du terrain : modèle numérique Copernicus GLO-90 servi par l'API gratuite Open-Meteo
(https://open-meteo.com/en/docs/elevation-api), 100 points par profil. Une connexion Internet est nécessaire.
Le calcul tient compte de la courbure de la Terre (réfraction standard k = 4/3) et de la 1re zone de Fresnel
à 869,5 MHz. Le résultat est une estimation : le modèle de terrain ne connaît ni les bâtiments ni la végétation.
"""
from __future__ import annotations

import json
import math
import urllib.request

R_TERRE = 6371000.0
K = 4 / 3
FREQ_MHZ = 869.525
LAMBDA = 299.792458 / FREQ_MHZ  # mètres
N_POINTS = 100
URL = "https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}"

_cache: dict[tuple[float, float], float] = {}


def distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_TERRE * math.asin(math.sqrt(min(1.0, max(0.0, a))))


def altitudes(points: list[tuple[float, float]]) -> list[float]:
    cles = [(round(la, 4), round(lo, 4)) for la, lo in points]
    manquants = [c for c in dict.fromkeys(cles) if c not in _cache]
    for i in range(0, len(manquants), 100):
        lot = manquants[i:i + 100]
        url = URL.format(lat=",".join(f"{a:.4f}" for a, _ in lot), lon=",".join(f"{b:.4f}" for _, b in lot))
        req = urllib.request.Request(url, headers={"User-Agent": "Supervision-ADRASEC06"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
        elev = data.get("elevation") or []
        if len(elev) != len(lot):
            raise RuntimeError("réponse incomplète du service d'altitudes")
        for c, e in zip(lot, elev):
            _cache[c] = float(e if e is not None else 0)
    return [_cache[c] for c in cles]


def profil(lat1, lon1, h1, lat2, lon2, h2) -> dict:
    """Profil du point 1 (antenne à h1 m du sol) vers le point 2 (antenne à h2 m)."""
    D = distance_m(lat1, lon1, lat2, lon2)
    pts = [(lat1 + (lat2 - lat1) * i / (N_POINTS - 1), lon1 + (lon2 - lon1) * i / (N_POINTS - 1)) for i in range(N_POINTS)]
    sol = altitudes(pts)
    a1, a2 = sol[0] + h1, sol[-1] + h2
    re = K * R_TERRE
    dist, terrain, ligne, fresnel = [], [], [], []
    pire, pire_i = math.inf, None
    for i, e in enumerate(sol):
        d = D * i / (N_POINTS - 1)
        reste = max(0.0, D - d)  # évite un nombre négatif minuscule au dernier point (arrondi)
        bombe = d * reste / (2 * re)
        t = e + bombe
        l = a1 + (a2 - a1) * d / D if D else a1
        r1 = math.sqrt(max(0.0, LAMBDA * d * reste / D)) if D else 0
        dist.append(round(d / 1000, 3))
        terrain.append(round(t, 1))
        ligne.append(round(l, 1))
        fresnel.append(round(l - 0.6 * r1, 1))
        if 0 < i < N_POINTS - 1 and r1 > 0:
            ratio = (l - t) / r1
            if ratio < pire:
                pire, pire_i = ratio, i
    if pire_i is None:
        verdict, niveau = "Trop proche pour un profil", "ok"
    elif pire >= 0.6:
        verdict, niveau = "Ligne à vue dégagée", "ok"
    elif pire >= 0:
        verdict, niveau = "Ligne à vue, mais zone de Fresnel partiellement obstruée (signal affaibli)", "attention"
    else:
        verdict, niveau = "Pas de ligne à vue : relief entre les deux points", "critique"
    obstacle = None
    if pire_i is not None and pire < 0.6:
        obstacle = {"km": dist[pire_i], "altitude": round(sol[pire_i]), "lat": round(pts[pire_i][0], 5),
                    "lon": round(pts[pire_i][1], 5), "manque": round(terrain[pire_i] - fresnel[pire_i], 1)}
    fspl = 20 * math.log10(max(D, 1) / 1000) + 20 * math.log10(FREQ_MHZ) + 32.44 if D > 0 else 0
    return {"distanceKm": round(D / 1000, 2), "altDepart": round(sol[0]), "altArrivee": round(sol[-1]),
            "verdict": verdict, "niveau": niveau, "degagement": None if pire_i is None else round(pire, 2),
            "obstacle": obstacle, "affaiblissement": round(fspl, 1),
            "dist": dist, "terrain": terrain, "ligne": ligne, "fresnel": fresnel}
