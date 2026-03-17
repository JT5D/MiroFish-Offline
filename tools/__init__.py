"""
MiroFish Reusable Toolkit
=========================
Standalone utilities extracted from the MiroFish-Offline codebase.
Each module is self-contained and can be copied into other projects.

Infrastructure:
    retry           — Exponential backoff decorators + batch retry client
    task_manager    — Thread-safe async task tracking with progress reporting
    ipc             — File-based inter-process communication (command/response)
    llm_client      — OpenAI-compatible LLM wrapper with Ollama support
    file_parser     — Text extraction (PDF/MD/TXT) with encoding detection + chunking
    logger          — Dual-output rotating file + console logging

Agent patterns:
    json_repair     — Multi-stage LLM JSON output repair (truncation, fences, corruption)
    llm_agent       — Base class for LLM-backed agents (prompt → call → parse → fallback)
    batch_processor — Parallel batch processing with per-item isolation + progress
"""
