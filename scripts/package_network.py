"""Package the portable server/client preview without runtime data or credentials."""

import hashlib
import json
import zipfile
from pathlib import Path


root = Path(__file__).resolve().parent.parent
files = [root / name for name in (
    ".gitignore", "agent_board.py", "install.py", "schema.json", "LICENSE", "README.md", "README.zh-CN.md",
    "SKILL.md", "AGENTS.snippet.md", "test_agent_board_kit.py", "test_board_network.py",
    "agents/openai.yaml", "scripts/server.sh", "scripts/client.sh", "scripts/client.ps1", "scripts/smoke_network.py")]
for folder, pattern in (("board_network", "*.py"), ("docs", "*.md"), ("examples/network", "*.json")):
    files.extend(sorted((root / folder).glob(pattern)))
out = root / "dist"
out.mkdir(exist_ok=True)
archive = out / "agent-board-network-0.1.0-preview.zip"
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for path in files:
        z.write(path, "agent-board-kit/" + path.relative_to(root).as_posix())
manifest = {"file": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "files": len(files)}
(out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps(manifest, indent=2))
