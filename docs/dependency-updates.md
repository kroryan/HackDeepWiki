# Actualización de componentes y dependencias

## Componentes empaquetados

Node, OpenCode, Engraphis y las herramientas AppImage se declaran en
`build/components.json`. No se deben cambiar versiones directamente en YAML o
scripts.

Para actualizar:

1. cambiar la versión/commit y todos los SHA-256 de plataforma en el manifiesto;
2. ejecutar `python scripts/prepare_assets.py <linux|windows>`;
3. ejecutar tests de contrato OpenCode/Engraphis;
4. compilar y ejecutar el smoke test del artefacto;
5. revisar el inventario CycloneDX y checksums.

No se permiten descargas sin hash ni fallback a `main`/`latest`.

## Python y npm

Dependabot abre actualizaciones agrupadas semanalmente. No se hace automerge.
Después de actualizar:

```bash
poetry -C api lock
poetry -C api sync --with dev --without build
api/.venv/bin/python -m pytest -q
npm ci --legacy-peer-deps
npm run test:coverage
npm run lint
npm run typecheck
```

Los advisories sin corrección solo pueden ignorarse en un archivo versionado,
con paquete, versión, identificador exacto, motivo y caducidad. Los scripts
`check_pip_audit.py` y `check_npm_audit.py` rechazan excepciones amplias,
caducadas o que ya no corresponden al resultado real.
