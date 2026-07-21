# PENDIENTE — Estado del proyecto tras la sesión de "wikis de sitios web + escaneo de vulnerabilidades"

Última actualización: 2026-07-21, commit `6db308d` ("feat: website wikis (crawl + generation) and full vulnerability scanning stack"), 5 commits por delante de `origin/main`, **sin push** (lo hace el usuario).

Este documento es el mapa completo de lo que se hizo, lo que falta, lo que no está probado, y decisiones de diseño que hay que recordar. Está pensado para que cualquier sesión futura (tuya o de otro agente) pueda retomar sin releer todo el historial de chat.

---

## 0. COMANDOS PARA SUBIR LA IMAGEN DOCKER (pendiente, lo haces tú)

La cuenta Docker Hub es **`krory90`**. La imagen local ya está construida como `freedeepwiki-vulnscan:latest` (~15GB). `docker_tools.py` espera poder hacer `docker pull krory90/freedeepwiki-vulnscan:latest` la primera vez que un usuario corre un escaneo profundo — **si no se sube, el toggle "Deep security scan (Docker)" caerá al fallback de build local** (`_build_local_image`, que reconstruye desde `docker/vulnscan/Dockerfile`, tarda ~15-20 min y requiere que el usuario tenga el repo clonado localmente con ese Dockerfile presente — funciona pero es mucho peor UX que un pull).

```bash
# 1. Login (si no lo has hecho ya)
docker login -u krory90

# 2. Etiquetar la imagen local con el nombre del registro
docker tag freedeepwiki-vulnscan:latest krory90/freedeepwiki-vulnscan:latest

# 3. Subir (esto tardará bastante, son ~15GB)
docker push krory90/freedeepwiki-vulnscan:latest

# 4. (Opcional pero recomendado) verificar que se puede volver a bajar en limpio
docker rmi krory90/freedeepwiki-vulnscan:latest
docker pull krory90/freedeepwiki-vulnscan:latest
```

Si en el futuro se reconstruye la imagen (nuevas herramientas, actualización de Kali, etc.), conviene versionar con un tag además de `latest`:

```bash
docker tag freedeepwiki-vulnscan:latest krory90/freedeepwiki-vulnscan:2026-07-21
docker push krory90/freedeepwiki-vulnscan:2026-07-21
docker push krory90/freedeepwiki-vulnscan:latest
```

`REGISTRY_IMAGE` está hardcodeado en `api/web_vuln_scanner/docker_tools.py` línea ~42 como `"krory90/freedeepwiki-vulnscan:latest"`. Si cambias de cuenta o nombre de imagen, hay que actualizar esa constante.

---

## 1. Qué se hizo en esta sesión (resumen ejecutivo)

1. **Auditoría completa del scanner de dependencias existente** (`api/vuln_scanner/`) — encontrado y arreglado un crash real (bug de logging con conteo de argumentos incorrecto) más 3 bugs de exactitud (falsos positivos de categoría por substring, ~80% de hallazgos duplicados por alias de OSV, severidad perdida a UNKNOWN). Todo verificado contra datos reales de OSV.dev.

2. **Wikis de páginas web/dominios** (feature nueva completa):
   - `api/web_crawler/`: crawler con Playwright que vuelca cada página como Markdown mirando la estructura de URLs del sitio, reutilizando el pipeline de generación de wiki existente vía `repo_type == "website"`.
   - Clasificación website vs. repo vs. local **reescrita** en `src/app/page.tsx` (el regex original corrompía URLs de un solo segmento de path, ej. `https://example.com/blog` se parseaba mal como `owner/repo` de git — bug real encontrado y arreglado).
   - Dos modos de generación: **contenido** (default — una fan wiki produce una fan wiki, no un reporte técnico) vs. **técnico** (opt-in, arquitectura/stack/estructura de páginas).
   - Contenido de usuario (perfiles/comentarios/foros) **siempre excluido automáticamente** — sin toggle de "Community Analytics" (esa feature se canceló explícitamente a media sesión). La exclusión es doble: instrucción al prompt del LLM + filtro duro en el índice RAG (`api/data_pipeline.py::_exclude_website_user_content`).

