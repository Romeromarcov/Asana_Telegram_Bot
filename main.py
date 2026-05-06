"""
Entry point para desarrollo local.
En Railway, Railway usa el Procfile: uvicorn dashboard:app --host 0.0.0.0 --port $PORT
El bot arranca automáticamente desde el lifespan de dashboard.py.
"""
import os
import uvicorn
from dashboard import app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
