# FreeDeepWiki
<img width="1344" height="759" alt="image" src="https://github.com/user-attachments/assets/b88d40f8-78f9-413e-9327-a10e2610f0cf" />
<img width="662" height="822" alt="image" src="https://github.com/user-attachments/assets/8c28d9d9-92ab-4877-afbe-56c40df03294" />
<img width="1907" height="780" alt="image" src="https://github.com/user-attachments/assets/3a454f91-39b7-4a25-a4b6-2c49cc3480fc" />





**FreeDeepWiki** turns any Git repository into an interactive, AI-generated wiki. Point it at a GitHub, GitLab, or Bitbucket repo (or a local folder) and it will:

1. Analyze the code structure
2. Generate structured documentation, page by page
3. Draw Mermaid diagrams of how things fit together
4. Let you chat with the repository (RAG-powered Q&A and multi-step Deep Research)

It ships as a **single portable binary** — an AppImage on Linux, a `.exe` on Windows — with no Docker, no database, and no mandatory API key: it works out of the box against a local [Ollama](https://ollama.com) install, and can also talk to OpenAI, Google Gemini, Anthropic Claude, OpenRouter, AWS Bedrock, Azure OpenAI, Alibaba Dashscope, or any OpenAI-compatible endpoint (Novita, Together, Groq, vLLM, LM Studio, and similar).

[English](./README.md) | [简体中文](./README.zh.md) | [繁體中文](./README.zh-tw.md) | [日本語](./README.ja.md) | [Español](./README.es.md) | [한국어](./README.kr.md) | [Tiếng Việt](./README.vi.md) | [Português Brasileiro](./README.pt-br.md) | [Français](./README.fr.md) | [Русский](./README.ru.md)

## Features

- **Automatic wiki generation** for any public or token-authenticated GitHub / GitLab / Bitbucket repository, or a local directory.
- **Visual architecture diagrams** rendered with Mermaid, generated alongside the docs.
- **Ask & Deep Research** — a chat panel grounded in the repo's own code (RAG over embeddings), plus a multi-iteration research mode for harder questions.
- **Fully portable** — download one file, run it, done. No containers, no services to stand up, no `.env` to hand-edit before your first run.
- **Local-first by default** — the packaged app auto-discovers a running Ollama instance and uses it for both generation and embeddings, so it works fully offline with zero API keys.
- **Bring your own provider** when you want cloud-grade models: OpenAI, Google Gemini, Anthropic Claude (API key or a Pro/Max subscription token from `claude login`), OpenRouter, AWS Bedrock, Azure OpenAI, Alibaba Dashscope, or any OpenAI-compatible API (Novita, Together, Groq, vLLM, LM Studio, etc.) via the custom-endpoint option.
- **Multi-language wiki output** with a language switcher in the UI.

## Quick start — portable app (recommended)

1. Grab the latest build from the [Releases page](https://github.com/kroryan/FreeDeepWiki/releases):
   - **Linux** → `FreeDeepWiki-x86_64.AppImage`
   - **Windows** → `FreeDeepWiki-windows-x64.exe`
2. Make it executable and run it (Linux: `chmod +x FreeDeepWiki-x86_64.AppImage && ./FreeDeepWiki-x86_64.AppImage`; Windows: just double-click it).
3. It starts its own local server, waits for it to come up, and opens your browser automatically at `http://127.0.0.1:<port>`.
4. Paste a repository URL and generate your first wiki. If you have Ollama running locally, no further setup is needed — otherwise open the model settings panel and add an API key for the provider of your choice.

Every push to `main` publishes an updated pre-release build; tagged commits (`vX.Y.Z`) publish a stable release. Both platforms are built and attached automatically by [`.github/workflows/release.yml`](.github/workflows/release.yml).

## Configuring a provider

Open the model/provider selector in the app and pick one of:

| Provider | What you need |
|---|---|
| **Ollama** | Nothing — auto-detected at `http://localhost:11434` if running. |
| **OpenAI** | An API key from the OpenAI platform dashboard. |
| **Claude** | An Anthropic API key, **or** a Claude Pro/Max subscription token from `claude login` (Claude Code CLI) — both are sent straight to `api.anthropic.com` with the right auth headers. |
| **Google Gemini** | A free API key from Google AI Studio. |
| **OpenRouter / Bedrock / Azure / Dashscope** | Credentials for that provider, entered in the same settings panel. |
| **Custom (OpenAI-compatible)** | Any OpenAI-compatible base URL (Novita, Together, Groq, vLLM, LM Studio, ...) plus its API key. Use the **Reload** button to fetch the model list from that endpoint. |

## Running from source

```bash
# Frontend
npm install
npm run dev            # http://localhost:3000

# Backend (separate terminal)
cd api
poetry install --only main
poetry run python -m api.main   # http://localhost:8001
```

The frontend proxies API/WebSocket calls to `SERVER_BASE_URL` (defaults to `http://localhost:8001`).

## Building the portable binaries yourself

```bash
npm run build
python scripts/prepare_assets.py linux   # or "windows"
pip install poetry pyinstaller
poetry -C api install --only main
pyinstaller freedeepwiki.spec
```

This bundles the built Next.js frontend, the FastAPI backend, and a Node.js runtime into a single PyInstaller binary (`scripts/launcher.py` is the entrypoint), which the Linux job then wraps into an AppImage.

## 🤝 Contributing

Contributions are welcome:
- Open issues for bugs or feature requests
- Submit pull requests to improve the code
- Share feedback and ideas

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## ⭐ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=kroryan/freedeepwiki&type=Date)](https://star-history.com/#kroryan/freedeepwiki&Date)

## 🙏 Credits

This project is a fork of [deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open) by [AsyncFuncAI](https://github.com/AsyncFuncAI). All credit for the original codebase goes to them.
