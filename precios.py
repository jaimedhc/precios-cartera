#!/usr/bin/env python3
"""Actualiza los precios de la cartera.

Lee activos.json y, para cada activo, prueba sus fuentes en orden hasta que
una funciona. Escribe tres ficheros:

- precios.json: último precio de cada activo, con el mismo formato que el
  panel de Claude (id, nombre, tipo, precio, divisa, fecha, fuente).
- msci.json: cierres del iShares Core MSCI World (EUNL, Xetra) y su caída
  desde el cierre más alto de 52 semanas, para el aviso de recompra.
- estado.json: de dónde salió cada precio, a qué hora y qué falló.

Si todas las fuentes de un activo fallan, conserva su precio anterior.
Nunca sustituye un precio por otro más antiguo.
"""

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).resolve().parent
MADRID = ZoneInfo("Europe/Madrid")
BERLIN = ZoneInfo("Europe/Berlin")
TIMEOUT = 25
CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}
NIVELES_CAIDA = (0.03, 0.05, 0.10, 0.20)


# ---------------------------------------------------------------- utilidades

def ahora():
    return datetime.now(MADRID).replace(microsecond=0)


def leer_json(nombre, defecto):
    ruta = BASE / nombre
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return defecto


def escribir_json(nombre, datos):
    texto = json.dumps(datos, ensure_ascii=False, indent=2) + "\n"
    (BASE / nombre).write_text(texto, encoding="utf-8")


def redondear(valor):
    valor = float(valor)
    return round(valor, 2) if abs(valor) >= 100 else round(valor, 4)


def a_epoch(valor):
    """Segundos desde 1970 a partir de un número, un datetime o un Timestamp de pandas."""
    if valor is None or valor == "":
        return None
    if hasattr(valor, "timestamp"):
        return int(valor.timestamp())
    return int(float(valor))


def hora_desde_epoch(segundos):
    segundos = a_epoch(segundos)
    if not segundos:
        return None
    instante = datetime.fromtimestamp(segundos, tz=timezone.utc)
    return instante.astimezone(MADRID).replace(microsecond=0).isoformat()


def get(url, **kwargs):
    kwargs.setdefault("headers", CABECERAS)
    kwargs.setdefault("timeout", TIMEOUT)
    ultimo_error = None
    for intento in range(2):
        try:
            respuesta = requests.get(url, **kwargs)
            if respuesta.status_code == 429 and intento == 0:
                time.sleep(8)
                continue
            respuesta.raise_for_status()
            return respuesta
        except requests.RequestException as error:
            ultimo_error = error
            time.sleep(3)
    raise ultimo_error


# ---------------------------------------------------------------- Yahoo Finance

_yf = None


def yf():
    global _yf
    if _yf is None:
        import yfinance

        _yf = yfinance
    return _yf


def historial_yahoo(simbolo, periodo="10d"):
    ticker = yf().Ticker(simbolo)
    tabla = ticker.history(
        period=periodo, interval="1d", auto_adjust=False, actions=False,
        raise_errors=True,
    )
    if tabla is None or tabla.empty:
        raise ValueError(f"Yahoo no tiene datos de {simbolo}")
    tabla = tabla.dropna(subset=["Close"])
    if tabla.empty:
        raise ValueError(f"Yahoo no tiene cierres de {simbolo}")
    try:
        meta = ticker.history_metadata or {}
    except Exception:  # noqa: BLE001 - los metadatos son opcionales
        meta = {}
    return tabla, meta


def precio_yahoo(simbolo):
    tabla, meta = historial_yahoo(simbolo)
    precio = meta.get("regularMarketPrice") or float(tabla["Close"].iloc[-1])
    hora_epoch = a_epoch(meta.get("regularMarketTime"))
    if hora_epoch:
        zona = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
        fecha = datetime.fromtimestamp(hora_epoch, tz=zona).date().isoformat()
    else:
        fecha = tabla.index[-1].date().isoformat()
    return {
        "precio": float(precio),
        "fecha": fecha,
        "moneda": (meta.get("currency") or "").upper() or None,
        "hora": hora_desde_epoch(hora_epoch),
        "simbolo": simbolo,
        "fuente": "Yahoo Finance",
    }


