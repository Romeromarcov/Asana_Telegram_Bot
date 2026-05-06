"""
Script de limpieza ONE-TIME: elimina proyectos duplicados de Asana.

Uso:
    ASANA_TOKEN=<tu_token> python cleanup_projects.py

Qué hace:
  1. Lista todos los proyectos "Tareas - *" del workspace
  2. Por cada nombre, conserva el MÁS ANTIGUO y archiva/elimina los demás
  3. Regenera projects.json con los GIDs correctos y sus secciones
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

import httpx

ASANA_TOKEN     = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE = os.environ.get("ASANA_WORKSPACE_ID", "1145691884633083")
ASANA_BASE      = "https://app.asana.com/api/1.0"
PROJECTS_FILE   = Path(__file__).parent / "projects.json"

# GIDs de los miembros del equipo (de team.txt)
TEAM = {
    "1214167186224936": "Alexandra (Atención al Cliente)",
    "1214167186224927": "Marcos Velasco (Administración)",
    "1214167186224933": "Luis Laya (Almacén)",
    "1214167186224921": "Melanie Reverón (Finanzas)",
    "1214167186224930": "Ronald Cáseres (Supervisor Ventas)",
}

# Mapping nombre base → asana_gid del miembro
FIRST_NAME_TO_GID = {
    "Alexandra": "1214167186224936",
    "Marcos":    "1214167186224927",
    "Luis":      "1214167186224933",
    "Melanie":   "1214167186224921",
    "Ronald":    "1214167186224930",
}


async def main():
    if not ASANA_TOKEN:
        print("❌ Define ASANA_TOKEN como variable de entorno")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {ASANA_TOKEN}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20) as client:

        # 1. Listar todos los proyectos del workspace
        print("📋 Listando proyectos…")
        r = await client.get(
            f"{ASANA_BASE}/projects",
            headers=headers,
            params={
                "workspace": ASANA_WORKSPACE,
                "opt_fields": "name,gid,created_at",
                "limit": 100,
            },
        )
        r.raise_for_status()
        all_projects = r.json()["data"]

        # 2. Agrupar proyectos "Tareas - *" por nombre
        by_name = defaultdict(list)
        for p in all_projects:
            if p["name"].startswith("Tareas - "):
                by_name[p["name"]].append(p)

        # 3. Por cada grupo, conservar el más antiguo y eliminar el resto
        to_delete = []
        to_keep   = {}   # nombre → proyecto a conservar

        for name, projects in by_name.items():
            # Ordenar por created_at ASC → el primero es el más antiguo
            projects_sorted = sorted(projects, key=lambda p: p.get("created_at", ""))
            keep   = projects_sorted[0]
            delete = projects_sorted[1:]

            to_keep[name] = keep
            to_delete.extend(delete)
            print(f"  ✅ Conservar: {name} → {keep['gid']} ({keep.get('created_at','')[:10]})")
            for d in delete:
                print(f"  🗑️  Eliminar:  {name} → {d['gid']} ({d.get('created_at','')[:10]})")

        if not to_delete:
            print("\n✨ No hay duplicados. Todo limpio.")
        else:
            print(f"\n🗑️  Eliminando {len(to_delete)} proyectos duplicados…")
            for proj in to_delete:
                r = await client.delete(
                    f"{ASANA_BASE}/projects/{proj['gid']}",
                    headers=headers,
                )
                if r.status_code in (200, 204):
                    print(f"  ✓ Eliminado {proj['name']} ({proj['gid']})")
                else:
                    print(f"  ✗ Error {r.status_code} al eliminar {proj['gid']}: {r.text[:100]}")

        # 4. Regenerar projects.json con los proyectos conservados
        print("\n📄 Regenerando projects.json…")
        projects_cfg = {}

        for name, proj in to_keep.items():
            first_name  = name.replace("Tareas - ", "")
            asana_gid   = FIRST_NAME_TO_GID.get(first_name)
            if not asana_gid:
                print(f"  ⚠️  No encontré GID para {first_name}, omitiendo")
                continue

            project_gid = proj["gid"]

            # Obtener secciones
            r2 = await client.get(
                f"{ASANA_BASE}/projects/{project_gid}/sections",
                headers=headers,
                params={"opt_fields": "name,gid"},
            )
            r2.raise_for_status()
            sections = {s["name"]: s["gid"] for s in r2.json()["data"]}

            projects_cfg[asana_gid] = {
                "asana_gid":   asana_gid,
                "name":        TEAM[asana_gid],
                "project_gid": project_gid,
                "sections":    sections,
            }
            print(f"  ✓ {TEAM[asana_gid]} → proyecto {project_gid} | secciones: {list(sections.keys())}")

        PROJECTS_FILE.write_text(
            json.dumps(projects_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n✅ projects.json actualizado con {len(projects_cfg)} miembros")
        print(f"   Ruta: {PROJECTS_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
