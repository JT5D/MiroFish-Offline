"""
MiroFish Reusable Toolkit
=========================
Standalone utilities extracted from the MiroFish-Offline codebase.
Each module is self-contained and can be copied into other projects.

Modules:
    retry         — Exponential backoff decorators + batch retry client
    task_manager  — Thread-safe async task tracking with progress reporting
    ipc           — File-based inter-process communication (command/response)
    llm_client    — OpenAI-compatible LLM wrapper with Ollama support
    file_parser   — Text extraction (PDF/MD/TXT) with encoding detection + chunking
    logger        — Dual-output rotating file + console logging
"""