def simbolos_por_isin(isin, sufijos=None):
    busqueda = yf().Search(isin, max_results=10, news_count=0, lists_count=0)
    simbolos = [c.get("symbol") for c in (busqueda.quotes or []) if c.get("symbol")]
    if not simbolos:
        raise ValueError(f"Yahoo no encuentra el ISIN {isin}")
    if sufijos:
        def orden(simbolo):
            for i, sufijo in enumerate(sufijos):
                if simbolo.endswith(sufijo):
                    return i
            return len(sufijos)

        simbolos.sort(key=orden)
    return simbolos


def precio_yahoo_isin(isin, divisa, sufijos=None):
    errores = []
    for simbolo in simbolos_por_isin(isin, sufijos)[:5]:
        try:
            resultado = precio_yahoo(simbolo)
        except Exception as error:  # noqa: BLE001
            errores.append(f"{simbolo}: {error}")
            continue
        if resultado["moneda"] != divisa:
            errores.append(f"{simbolo}: cotiza en {resultado['moneda'] or 'moneda desconocida'}")
            continue
        return resultado
    raise ValueError("; ".join(errores) or f"sin cotización en {divisa}")


# ---------------------------------------------------------------- FT Markets

def precio_ft_fondo(simbolo):
    url = "https://markets.ft.com/data/funds/tearsheet/summary"
    html = get(url, params={"s": simbolo}).text
    encontrado = re.search(
        r"Price \(([A-Z]{3})\)\s*</span>\s*<span[^>]*mod-ui-data-list__value[^>]*>"
        r"\s*([0-9][0-9.,]*)",
        html,
    )
    if not encontrado:
        titulo = re.search(r"<title>(.*?)</title>", html, re.S)
        pista = re.search(r".{0,80}Price \(.{0,80}", html, re.S)
        raise ValueError(
            "FT: no encuentro el precio en la página "
            f"(título: {titulo.group(1).strip()[:100] if titulo else '?'}; "
            f"texto: {re.sub(r'<[^>]+>', ' ', pista.group(0))[:160] if pista else 'sin Price'})"
        )
    fecha = None
    cuando = re.search(r"[Aa]s of\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})", html)
    if cuando:
        texto = f"{cuando.group(1)} {cuando.group(2)} {cuando.group(3)}"
        fecha = datetime.strptime(texto, "%b %d %Y").date().isoformat()
    if not fecha:
        raise ValueError("FT: no encuentro la fecha del valor liquidativo")
    return {
        "precio": float(encontrado.group(2).replace(",", "")),
        "fecha": fecha,
        "moneda": encontrado.group(1),
        "hora": None,
        "simbolo": simbolo,
        "fuente": "FT Markets",
    }


# ---------------------------------------------------------------- cripto y divisas

def precios_coingecko(ids):
    respuesta = get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": ",".join(ids),
            "vs_currencies": "eur",
            "include_last_updated_at": "true",
        },
    )
    return respuesta.json()


def precio_coingecko(id_moneda, cache):
    datos = cache.get(id_moneda) or {}
    if "eur" not in datos:
        raise ValueError(f"CoinGecko no devolvió {id_moneda}")
    hora = hora_desde_epoch(datos.get("last_updated_at"))
    return {
        "precio": float(datos["eur"]),
        "fecha": (hora or ahora().isoformat())[:10],
        "moneda": "EUR",
        "hora": hora,
        "simbolo": id_moneda,
        "fuente": "CoinGecko",
    }


def precio_kraken(par):
    datos = get("https://api.kraken.com/0/public/Ticker", params={"pair": par}).json()
    if datos.get("error"):
        raise ValueError(f"Kraken: {datos['error']}")
    ticker = next(iter(datos["result"].values()))
    momento = ahora()
    return {
        "precio": float(ticker["c"][0]),
        "fecha": momento.date().isoformat(),
        "moneda": "EUR",
        "hora": momento.isoformat(),
        "simbolo": par,
        "fuente": "Kraken",
    }


def precio_frankfurter(de, a):
    errores = []
    for url in ("https://api.frankfurter.dev/v1/latest", "https://api.frankfurter.app/latest"):
        try:
            datos = get(url, params={"base": de, "symbols": a}).json()
            return {
                "precio": float(datos["rates"][a]),
                "fecha": datos["date"],
                "moneda": a,
                "hora": None,
                "simbolo": f"{de}{a}",
                "fuente": "BCE (Frankfurter)",
            }
        except Exception as error:  # noqa: BLE001
            errores.append(f"{url}: {error}")
    raise ValueError("; ".join(errores))


