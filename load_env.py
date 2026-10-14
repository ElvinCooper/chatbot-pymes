"""Arranca los scripts con dotenv interpretado como datos, nunca como shell."""

import os
from pathlib import Path
import subprocess
import sys
from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parent
    shell = Path(sys.argv[1]).resolve()
    script = Path(sys.argv[2]).resolve()
    if shell.name not in {"bash", "bash.exe"} or not shell.is_file():
        raise SystemExit("Shell inválido")
    if script.parent != root or script.name not in {
        "run_bot.sh",
        "run_whatsapp.sh",
        "run_messenger.sh",
    }:
        raise SystemExit("Script no autorizado")
    values = dotenv_values(root / ".env", interpolate=False)
    env = os.environ.copy()
    env.update({key: value for key, value in values.items() if value is not None})
    env["CHATBOT_DOTENV_LOADED"] = "1"
    return subprocess.call([str(shell), script.as_posix(), *sys.argv[3:]], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
