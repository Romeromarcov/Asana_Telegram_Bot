# 📱 Bot de Asana + Telegram — Guía de instalación paso a paso

## ¿Qué hace este bot?
- Envía recordatorios a cada miembro del equipo **2 veces al día** (mañana y tarde)
- Cada persona responde `/listo 1` para marcar una tarea como completada en Asana
- Te manda un **reporte diario** a ti (el manager) con todo completado y pendiente
- Sigue insistiendo hasta que las tareas estén cerradas

---

## PASO 1 — Crear el bot en Telegram (5 minutos)

1. Abre Telegram y busca **@BotFather**
2. Escríbele `/newbot`
3. Te pedirá un nombre para el bot (ej: `Mi Equipo Tareas`)
4. Luego un username (ej: `miequipo_tareas_bot`) — debe terminar en `bot`
5. BotFather te dará un **token** como este:
   ```
   123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
   ```
   **Guárdalo**, lo necesitarás luego.

---

## PASO 2 — Obtener tu Chat ID de Telegram

1. Abre Telegram y busca tu nuevo bot (el que acabas de crear)
2. Escríbele `/start`
3. Ahora abre este enlace en tu navegador (reemplaza TU_TOKEN con el tuyo):
   ```
   https://api.telegram.org/botTU_TOKEN/getUpdates
   ```
4. Verás un JSON. Busca el número en `"id"` dentro de `"chat"` — ese es tu **Chat ID**.
   Ejemplo: `"chat":{"id":123456789,...}`

> ⚠️ Repite este proceso para cada miembro del equipo:
> diles que busquen el bot y escriban `/mi_id` — el bot les responderá con su ID.

---

## PASO 3 — Obtener el token de Asana

1. Ve a https://app.asana.com/0/my-profile/apps
2. Haz clic en **"Create new token"** (o "Crear nuevo token")
3. Dale un nombre (ej: "Bot Telegram")
4. Copia el token generado

---

## PASO 4 — Obtener el Workspace ID de Asana

1. Ve a https://app.asana.com/api/1.0/workspaces en tu navegador
   (debes estar logueado en Asana)
2. Verás algo como: `{"data":[{"gid":"1234567890123456","name":"Tu Empresa"}]}`
3. El número largo es tu **Workspace ID**

---

## PASO 5 — Obtener los GIDs de usuario de cada miembro en Asana

Para cada persona del equipo:
1. Ve a https://app.asana.com/api/1.0/users?workspace=TU_WORKSPACE_ID
2. Busca el nombre de la persona
3. Copia su `"gid"` (número largo)

---

## PASO 6 — Configurar el archivo del bot

Abre el archivo `bot.py` y busca esta sección cerca de la línea 40:

```python
TEAM = {
    # 123456789: "1234567890123456",   # ejemplo
}
```

Reemplázala con los datos reales de tu equipo:

```python
TEAM = {
    123456789: "9876543210987654",   # Juan Pérez
    987654321: "1111222233334444",   # María López
    555666777: "5555666677778888",   # Carlos Ruiz
}
```

**Formato:** `telegram_id: "asana_gid"`

---

## PASO 7 — Desplegar en Railway (gratis / $5 al mes)

Railway es la forma más fácil de tener el bot corriendo 24/7 sin saber programar.

1. Ve a https://railway.app y crea una cuenta (puedes entrar con GitHub o Google)
2. Haz clic en **"New Project"** → **"Deploy from GitHub repo"**
   - Si no tienes GitHub, elige **"Empty project"** → **"Add Service"** → **"GitHub repo"**
   - Alternativa sin GitHub: usa el botón **"Deploy from template"** y busca "Python"

### Subir los archivos a Railway:
1. En tu proyecto de Railway, ve a la pestaña **"Files"** (o usa el editor web)
2. Sube los 3 archivos: `bot.py`, `requirements.txt`, y crea el archivo de inicio

### Agregar las variables de entorno:
1. En Railway, ve a tu servicio → pestaña **"Variables"**
2. Agrega cada una de estas variables (clic en "Add Variable"):

| Variable | Valor |
|----------|-------|
| `TELEGRAM_TOKEN` | El token de BotFather |
| `ASANA_TOKEN` | Tu token de Asana |
| `ASANA_WORKSPACE_ID` | El ID de tu workspace |
| `MANAGER_CHAT_ID` | Tu Chat ID de Telegram |
| `TIMEZONE` | Tu zona horaria (ver abajo) |

### Zonas horarias comunes:
- Colombia / Ecuador / Perú: `America/Bogota`
- México (centro): `America/Mexico_City`
- Argentina / Uruguay: `America/Argentina/Buenos_Aires`
- Chile: `America/Santiago`
- España: `Europe/Madrid`
- Venezuela: `America/Caracas`
- EE.UU. Este: `America/New_York`

3. Haz clic en **"Deploy"** — Railway instalará todo automáticamente.

---

## PASO 8 — Añadir a tu equipo

Una vez el bot esté corriendo:
1. Comparte el username de tu bot con cada miembro del equipo (ej: `@miequipo_tareas_bot`)
2. Diles que lo busquen en Telegram y escriban `/start`
3. El bot confirmará que están registrados

---

## Comandos del bot

### Para el equipo:
| Comando | Función |
|---------|---------|
| `/start` | Iniciar y ver bienvenida |
| `/mis_tareas` | Ver tareas pendientes en cualquier momento |
| `/listo 1` | Marcar la tarea #1 como completada |
| `/listo_todas` | Marcar todas las tareas como completadas |
| `/mi_id` | Ver tu ID de Telegram (para configuración) |

### Solo para el manager:
| Comando | Función |
|---------|---------|
| `/reporte` | Ver el reporte completo del equipo ahora mismo |

---

## Horarios automáticos (puedes cambiarlos en Variables de Railway)

| Variable | Por defecto | Descripción |
|----------|-------------|-------------|
| `MORNING_HOUR` | `9` | Hora del recordatorio de mañana |
| `AFTERNOON_HOUR` | `15` | Hora del recordatorio de tarde |
| `REPORT_HOUR` | `18` | Hora del reporte diario al manager |

---

## ¿Problemas?

- **El bot no responde:** Verifica que el `TELEGRAM_TOKEN` sea correcto en Railway
- **No muestra tareas:** Verifica el `ASANA_TOKEN` y que las tareas estén asignadas al usuario en Asana
- **Error de timezone:** Usa exactamente el formato de la tabla de zonas horarias
- **Alguien no aparece:** Verifica que su `telegram_id` y `asana_gid` sean correctos en `bot.py`

---

## Costo estimado
- **Railway:** Gratis hasta cierto uso mensual, luego ~$5/mes
- **Telegram Bot API:** Completamente gratis
- **Asana API:** Gratis (incluido en todos los planes de Asana)