# ---------------------------------------------------------------- un activo

def obtener(fuente, activo, cache_coingecko):
    tipo = fuente["tipo"]
    if tipo == "yahoo":
        return precio_yahoo(fuente["s"])
    if tipo == "yahoo_isin":
        return precio_yahoo_isin(fuente["isin"], activo["divisa"], fuente.get("sufijos"))
    if tipo == "ft_fondo":
        return precio_ft_fondo(fuente["s"])
    if tipo == "coingecko":
        return precio_coingecko(fuente["id"], cache_coingecko)
    if tipo == "kraken":
        return precio_kraken(fuente["par"])
    if tipo == "frankfurter":
        return precio_frankfurter(fuente["de"], fuente["a"])
    raise ValueError(f"tipo de fuente desconocido: {tipo}")


def validar(resultado, activo):
    if not resultado["precio"] or resultado["precio"] <= 0:
        raise ValueError("precio no válido")
    if resultado.get("moneda") and resultado["moneda"] != activo["divisa"]:
        raise ValueError(f"moneda {resultado['moneda']}, esperaba {activo['divisa']}")
    date.fromisoformat(resultado["fecha"])


def cambio_sospechoso(candidato, previo):
    """Un salto de más del 50 % frente a un precio reciente suele ser un error de
    fuente (otra moneda, otro valor con el mismo ISIN), no un movimiento real."""
    if not previo or not previo.get("precio") or not previo.get("fecha"):
        return False
    antiguedad = date.fromisoformat(candidato["fecha"]) - date.fromisoformat(previo["fecha"])
    if antiguedad > timedelta(days=30):
        return False
    return abs(candidato["precio"] / float(previo["precio"]) - 1) > 0.5


def actualizar_activos(activos, anteriores, estado):
    ids_coingecko = [
        f["id"] for a in activos for f in a["fuentes"] if f["tipo"] == "coingecko"
    ]
    cache = {}
    if ids_coingecko:
        try:
            cache = precios_coingecko(ids_coingecko)
        except Exception as error:  # noqa: BLE001
            estado["errores"].append(f"CoinGecko: {error}")

    filas = []
    for activo in activos:
        previo = anteriores.get(activo["id"])
        fallos = []
        resultado = None
        for fuente in activo["fuentes"]:
            try:
                candidato = obtener(fuente, activo, cache)
                validar(candidato, activo)
            except Exception as error:  # noqa: BLE001
                fallos.append(f"{fuente['tipo']}: {str(error)[:300]}")
                continue
            if previo and previo.get("fecha") and candidato["fecha"] < previo["fecha"]:
                fallos.append(
                    f"{fuente['tipo']}: dato del {candidato['fecha']}, más antiguo que el guardado"
                )
                continue
            if cambio_sospechoso(candidato, previo):
                fallos.append(
                    f"{fuente['tipo']}: {candidato['precio']} cambia más de un 50 % "
                    f"frente al guardado ({previo['precio']}); lo descarto"
                )
                continue
            resultado = candidato
            break

        info = {"fallos": fallos}
        if resultado:
            fila = {
                "id": activo["id"],
                "nombre": activo["nombre"],
                "tipo": activo["tipo"],
                "precio": redondear(resultado["precio"]),
                "divisa": activo["divisa"],
                "fecha": resultado["fecha"],
                "fuente": resultado["fuente"],
            }
            info.update(
                ok=True,
                fuente=resultado["fuente"],
                simbolo=resultado.get("simbolo"),
                hora=resultado.get("hora"),
            )
            if previo and previo.get("precio"):
                cambio = fila["precio"] / float(previo["precio"]) - 1
                info["cambio_vs_anterior"] = round(cambio, 4)
                if abs(cambio) > 0.25:
                    info["aviso"] = f"cambia un {cambio:+.0%} frente al precio anterior: revisar"
        else:
            fila = previo
            info.update(ok=False, conservado=bool(previo))
            estado["errores"].append(f"{activo['id']}: sin precio nuevo")
        estado["activos"][activo["id"]] = info
        if fila:
            filas.append(fila)
        time.sleep(1)
    return filas


# ---------------------------------------------------------------- MSCI World