3. **Escaneo de vulnerabilidades — tres frentes**:
   - **Web** (`api/web_vuln_scanner/`): checks Python puros siempre activos (headers/cookies/TLS/rutas expuestas/puertos) + correlación OSV para librerías JS detectadas.
   - **Docker toolkit opcional** (`docker/vulnscan/`): fork independiente y completo del kit de herramientas de Kali de RedAmon (mismo Dockerfile base, sin los MCP servers — se invoca todo por subprocess uno-a-uno). Incluye nmap, nikto, httpx, whatweb, testssl, nuclei, subfinder+dnsx, ffuf, dalfox, wpscan, gitleaks, semgrep, y el arsenal completo de RedAmon (metasploit, hydra, sqlmap, etc. — no se usan actualmente pero están ahí para el futuro).
   - **Código de repos**: el scanner de dependencias ahora puede correr adicionalmente gitleaks (secretos) + semgrep (SAST) sobre el clon local, opt-in vía `enable_code_scan`.
   - **Página de "Soluciones Sugeridas"** (`api/vuln_common/remediation.py`): agrupa remediaciones por acción normalizada, prioriza por severidad, compartida por los tres tipos de escaneo. Tiene su propia pestaña/UI (`VulnRemediationPlan.tsx`) en ambos reportes (dependencias y web).
   - **Logging en tiempo real en consola** para los 4 orquestadores (crawler, dep-scan, web-scan, code-scan) — cada herramienta individual loguea inicio/fin/conteo de hallazgos, no solo un mensaje agregado.
   - **Neo4j** (`api/web_vuln_scanner/graph_db.py`) + **Postgres** vía `docker/vulnscan/docker-compose.yml` para persistencia de resultados de escaneo — ver sección de pendientes, **nunca se probó contra una instancia real corriendo**.

4. **Endpoints backend nuevos**: `/ws/website/crawl`, `/ws/web_vuln_scan`, `/api/web_vuln_cache`.

5. **UI nueva**: `WebVulnSection`, `WebVulnOverview`, `WebFindingCard`, `WebFindingDetailDrawer`, `VulnRemediationPlan` — wireados en la página de repo (sidebar + panel de contenido) para `repo_type === 'website'`.

6. **Toggle "Deep security scan (Docker)"** en `ConfigurationModal` (solo en Generate Wiki / home page) — opt-in porque implica descargar/usar Docker.

7. **Pequeños**: iconos (favicon, AppImage, .exe) desde `icono.ico`/`freedeepwiki.png`, mensaje de bienvenida actualizado en los 10 idiomas, remote git actualizado a `HackDeepWiki`.

---

## 2. LO QUE FALTA / NO ESTÁ HECHO (importante, léelo antes de asumir que todo funciona)

### 2.1. Nunca se probó de punta a punta en el navegador
Todo lo de esta sesión se verificó con scripts Python standalone y `tsc --noEmit` / `npm run build`. **Nunca se abrió el navegador y se hizo click en el flujo completo**: escribir una URL de sitio web en la home, generar la wiki, ver que el crawl progresa, ver que la wiki se genera con contenido correcto, click en "Website Security", ver que el escaneo corre y el reporte se renderiza. Esto es lo primero que hay que hacer antes de confiar en que la feature funciona de verdad para un usuario.

### 2.2. Docker toolkit — herramientas nuevas sin probar de verdad
Solo se verificaron con datos reales estas herramientas: `nmap` (sí), `testssl` (sí, tras arreglar un bug real de parseo de su JSON anidado). **Nunca se corrieron con datos reales**: `httpx`, `whatweb` (se corrió una vez suelto, no a través de `docker_tools.py`), `nikto`, `nuclei`, `subfinder+dnsx`, `ffuf`, `dalfox`, `wpscan`, `gitleaks`, `semgrep`. Los parsers de estas herramientas se escribieron basándose en la documentación/formato esperado de cada CLI, **no verificados contra su output JSON real** como sí se hizo meticulosamente con `testssl`. Es muy posible que alguno tenga el mismo tipo de bug que tuvo `testssl` (asumir una forma de JSON que no es la real). **Antes de confiar en el Docker toolkit, correr cada herramienta manualmente contra un sitio de prueba y comparar con lo que el parser produce.**

### 2.3. `run_docker_toolkit` completo nunca se ejecutó de principio a fin
Se probó `run_testssl` aislado y `run_nmap` aislado. La función `run_docker_toolkit` (que encadena las ~9 herramientas con el helper `_timed_safe`) nunca corrió completa contra un sitio real después de añadir subfinder/ffuf/dalfox/wpscan. Puede haber bugs de integración (nombres de variables, orden de argumentos) que solo aparecen al ejecutar todo junto.

