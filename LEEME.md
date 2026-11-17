# Publicador de reels en la nube (@quehariastu._)

GitHub Actions publica los reels de `cola.json` a su hora (hora de Chile), aunque el PC esté apagado.

- `cola.json`: los reels programados (`pendiente`, `publicado`, `error`). Después de cada publicación, GitHub lo actualiza solo.
- `videos/`: los videos optimizados para Instagram (720×1280, ~5 MB).
- `publicar_reel.py` + `tunel.py`: el mismo publicador del PC. Abre un túnel temporal (serveo.net por SSH) desde el servidor de GitHub y Meta descarga el video desde ahí (`file_url`).
- `registro.log`: lo que pasó en cada ejecución.
- Secretos del repositorio (Settings → Secrets → Actions): `IG_TOKEN`, el token de la app Publicador Reels, y `IG_USER_ID`. **Nunca van en los archivos.**

Revisa cada ejecución en la pestaña **Actions**. Para publicar ya (o probar sin publicar): Actions → "Publicar reels" → **Run workflow**.
Horario de revisión: cada hora (minuto 3) y cada 10 minutos entre las 19:00 y las 20:59 UTC, que son las 16:00–17:59 en Chile en verano y las 15:00–16:59 en invierno.