def historial_stooq(simbolo):
    import io

    import pandas as pd

    texto = get("https://stooq.com/q/d/l/", params={"s": simbolo, "i": "d"}).text
    tabla = pd.read_csv(io.StringIO(texto))
    if "Close" not in tabla.columns or tabla.empty:
        raise ValueError(f"Stooq no tiene datos de {simbolo}")
    tabla["Date"] = pd.to_datetime(tabla["Date"])
    return tabla.set_index("Date")[["Close"]].dropna()


def cierres_msci():
    try:
        tabla, _ = historial_yahoo("EUNL.DE", periodo="13mo")
        serie = tabla["Close"]
        fechas = [indice.date() for indice in tabla.index]
        return list(zip(fechas, serie.astype(float))), "Yahoo Finance (EUNL.DE)"
    except Exception as error_yahoo:  # noqa: BLE001
        try:
            tabla = historial_stooq("eunl.de")
            fechas = [indice.date() for indice in tabla.index]
            return list(zip(fechas, tabla["Close"].astype(float))), "Stooq (eunl.de)"
        except Exception as error_stooq:  # noqa: BLE001
            raise ValueError(f"Yahoo: {error_yahoo}; Stooq: {error_stooq}") from None


def maximo_52_semanas(cierres, hasta):
    ventana = [(f, c) for f, c in cierres if hasta - timedelta(days=365) < f <= hasta]
    return max(ventana, key=lambda par: par[1])


def datos_msci():
    cierres, fuente = cierres_msci()
    cierres.sort()
    momento = datetime.now(BERLIN)
    en_curso = None
    if cierres and cierres[-1][0] == momento.date() and (momento.hour, momento.minute) < (17, 40):
        en_curso = cierres.pop()
    if len(cierres) < 2:
        raise ValueError("faltan cierres del MSCI World")
    (f_hoy, c_hoy), (f_ayer, c_ayer) = cierres[-1], cierres[-2]
    f_max, c_max = maximo_52_semanas(cierres, f_hoy)
    f_max_ayer, c_max_ayer = maximo_52_semanas(cierres, f_ayer)
    caida_hoy = 1 - c_hoy / c_max
    caida_ayer = 1 - c_ayer / c_max_ayer
    cruzados = [n for n in NIVELES_CAIDA if caida_hoy >= n > caida_ayer]
    datos = {
        "nombre": "iShares Core MSCI World UCITS ETF (Xetra)",
        "isin": "IE00B4L5Y983",
        "ticker": "EUNL",
        "fuente": fuente,
        "fecha": f_hoy.isoformat(),
        "cierre": round(c_hoy, 4),
        "fecha_anterior": f_ayer.isoformat(),
        "cierre_anterior": round(c_ayer, 4),
        "max_52s": round(c_max, 4),
        "fecha_max_52s": f_max.isoformat(),
        "caida_hoy": round(caida_hoy, 4),
        "caida_ayer": round(caida_ayer, 4),
        "nivel_cruzado": max(cruzados) if cruzados else None,
        "nota": (
            "Caídas medidas con cierres diarios frente al cierre más alto de las 52 "
            "semanas anteriores. 'caida_hoy' usa el último cierre completo y "
            "'caida_ayer' el anterior."
        ),
    }
    if en_curso:
        datos["en_curso"] = {
            "fecha": en_curso[0].isoformat(),
            "precio": round(en_curso[1], 4),
            "caida": round(1 - en_curso[1] / max(c_max, en_curso[1]), 4),
        }
    return datos


# ---------------------------------------------------------------- principal

def main():
    activos = leer_json("activos.json", [])
    anteriores = {fila["id"]: fila for fila in leer_json("precios.json", [])}
    estado = {"actualizado": None, "activos": {}, "errores": []}

    filas = actualizar_activos(activos, anteriores, estado)
    escribir_json("precios.json", filas)

    try:
        escribir_json("msci.json", datos_msci())
        estado["msci"] = {"ok": True}
    except Exception as error:  # noqa: BLE001
        estado["msci"] = {"ok": False, "fallo": str(error)[:300]}
        estado["errores"].append(f"MSCI: {error}")

    estado["actualizado"] = ahora().isoformat()
    escribir_json("estado.json", estado)

    nuevos = sum(1 for info in estado["activos"].values() if info.get("ok"))
    print(f"Precios nuevos: {nuevos} de {len(activos)}")
    for error in estado["errores"]:
        print(f"  - {error}")
    if nuevos == 0:
        sys.exit("Ninguna fuente ha funcionado")


if __name__ == "__main__":
    main()