### 2.4. Neo4j / Postgres nunca se levantaron
`docker/vulnscan/docker-compose.yml` define los tres servicios (vulnscan, neo4j, postgres) pero **nunca se hizo `docker compose up`** para verificar que arrancan correctamente, que `graph_db.py` se conecta bien con las credenciales por defecto (`neo4j`/`freedeepwiki_secret`), ni que el schema (`CONSTRAINTS`/`INDEXES`) se crea sin errores. `try_persist_report()` está en el orquestador con `try/except` amplio así que un fallo aquí no rompe el escaneo — pero tampoco vas a saber si funciona hasta probarlo.

Postgres en particular: **está definido en el compose pero NINGÚN código Python escribe en él todavía**. Se mencionó como "historial de scans" en el diseño pero nunca se implementó el schema de tablas ni el writer. Es infraestructura fantasma por ahora — o se implementa o se quita del compose para no confundir.

### 2.5. `ModelSelectionModal` (flujo "Update Wiki") no tiene las opciones de website
El modal de "Generate Wiki" (`ConfigurationModal`) tiene todo: crawl scope, Technical Analysis, Deep security scan. El modal de "Refresh/Update Wiki" (`ModelSelectionModal`) **solo tiene el toggle de Security Analysis de dependencias** (heredado de una sesión anterior) — nunca se extendió con las opciones de website. Si un usuario actualiza una wiki de sitio web existente, no puede cambiar el scope del crawl ni activar el escaneo profundo desde ahí. Falta portar el mismo bloque de UI que tiene `ConfigurationModal` (crawl scope radio buttons, Technical Analysis checkbox, Deep scan checkbox) a `ModelSelectionModal`, condicionado a `isWebsite` (que ya existe como prop ahí, se usa solo para el toggle de Security Analysis de dependencias).

### 2.6. i18n incompleto para las features nuevas
Solo se tradujo el mensaje de bienvenida de la home page (`home.*` keys) a los 10 idiomas. **Todo el texto nuevo dentro de `ConfigurationModal`, `WebVulnSection`, `VulnRemediationPlan`, etc. está hardcodeado en inglés** (no usa el sistema `t()`/mensajes de idioma). Si se quiere soporte i18n real para la feature completa, hay que extraer todos esos strings a los archivos de `src/messages/*.json`.

### 2.7. Portabilidad de Windows para el Docker toolkit
Nunca se verificó que `docker_tools.py` funcione en Windows. Docker Desktop en Windows corre contenedores Linux vía WSL2, así que en teoría `docker run` debería comportarse igual, pero:
- Los paths de bind-mount (`-v {tmp_dir}:/out`) usan `tempfile.TemporaryDirectory()` de Python, que en Windows da paths tipo `C:\Users\...\AppData\Local\Temp\...` — Docker Desktop en Windows necesita que esos paths estén "compartidos" con el daemon (configuración de Docker Desktop), y la sintaxis de bind mount con paths de Windows puede necesitar conversión (`/c/Users/...` o similar según el backend). **Nunca probado.**
- `shutil.which("docker")` debería encontrar `docker.exe` bien en Windows si está en PATH, pero no se verificó.

### 2.8. El AppImage empaqueta el crawler/scanner de dependencias pero NO el Docker toolkit
Esto es intencional (el toolkit de 15GB nunca debe ir dentro del AppImage), pero asegúrate de que quede claro en cualquier documentación de usuario: el escaneo web básico (headers/cookies/TLS/puertos) funciona siempre sin Docker; el escaneo profundo requiere que el usuario tenga Docker instalado por su cuenta y bajará ~15GB la primera vez que lo active.

### 2.9. Heurísticas / filtros que pueden necesitar ajuste fino
- `run_ffuf`: el filtro `sensitive_hints` (lista de substrings como `.env`, `.git`, `backup`, etc.) es una lista arbitraria elegida a mano — puede generar falsos negativos (rutas sensibles con nombres no incluidos) o falsos positivos (rutas benignas que casualmente contienen esos substrings, ej. `/api-backup-docs`).
- `run_wpscan` solo se dispara si algún string en `techs` contiene `"wordpress"` (case-insensitive) — verificar que esto realmente calza con el formato que `whatweb`/`httpx` producen en la práctica (whatweb usa `"generator:WordPress 7.0.2"` como vi en pruebas anteriores de esta sesión, así que el `.lower()` + `"wordpress" in t` debería matchear, pero no se verificó con el código actual tras los cambios).
- `run_dalfox` limita a 10 URLs con query params (`urls_with_params[:10]`) — arbitrario, podría necesitar ser configurable.

