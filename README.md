# precios-cartera

Busca cada 30 minutos el último precio de los activos de la cartera y lo guarda
en este repositorio. La revisión diaria de Claude lee estos ficheros para
actualizar el panel de la cartera.

## Qué genera

| Fichero | Qué contiene |
|---|---|
| `precios.json` | Último precio de cada activo, con su fecha y su fuente. |
| `msci.json` | Cierres del iShares Core MSCI World (EUNL, Xetra) y su caída desde el cierre más alto de 52 semanas, para el aviso de recompra. |
| `vigilancia.json` | Para cada valor de `vigilancia_lista.json`: media de 200 sesiones, máximo de 20 sesiones, mínimo de 10, stop orientativo y si cumple la regla de entrada del tramo a corto plazo. |
| `estado.json` | De dónde salió cada precio, a qué hora y qué fuentes fallaron. |

Solo hay precios: ninguna cantidad ni importe de la cartera.

## De dónde salen los precios

- **Fondos**: valor liquidativo de FT Markets; si falla, Yahoo Finance. Solo cambia una vez al día.
- **Plata física (ETC)**, **District Metals** y el **cambio euro/dólar canadiense**: Yahoo Finance (con unos 15 minutos de retraso).
- **Criptomonedas**: CoinGecko; si falla, Kraken.
- **Cambio euro/dólar canadiense**: si Yahoo falla, tipo de referencia del BCE.

Si todas las fuentes de un activo fallan, se conserva el precio anterior. Nunca se
sustituye un precio por otro más antiguo, y un salto de más del 50 % frente a un
precio reciente se descarta como error de la fuente.

## Cómo se usa

- Se ejecuta solo cada 30 minutos (GitHub Actions, pestaña **Actions** → **Precios**).
- Para lanzarlo a mano: **Actions** → **Precios** → **Run workflow**.
- Para añadir o quitar un activo, edita `activos.json`.
- Para añadir o quitar un valor vigilado, edita `vigilancia_lista.json` (con su símbolo de Yahoo Finance, por ejemplo `NVDA` o `IDR.MC`).
- Para probarlo en tu ordenador: `pip install -r requirements.txt` y `python precios.py`.
