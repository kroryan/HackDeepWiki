# Setup local con Ollama

Esta instalación mantiene intactos los archivos funcionales de upstream. La
personalización vive en `hackdeepwiki.sh` y en overrides de Docker Compose, de modo
que un `git pull` o merge futuro pueda incorporar cambios sin sobrescribir la
configuración local.

## Requisitos

- Docker con Docker Compose v2
- Ollama
- Bash y `curl`

En Linux nativo el script usa automáticamente red host, porque Ollama suele
escuchar solo en `127.0.0.1`. En Docker Desktop, WSL, macOS y Windows usa red
bridge y `host.docker.internal`. Puede forzarse con `--network host` o
`--network bridge`.

## Primer uso

```bash
./hackdeepwiki.sh setup
./hackdeepwiki.sh up --no-build
```

La interfaz queda en <http://localhost:3000> y la API en
<http://localhost:8001>. El proveedor predeterminado es Ollama. En cada
arranque, HackDeepWiki consulta `/api/tags` y muestra los modelos de generación
publicados por ese endpoint. Los modelos con capacidad `embedding` se usan
separadamente y no aparecen mezclados en el selector. Cuando está disponible,
`nomic-embed-text` se prioriza automáticamente como embedder por su menor
consumo y latencia; `--embed-model` permite elegir otro explícitamente.

## Uso diario

```bash
./hackdeepwiki.sh status
./hackdeepwiki.sh health
./hackdeepwiki.sh models
./hackdeepwiki.sh test
./hackdeepwiki.sh logs -f
./hackdeepwiki.sh down
```

Para usar otro servidor Ollama y leer automáticamente su catálogo:

```bash
./hackdeepwiki.sh up --no-build \
  --ollama-endpoint http://100.94.16.58:11434
```

`--ollama-model` sigue disponible opcionalmente para elegir cuál de los
modelos descubiertos aparecerá como predeterminado:

```bash
./hackdeepwiki.sh up --no-build \
  --ollama-endpoint http://100.94.16.58:11434 \
  --ollama-model ornith:35b
```

Los modelos remotos lentos no tienen un timeout de generación. Los embeddings
se envían por lotes de 32 y cada lote admite hasta 1800 segundos. Ambos valores
pueden ajustarse para servidores con poca RAM o enlaces muy lentos:

```bash
./hackdeepwiki.sh up --no-build \
  --ollama-endpoint http://100.94.16.58:11434 \
  --embed-batch-size 16 \
  --ollama-timeout 3600
```

Las opciones persistentes pueden guardarse copiando
`hackdeepwiki.env.example` a `hackdeepwiki.env`. Este último y el runtime `.hackdeepwiki/`
están ignorados por Git.

## Límite de GitHub

Las consultas anónimas de GitHub tienen un límite bajo. Para repositorios
públicos, HackDeepWiki cambia automáticamente a un clon Git superficial cuando se
agota esa cuota, por lo que no es obligatorio configurar un token.

Para repositorios privados, o para evitar incluso ese fallback, cree un token
de GitHub de solo lectura, copie el ejemplo y guárdelo en `hackdeepwiki.env`:

```bash
cp hackdeepwiki.env.example hackdeepwiki.env
# Edite hackdeepwiki.env y establezca:
GITHUB_TOKEN=github_pat_...
chmod 600 hackdeepwiki.env
./hackdeepwiki.sh up --no-build
```

El token se utiliza únicamente en el servidor mediante `/api/github`; no se
incluye en el JavaScript enviado al navegador. También puede indicarse mediante
un archivo:

```bash
./hackdeepwiki.sh up --github-token-file /ruta/segura/github.token
```

El contenedor tiene `restart: "no"`: solo arranca al ejecutar `up`, nunca por
reiniciar el equipo o el daemon de Docker. `down` no detiene Ollama.