### 2.10. CI/workflow no cubre el Docker toolkit
`.github/workflows/release.yml` construye el AppImage/exe pero no construye ni sube la imagen Docker. Si se quiere automatizar la publicación de `krory90/freedeepwiki-vulnscan`, habría que añadir un job separado (probablemente NO en el mismo workflow, dado que tarda mucho y no debería bloquear releases del AppImage).

### 2.11. Timeouts del websocket pueden ser insuficientes para sitios grandes
`fetchWebsiteStructureViaCrawl` y `runWebVulnScan` en el frontend tienen timeout de 20 min y 10 min respectivamente. Un crawl de "todo el sitio" (`crawl_scope_mode: 'all'`, tope duro de 2000 páginas) en un sitio grande podría tardar más de 20 minutos con Playwright (cada página tarda ~1-3s). Vale la pena revisar si esos timeouts son realistas para el caso "all" vs. el caso "count" con pocas páginas.

### 2.12. Agrupación de remediación por texto exacto normalizado
`build_remediation_plan` agrupa por `_normalize_action()` que solo hace `strip()` + colapsar espacios + lowercase. Dos remediaciones para la misma librería pero con números de versión ligeramente distintos en el texto (ej. "Upgrade lodash from 4.17.20 to 4.17.21" vs "Upgrade lodash from 4.17.19 to 4.17.21") **no se agruparán** aunque sean esencialmente la misma acción, porque el texto completo difiere. Funciona bien cuando el fix es idéntico (mismo `ai_remediation` string), pero no hace fuzzy matching. Podría mejorarse con un matching más inteligente (ej. por package_name) si se nota que genera ruido.

### 2.13. Sin documentación de usuario
No se actualizó ningún README ni doc de usuario explicando las features nuevas (wikis de sitios web, escaneo de vulnerabilidades, requisitos de Docker, etc.). Solo existe el mensaje de bienvenida corto en la home page.

### 2.14. `api/poetry.lock` regenerado — verificar que el CI lo acepta
Se añadieron `playwright`, `beautifulsoup4`, `markdownify`, `neo4j` a `api/pyproject.toml` (no estaban declarados aunque ya se usaban en runtime). Se instaló `poetry==2.0.1` dentro de `.venv` (no había `poetry` de sistema disponible en Kali) y se corrió `poetry lock` para regenerar `api/poetry.lock` en consecuencia (`poetry check` pasó limpio, solo warnings preexistentes de formato viejo de poetry, no relacionados). **Nunca se verificó que `poetry install --only main` con este lock nuevo funcione en un entorno limpio de CI** (GitHub Actions, Windows/Linux runners) — solo se verificó `poetry check` localmente. Si el primer build de CI tras este cambio falla, empezar por ahí.

También se añadieron `api.vuln_common`, `api.web_crawler`, `api.web_vuln_scanner`, `neo4j` a `packages_to_collect` en `freedeepwiki.spec` (nota: `neo4j` no aparece como carpeta suelta en `dist/freedeepwiki/_internal/` -- eso es normal, un paquete Python puro sin datos extra se empaqueta comprimido dentro del PYZ archive embebido en el ejecutable, no como archivos `.py` sueltos en el filesystem; se confirmó revisando `build/freedeepwiki/PYZ-00.toc`, que sí lista sus 224 submódulos correctamente. Solo paquetes con binarios nativos, como `libzim` con su `.so`, aparecen como carpetas sueltas).

---

## 3. Notas de arquitectura para recordar

