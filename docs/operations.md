# Operación y recuperación

## Salud

- `/health/live`: el proceso responde.
- `/health/ready`: SQLite, espacio, worker y subsistemas esenciales están
  operativos.
- `/health/capabilities`: capacidades opcionales y degradaciones.
- `/health/build`: commit, canal, fecha y componentes empaquetados.
- `/health/metrics`: contadores de duración y fallos en formato JSON local.

Las rutas salvo liveness requieren autenticación cuando esta está habilitada.
Cada respuesta incluye un ID de correlación que también aparece en logs.

## Datos

Todo el estado portable vive bajo `DATABASE/` o
`HACKDEEPWIKI_DATA_ROOT`. En POSIX el directorio se crea con permisos `0700`.
Solo se soporta un proceso sobre una raíz de datos.

Comandos seguros:

```bash
api/.venv/bin/python scripts/manage_data.py integrity
api/.venv/bin/python scripts/manage_data.py backup /ruta/backup
api/.venv/bin/python scripts/manage_data.py restore \
  /ruta/backup/profile.db profile.db --confirm-stopped
api/.venv/bin/python scripts/manage_data.py rotate-secrets
```

Las migraciones usan `PRAGMA user_version`, transacciones y un backup previo.
Una restauración comprueba integridad antes de reemplazar la base. La rotación
pide las claves mediante entrada oculta (o variables de entorno dedicadas);
no se aceptan como argumentos de texto para no filtrarlas en el historial.

## Diagnóstico

`GET /api/storage/diagnostics` devuelve el estado redactado y
`GET /api/storage/diagnostics/export` crea un paquete con build, configuración segura,
salud y logs recientes. No incluye bases de datos ni secretos. Antes de
compartirlo debe revisarse igualmente como cualquier log de soporte.

## Compilación y limpieza

```bash
python scripts/check_build_space.py
python scripts/clean_build.py --frontend --backend --appdir --image
```

El limpiador solo acepta targets conocidos y resueltos dentro del repositorio.
Una compilación completa puede necesitar varios GiB libres.
