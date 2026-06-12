from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Load a prompt text file from this package (e.g. synthesize_report.txt)."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")