- **`repo_type === 'website'`** es el discriminador central. Se propaga desde `parseRepositoryInput` (home page) → query params de URL → `effectiveRepoInfo.type` en la página de repo → todo el pipeline backend (`_create_repo` en `data_pipeline.py` intercepta este caso antes de intentar `git clone`).
- **El crawler nunca reintenta un crawl si ya existe** salvo que se pase `fresh=True` (usado en refresh de wiki). El directorio vive en `<data_root>/repos/website_<hostname>/` con un `_site_meta.json` de manifiesto (excluido explícitamente de `DEFAULT_EXCLUDED_FILES` para que nunca se indexe como contenido de wiki).
- **La exclusión de contenido de usuario es de doble capa**: (1) el prompt de `determineWikiStructure` le dice al LLM que no genere páginas para contenido flaggeado `likely_user_content`, (2) `_exclude_website_user_content` en `data_pipeline.py` filtra esos documentos del índice RAG por completo, así que ni siquiera son recuperables vía retrieval aunque el LLM cite algo por error.
- **`WebFinding`** (modelo de `web_vuln_scanner`) es deliberadamente distinto de `CVEFinding` (modelo de `vuln_scanner`) — uno es por URL/check, el otro por paquete/versión. `remediation.py` los unifica duck-typing sobre campos compatibles (`severity`, `remediation`/`ai_remediation`, `title`, `id`).
- **Los tres orquestadores de escaneo** (`vuln_scanner/orchestrator.py`, `web_vuln_scanner/orchestrator.py`, `web_crawler/crawler.py`) ahora loguean cada paso vía `logger.info()` en su callback interno `_p()`, además de enviar el mismo mensaje por WebSocket. Esto satisface el requisito de "debug en tiempo real visible en consola" pedido explícitamente por el usuario.
- **`docker_tools.py` nunca lanza Docker eagerly** — `check_docker_status()` es la única llamada síncrona barata (usa `docker info`/`docker image inspect` con timeout corto); `ensure_image()` solo se invoca cuando un escaneo realmente necesita el toolkit y el usuario activó el toggle.
- **El Dockerfile (`docker/vulnscan/Dockerfile`) es un fork textual** de `tmp/redamon/mcp/kali-sandbox/Dockerfile` (mismo Kali base + lista de herramientas), pero sin la sección de MCP servers al final (se reemplazó por un `entrypoint.sh` de dispatch simple: `exec "$@"`). Esto significa que la imagen tiene TODO el arsenal de RedAmon (metasploit, hydra, sqlmap, John, hashcat, ysoserial, BloodHound tooling, etc.) aunque `docker_tools.py` solo usa un subconjunto — dejado así deliberadamente por si se quieren usar más herramientas en el futuro sin reconstruir la imagen.

---

## 4. Verificaciones que SÍ se hicieron con datos reales (para no repetir trabajo)

- Crawler probado contra `example.com`, `httpbin.org`, y `https://kroryandev.com/` (8 páginas reales, JS renderizado con Playwright, estructura de Markdown correcta).
- Scanner de dependencias (OSV) probado contra `django==3.2.0`, `express==4.17.1`, `flask==1.1.0`, `axios==0.21.0`, `requests==2.25.0` — 66 hallazgos limpios, 0 UNKNOWN, deduplicación de alias verificada, fixed_version siempre correcto.
- Checks Python puros de `web_vuln_scanner` (headers/cookies/CORS/etc. — nota: el módulo `extra_checks.py` con checks hardcodeados de WordPress se **eliminó** a petición del usuario, sustituido por `wpscan` real) probados contra `kroryandev.com` real.
- `nmap` vía Docker probado contra `kroryandev.com` (puertos 80/443 abiertos, resto filtrado/cerrado — resultado correcto).
- `testssl` vía Docker probado contra `kroryandev.com` — encontró el bug de parseo real (estructura JSON anidada, no plana), se arregló, y tras el fix produjo 11 hallazgos accionables reales (BREACH potencialmente vulnerable, HSTS no ofrecido, LUCKY13, OCSP stapling no ofrecido, DNS CAA no configurado, cifrados obsoletos).
- Imagen Docker completa construida con éxito (~15GB), herramientas verificadas presentes: `nmap`, `nuclei`, `gitleaks`, `semgrep`, `whatweb`, `nikto`, `httpx`, `testssl` (nota: el binario se llama `testssl`, no `testssl.sh`, en Kali — ya corregido en el código).
- `build_remediation_plan` probado con datos sintéticos: agrupa correctamente por acción, toma la severidad peor, excluye hallazgos `ai_dismissed`.

---

## 5. Orden sugerido para retomar (si quieres una checklist)

1. **Probar en el navegador de verdad**: generar una wiki de un sitio web pequeño real, verificar que el crawl progresa, que la wiki se genera bien, que "Website Security" corre y muestra resultados.
2. **Verificar cada herramienta del Docker toolkit una por una** contra un sitio de prueba, comparando el JSON crudo con lo que el parser produce (mismo proceso que se usó para arreglar `testssl`).
3. **Levantar `docker compose up` una vez** para confirmar que Neo4j/Postgres arrancan y que `graph_db.py` se conecta sin errores.
4. **Decidir qué hacer con Postgres**: implementar el writer de historial, o quitarlo del compose si no se va a usar pronto.
5. **Portar las opciones de website a `ModelSelectionModal`** (Update Wiki) para paridad con Generate Wiki.
6. **Subir la imagen Docker** con los comandos de la sección 0.
7. Si hay tiempo: extraer los strings hardcodeados en inglés de las nuevas UI a los archivos de idioma.
